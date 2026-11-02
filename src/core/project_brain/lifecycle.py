"""Durable requests, fenced checkpoints, and bounded model accounting."""

import hashlib
import sys
from datetime import timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.config import settings
from src.core.llm_client import TokenUsage
from src.core.project_brain import GENERATOR_VERSION, SCHEMA_VERSION
from src.core.project_brain.compiler import encoded
from src.core.project_brain.recipes import component_recipes
from src.core.project_brain.artifacts import ArtifactStore
from src.core.project_brain.exceptions import BudgetBusy, BudgetExhausted
from src.models import SyncSession
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainPublication, BrainUnit, utcnow
from src.core.project_brain.usage import (HTTP_ERROR_UNIT_LIMIT, HTTP_RETRY_STATUSES, CallSpec,
    _http_error_valid, abandon_reservation, model_label, validate_call_id, validate_transport_reservation)

ACTIVE_STATES = {"pending", "scanning", "extracting", "compiling", "validating"}


def recipe_hash() -> str:
    from src.core.project_brain.recipes import recipe_hash as current_recipe
    return current_recipe()


def enrolled(session: Session, project_id: int, branch: str) -> bool:
    """已登记的分支独占文档维护权，避免旧 README 同时写回。"""
    return bool(session.scalar(select(BrainProject.id).where(BrainProject.gitlab_project_id == project_id,
                                                           BrainProject.branch == branch,
                                                           BrainProject.maintenance_state.in_(["active", "paused"])).limit(1)))


def mark_dirty(session: Session, project_id: int, branch: str, name: str | None = None, deleted: bool = False) -> bool:
    """先持久化事件；只有调度确认新输入才推进构建序号。"""
    project = session.scalar(select(BrainProject).where(BrainProject.gitlab_project_id == project_id, BrainProject.branch == branch).with_for_update())
    if project is None:
        if name is None or deleted:
            return False
        project = BrainProject(gitlab_project_id=project_id, branch=branch, project_name=name,
                               gitlab_sync=settings.PROJECT_BRAIN_GITLAB_SYNC, maintenance_state="active")
        session.add(project)
        session.flush()
    if project.maintenance_state == "paused":
        return False
    project.maintenance_state, project.auto_update = "active", True
    now = utcnow()
    project.event_seq += 1
    project.dirty_at = now
    project.dirty_since = project.dirty_since or now
    if deleted:
        if project.source_status != "missing":
            project.request_seq += 1
        project.source_status, project.last_checked_at = "missing", now
        project.last_observed_head = None
        project.dirty_at = project.dirty_since = None
    return True


def create_build(session: Session, project_id: int, branch: str, name: str, sha: str, mode: str = "auto", historical: bool = False,
                 observed_seq: int | None = None, fingerprint: str | None = None,
                 reusable_id: str | None = None, observed_request_seq: int | None = None) -> BrainBuild | None:
    """登记或复用确定 SHA 的构建；历史构建不改变当前维护请求。"""
    project = session.scalar(select(BrainProject).where(BrainProject.gitlab_project_id == project_id, BrainProject.branch == branch).with_for_update())
    if project is None:
        project = BrainProject(gitlab_project_id=project_id, branch=branch, project_name=name,
                               gitlab_sync=settings.PROJECT_BRAIN_GITLAB_SYNC, auto_update=not historical,
                               maintenance_state="unmanaged" if historical else "active")
        session.add(project)
        session.flush()
    if observed_seq is not None and (observed_seq != project.event_seq or project.maintenance_state != "active"):
        return None
    if observed_request_seq is not None and observed_request_seq != project.request_seq:
        return None
    recipe = recipe_hash()
    build = session.scalar(select(BrainBuild).where(BrainBuild.project_id == project.id, BrainBuild.source_commit == sha,
                                                  BrainBuild.recipe_hash == recipe, BrainBuild.mode == mode)
                           .order_by(BrainBuild.generation.desc()).limit(1).with_for_update())
    generation = (build.generation or 0) if build else 0
    # A closed privacy history cannot be reopened or retried. An explicit new
    # request gets an independent generation; background reconciliation leaves
    # the failed record and its accounting intact until that request arrives.
    if (build and build.status == "failed" and observed_seq is None
            and (build.coverage or {}).get("public_schema_version") == "2.0"
            and "failure_code=privacy_seal_conflict" in (build.error_message or "")):
        generation += 1
        build = None
    if build and build.status == "ready":
        try:
            if not build.artifact_path:
                raise ValueError("旧产物已清理")
            ArtifactStore().repository(build.id)
        except (OSError, ValueError):
            generation += 1
            build = None
    if not build and reusable_id:
        candidate = session.get(BrainBuild, reusable_id)
        if (candidate and candidate.project_id == project.id and not candidate.historical and candidate.status == "ready"
                and candidate.artifact_path and candidate.recipe_hash == recipe and candidate.source_fingerprint == fingerprint):
            build = candidate
    if not historical:
        if project.maintenance_state == "paused":
            return None
        project.maintenance_state, project.auto_update, project.source_status = "active", True, "available"
        if not build or build.request_seq != project.request_seq or build.historical:
            project.request_seq += 1
        project.desired_sha, project.last_observed_head, project.last_checked_at = sha, sha, utcnow()
        project.dirty_at = project.dirty_since = None
        if fingerprint:
            project.observed_fingerprint = fingerprint
    if build:
        if not historical:
            build.request_seq = project.request_seq
            build.historical = False
            if build.status == "ready":
                project.active_build_id = build.id
                project.observed_fingerprint = build.source_fingerprint
                if project.gitlab_sync and not session.scalar(select(BrainPublication.id).where(BrainPublication.build_id == build.id)):
                    session.add(BrainPublication(build_id=build.id, target_branch=project.branch))
            elif build.status == "superseded":
                build.status = "pending"
                for unit in session.scalars(select(BrainUnit).where(BrainUnit.build_id == build.id, BrainUnit.status != "completed").with_for_update()):
                    abandon_reservation(unit, session)
                    unit.status, unit.lease_token, unit.lease_until = "pending", None, None
        return build
    previous = session.scalar(select(BrainBuild).where(BrainBuild.project_id == project.id, BrainBuild.status == "ready",
                                                      BrainBuild.historical.is_(False), BrainBuild.artifact_path.is_not(None))
                              .order_by(BrainBuild.brain_version.desc()).limit(1))
    build = BrainBuild(project_id=project.id, source_commit=sha, recipe_hash=recipe, mode=mode,
                       generation=generation,
                       coverage={"public_schema_version": "2.0", "component_recipes": component_recipes()},
                       diagnostic={"generator_version": GENERATOR_VERSION},
                       request_seq=project.request_seq, brain_version=project.next_version, historical=historical,
                       base_build_id=previous.id if previous else None)
    project.next_version += 1
    session.add(build)
    session.flush()
    session.add(BrainUnit(build_id=build.id, key="scan", kind="scan"))
    return build


def claim_unit(unit_id: int) -> tuple[BrainUnit, BrainBuild, BrainProject, str] | None:
    """数据库锁及租约令牌阻止重复消息和晚完成任务更新检查点。"""
    with SyncSession() as session:
        observed = session.get(BrainUnit, unit_id)
        if not observed:
            return None
        observed_build = session.get(BrainBuild, observed.build_id)
        project = session.scalar(select(BrainProject).where(BrainProject.id == observed_build.project_id).with_for_update())
        build = session.scalar(select(BrainBuild).where(BrainBuild.id == observed.build_id).with_for_update().execution_options(populate_existing=True))
        unit = session.scalar(select(BrainUnit).where(BrainUnit.id == unit_id).with_for_update().execution_options(populate_existing=True))
        if not unit or unit.status == "completed" or unit.status == "failed":
            return None
        now = utcnow()
        if unit.status == "running" and unit.lease_until and unit.lease_until > now:
            return None
        if build.status not in ACTIVE_STATES or build.execution_state != "running":
            return None
        if not build.historical and (build.request_seq != project.request_seq or project.maintenance_state != "active"
                                     or project.branch not in settings.main_branch_list or project.source_status == "missing"):
            build.status = "superseded"
            session.commit()
            return None
        # 软限时 600 秒，加 90 秒防止尚在退出的旧进程抢占。
        if unit.status == "running":
            abandon_reservation(unit, session)
        unknown = session.scalar(select(BrainModelCall.id).where(
            BrainModelCall.build_id == build.id, BrainModelCall.state == "unknown").limit(1))
        units = session.scalars(select(BrainUnit).where(BrainUnit.build_id == build.id)).all()
        if unknown is not None or any((row.payload or {}).get("_unknown_tokens", 0) != 0 for row in units):
            # Broker redelivery can arrive before Beat sees an expired lease.
            # A new token cannot recover the old response or authorize billing
            # unrelated work that the complete-call gate would later reject.
            build.status, build.completed_at, build.execution_state = "failed", now, "running"
            build.error_message = "知识任务调用未确认，检查点与费用保留；failure_code=model_calls_incomplete"
            for other in session.scalars(select(BrainUnit).where(
                    BrainUnit.build_id == build.id, BrainUnit.status.in_(["pending", "running"]))
                    .order_by(BrainUnit.id).with_for_update()):
                abandon_reservation(other, session)
                other.status, other.lease_token, other.lease_until, other.dispatched_at = "failed", None, None, None
                other.error_message = "同一知识构建有未确认调用，停止继续领取"
            session.commit()
            return None
        token = str(uuid4())
        unit.status, unit.lease_token, unit.lease_until = "running", token, now + timedelta(seconds=690)
        unit.attempts += 1
        build.status = {"scan": "scanning", "extract": "extracting", "privacy": "extracting", "finalize": "extracting", "source_privacy": "extracting",
                        "compile": "compiling", "summary": "compiling", "module_summary": "compiling",
                        "project_summary": "compiling", "planning": "compiling", "assemble": "validating"}[unit.kind]
        session.commit()
        session.expunge_all()
        return unit, build, project, token


def reserve_tokens(build_id: str, token: str, unit_id: int, amount: int, *,
                   call_id: str | None = None, call_spec: CallSpec | None = None) -> None:
    """持久化保守调用预算；进程丢失响应时仍计入最大已预留消耗。"""
    with SyncSession() as session:
        observed = session.get(BrainBuild, build_id)
        project = session.scalar(select(BrainProject).where(BrainProject.id == observed.project_id).with_for_update())
        # session.get above may have loaded the row before waiting for the
        # project lock. Refresh under the build lock before updating the budget.
        build = session.scalar(select(BrainBuild).where(BrainBuild.id == build_id).with_for_update().execution_options(populate_existing=True))
        unit = session.scalar(select(BrainUnit).where(BrainUnit.id == unit_id).with_for_update())
        if (unit is None or unit.build_id != build_id or unit.lease_token != token
                or unit.status != "running" or unit.lease_until is None or unit.lease_until <= utcnow()
                or build.status not in ACTIVE_STATES or build.execution_state not in {"running", "pausing"}):
            raise ValueError("知识任务租约已失效")
        if not build.historical and (build.request_seq != project.request_seq or project.maintenance_state != "active"
                                     or project.branch not in settings.main_branch_list or project.source_status == "missing"):
            raise ValueError("知识维护请求已失效")
        if type(amount) is not int or amount < 0:
            raise ValueError("知识预算预留不得为负数")
        if (call_id is None) != (call_spec is None):
            raise ValueError("知识模型记账元数据不完整")
        if call_id is not None:
            validate_call_id(call_id)
            if not isinstance(call_spec, CallSpec):
                raise ValueError("知识模型记账元数据无效")
            call_spec.validate()
            if amount == 0:
                raise ValueError("知识模型调用必须预留正额度")
            previous = session.get(BrainModelCall, call_id)
            if previous is not None:
                raise ValueError("知识模型调用标识已使用，禁止重复发送")
            validate_transport_reservation(session, build_id, unit, token, call_spec)
        if build.reserved_tokens + amount > settings.PROJECT_BRAIN_MAX_BUILD_TOKENS:
            siblings = session.scalars(select(BrainUnit).where(
                BrainUnit.build_id == build_id, BrainUnit.id != unit_id,
                BrainUnit.status == "running", BrainUnit.lease_until > utcnow()))
            live_reserved = sum(max(0, int((sibling.payload or {}).get("_reserved_tokens", 0))) for sibling in siblings)
            # Never release unknown charges. A retry merely waits for a live
            # request to settle; it must reserve again under the same locks.
            if live_reserved and build.reserved_tokens - live_reserved + amount <= settings.PROJECT_BRAIN_MAX_BUILD_TOKENS:
                raise BudgetBusy()
            raise BudgetExhausted("知识构建 Token 预算耗尽")
        build.reserved_tokens += amount
        unit.payload = {**(unit.payload or {}), "_reserved_tokens": int((unit.payload or {}).get("_reserved_tokens", 0)) + amount}
        if call_id is not None:
            session.add(BrainModelCall(id=call_id, build_id=build_id, unit_id=unit_id, lease_token=token,
                stage=unit.kind, role=call_spec.role, purpose=call_spec.purpose,
                requested_model=call_spec.requested_model, attempt=call_spec.attempt, reserved_tokens=amount,
                transport_group=call_spec.transport_group, transport_attempt=call_spec.transport_attempt))
        session.commit()


def close_http_error(build_id: str, unit_id: int, lease_token: str, call_id: str, bound: int, status: int) -> int:
    """Close one complete HTTP failure without fabricating actual usage."""
    validate_call_id(call_id)
    if type(bound) is not int or bound <= 0 or type(status) is not int or status not in HTTP_RETRY_STATUSES:
        raise ValueError("知识HTTP错误记账元数据无效")
    with SyncSession() as session:
        build = session.scalar(select(BrainBuild).where(BrainBuild.id == build_id).with_for_update())
        unit = session.scalar(select(BrainUnit).where(BrainUnit.id == unit_id).with_for_update())
        call = session.scalar(select(BrainModelCall).where(BrainModelCall.id == call_id).with_for_update())
        if (build is None or unit is None or call is None or unit.build_id != build_id
                or call.build_id != build_id or call.unit_id != unit_id or call.lease_token != lease_token
                or unit.lease_token != lease_token or unit.status != "running" or build.status not in ACTIVE_STATES
                or build.execution_state not in {"running", "pausing"}
                or unit.lease_until is None or unit.lease_until <= utcnow()
                or call.reserved_tokens != bound or call.transport_group is None or call.transport_attempt is None):
            raise ValueError("知识HTTP错误记账归属无效")
        CallSpec(call.role, call.requested_model, call.purpose, call.attempt,
                 call.transport_group, call.transport_attempt).validate()
        count = session.scalar(select(func.count()).select_from(BrainModelCall).where(
            BrainModelCall.build_id == build_id, BrainModelCall.unit_id == unit_id,
            BrainModelCall.state == "http_error"))
        if call.state == "http_error":
            if call.http_status != status or not _http_error_valid(call):
                raise ValueError("知识HTTP错误重复关闭内容不一致")
            return count
        if (call.state != "reserved" or count >= HTTP_ERROR_UNIT_LIMIT
                or call.http_status is not None or call.closed_at is not None or call.settled_at is not None
                or call.response_model is not None or call.usage_estimated
                or any(type(value) is not int or value != 0 for value in
                       (call.prompt_tokens, call.completion_tokens, call.cached_tokens))):
            raise ValueError("知识HTTP错误调用状态无效")
        # Only the live unit counter changes. The build retains the complete
        # upper bound, distinct from actual/estimated prompt and completion.
        payload = unit.payload or {}
        live = payload.get("_reserved_tokens", 0)
        if type(live) is not int or live < bound or build.reserved_tokens < bound:
            raise ValueError("知识HTTP错误预留额度无效")
        call.state, call.http_status, call.closed_at = "http_error", status, utcnow()
        unit.payload = {**payload, "_reserved_tokens": live - bound}
        session.commit()
        return count + 1


def account_tokens(build_id: str, usage: TokenUsage, estimated: bool, bound: int, unit_id: int | None = None,
                   lease_token: str | None = None, *, call_id: str | None = None, response_model: str | None = None) -> None:
    """记录实际/估算 Token，供应商异常超出预留预算时阻止发布。"""
    with SyncSession() as session:
        build = session.scalar(select(BrainBuild).where(BrainBuild.id == build_id).with_for_update())
        call = None
        if call_id is not None:
            validate_call_id(call_id)
            unit = session.scalar(select(BrainUnit).where(BrainUnit.id == unit_id, BrainUnit.build_id == build_id).with_for_update())
            call = session.scalar(select(BrainModelCall).where(BrainModelCall.id == call_id).with_for_update())
            if (unit is None or call is None or call.build_id != build_id or call.unit_id != unit_id
                    or call.lease_token != lease_token or call.reserved_tokens != bound):
                raise ValueError("知识模型记账归属无效")
            if (type(estimated) is not bool or any(type(value) is not int or value < 0 for value in
                    (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens))
                    or usage.cached_tokens > usage.prompt_tokens):
                raise ValueError("知识模型用量结构无效")
            # A provider can put arbitrary data in response.model. Only retain
            # the already configured public alias, never an unclassified value.
            reported_model = model_label(response_model)
            if reported_model != call.requested_model:
                reported_model = None
            if call.state == "settled":
                if ((call.prompt_tokens, call.completion_tokens, call.cached_tokens, call.usage_estimated, call.response_model)
                        != (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens, estimated, reported_model)):
                    raise ValueError("知识模型重复结算内容不一致")
                if build.reserved_tokens > settings.PROJECT_BRAIN_MAX_BUILD_TOKENS:
                    raise BudgetExhausted("供应商用量超出知识构建预算")
                return
            if call.state not in {"reserved", "unknown"}:
                raise ValueError("知识模型记账状态无效")
            call.state, call.settled_at = "settled", utcnow()
            call.prompt_tokens, call.completion_tokens, call.cached_tokens = usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens
            call.usage_estimated, call.response_model = estimated, reported_model
        build.total_prompt_tokens += usage.prompt_tokens
        build.total_completion_tokens += usage.completion_tokens
        build.total_cached_tokens += usage.cached_tokens
        build.usage_estimated = build.usage_estimated or estimated
        # 有响应时结算并释放未用额度；丢失响应的预留仍然保留。
        build.reserved_tokens += usage.total_tokens - bound
        if unit_id is not None:
            unit = session.scalar(select(BrainUnit).where(BrainUnit.id == unit_id, BrainUnit.build_id == build_id).with_for_update())
            if unit is None:
                raise ValueError("知识用量的任务归属无效")
            field = "_reserved_tokens" if lease_token is None or unit.lease_token == lease_token else "_unknown_tokens"
            unit.payload = {**(unit.payload or {}), field: max(0, int((unit.payload or {}).get(field, 0)) - bound)}
        session.commit()
        if call is not None:
            from src.utils.logger import logger
            logger.info("知识模型用量：build_id=%s unit_id=%s prompt=%s completion=%s cached=%s estimated=%s",
                        build_id, unit_id, usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens, estimated)
            logger.info("知识模型记账：build_id=%s unit_id=%s call_id=%s role=%s stage=%s purpose=%s requested_model=%s attempt=%d",
                        build_id, unit_id, call.id, call.role, call.stage, call.purpose, call.requested_model, call.attempt)
        if build.reserved_tokens > settings.PROJECT_BRAIN_MAX_BUILD_TOKENS:
            raise BudgetExhausted("供应商用量超出知识构建预算")


def freshness(project: BrainProject, build: BrainBuild | None) -> str:
    """将维护新鲜度与构建状态分开；文档提交不改变源码指纹。"""
    if not build:
        return "unknown"
    if project.dirty_at or project.source_status == "missing":
        return "stale"
    if project.observed_fingerprint is None:
        return "unknown"
    return "fresh" if project.observed_fingerprint == build.source_fingerprint else "stale"
