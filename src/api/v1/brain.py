"""Project Brain build, configuration, card, and export APIs."""

import asyncio
import hashlib
import re
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from src.config import settings
from src.core.gitlab_client import GitLabClient
from src.core.project_brain.artifacts import ArtifactStore, card_path
from src.core.project_brain.lifecycle import create_build, freshness, recipe_hash
from src.models import get_session
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainPublication, BrainUnit
from src.core.project_brain.usage import usage_data
from src.tasks.brain_task import dispatch_pending
from src.tasks.brain_publication_task import dispatch_publications
from src.utils.rate_limit import rate_limit_dependency
from src.utils.security import require_api_key

router = APIRouter(prefix="/projects/{project_id}/brain", tags=["Project Brain"], dependencies=[Depends(require_api_key)])
_READ = Depends(rate_limit_dependency("60/minute"))
_WRITE = Depends(rate_limit_dependency("5/minute"))


class RecoveryRequest(BaseModel):
    """Recovery always uses the parent's server-owned SHA and private paths."""
    model_config = ConfigDict(extra="forbid")


class EmptyControlRequest(BaseModel):
    """Execution control cannot change source, models, budget, or privacy."""

    model_config = ConfigDict(extra="forbid")


class BuildRequest(BaseModel):
    """客户端只指定 GitLab 分支和提交，不能提供 URL 或本机路径。"""

    model_config = ConfigDict(extra="forbid")
    branch: str = Field(min_length=1, max_length=255)
    commit_sha: str | None = Field(default=None, pattern=r"^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$")
    mode: Literal["auto", "full"] = "auto"

    @field_validator("branch")
    @classmethod
    def valid_branch(cls, value: str) -> str:
        """拒绝非法 Git 分支及命令选项样式。"""
        if (value.startswith(("-", "/")) or value.endswith(("/", ".", ".lock"))
                or ".." in value or "//" in value or "@{" in value or re.search(r"[\s~^:?*\[\\\x00-\x1f]", value)):
            raise ValueError("分支名无效")
        return value


class ProjectPatch(BaseModel):
    """按已登记分支配置自动维护和发布。"""

    model_config = ConfigDict(extra="forbid")
    auto_update: bool | None = None
    gitlab_sync: bool | None = None


def _enabled() -> None:
    if not settings.PROJECT_BRAIN_ENABLED:
        raise HTTPException(503, "Project Brain 尚未启用")


async def _project(session: AsyncSession, project_id: int, branch: str, lock: bool = False) -> BrainProject:
    query = select(BrainProject).where(BrainProject.gitlab_project_id == project_id, BrainProject.branch == branch)
    project = await session.scalar(query.with_for_update() if lock else query)
    if not project:
        raise HTTPException(404, "该分支尚未接入 Project Brain")
    return project


async def _build(session: AsyncSession, project_id: int, build_id: str) -> BrainBuild:
    build = await session.scalar(select(BrainBuild).join(BrainProject).where(BrainBuild.id == build_id, BrainProject.gitlab_project_id == project_id))
    if not build:
        raise HTTPException(404, "知识构建不存在")
    if build.status == "ready" and (build.coverage or {}).get("recovery_protocol"):
        from src.core.project_brain.recovery import verify_dependencies, RecoveryRejected
        try:
            await session.run_sync(lambda sync: verify_dependencies(sync, build))
        except RecoveryRejected:
            raise HTTPException(409, "恢复依赖已经改变，知识版本需要重新验证") from None
    return build


def _public_protocol(build: BrainBuild | None) -> bool:
    """The database marker is present before any source-derived display is safe."""
    return bool(build and (build.coverage or {}).get("public_schema_version") == "2.0")


def _error_code(message: str | None, fallback: str) -> str | None:
    """Expose fixed operational categories, never provider/source exception text."""
    if not message:
        return None
    codes = {"source_identity_privacy_conflict", "source_binding_invalid", "privacy_checkpoint_missing", "privacy_seal_conflict", "known_privacy_regression", "model_calls_incomplete", "capacity_limit", "summary_output_capacity",
             "output_privacy_window_exhausted", "retry_reasoning_privacy_window_exhausted", "privacy_response_invalid",
             "model_http_retry_exhausted", "model_http_retry_window_exhausted", "model_work_window_exhausted"}
    match = re.search(r"(?:^|[;；\s])failure_code=([a-z_]+)(?:$|[;；\s])", message)
    return match[1] if match and match[1] in codes else fallback


def _public_manifest(build: BrainBuild):
    """Read only sealed public metadata, authenticated by the DB manifest digest.

    The API has neither the identity key nor the privacy ledger. In-progress,
    failed, expired, or corrupt artifacts must never fall back to raw DB text.
    """
    if not _public_protocol(build) or build.status != "ready" or not build.artifact_path:
        return None
    expected = (build.coverage or {}).get("manifest_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise HTTPException(409, "知识公开清单未通过完整性认证")
    try:
        from src.core.project_brain.public_schema import parse_manifest

        content = ArtifactStore().repository(build.id)["docs/project-brain/manifest.json"]
        if hashlib.sha256(content.encode()).hexdigest() != expected:
            raise ValueError("manifest_mismatch")
        manifest = parse_manifest(content)
        if (manifest.schema_version != "2.0" or manifest.build_id != build.id
                or manifest.recipe_hash != build.recipe_hash or manifest.source_commit != build.source_commit):
            raise ValueError("manifest_identity_mismatch")
        return manifest
    except (ValueError, OSError, KeyError):
        raise HTTPException(409, "知识公开清单未通过完整性认证") from None


def _branch_data(manifest) -> dict:
    metadata = manifest.public_metadata if manifest else None
    return {"branch": metadata.branch_display if metadata else None,
            "branch_id": metadata.branch_id if metadata else None}


def _build_data(build: BrainBuild, manifest=None) -> dict:
    data = {key: getattr(build, key) for key in ("id", "source_commit", "brain_version", "generation", "historical", "base_build_id", "status", "recipe_hash",
                                               "execution_state", "pause_requested_at", "paused_at", "resumed_at",
                                               "coverage", "error_message", "total_prompt_tokens", "total_completion_tokens", "total_cached_tokens",
                                               "reserved_tokens", "usage_estimated", "usage_tracking_version", "created_at", "completed_at")}
    if _public_protocol(build):
        data["public_schema_version"] = "2.0"
        # Intermediate coverage can include paths, module names and cost-plan
        # descriptions. The sealed manifest is the only public display source.
        data["coverage"] = manifest.coverage if manifest else {}
        data["error_message"] = _error_code(build.error_message, "build_failed")
    return data


def _publication_data(publication: BrainPublication | None, *, public: bool = False, manifest=None) -> dict | None:
    if publication is None:
        return None
    data = {key: getattr(publication, key) for key in ("id", "status", "target_branch", "commit_sha", "merge_commit_sha", "mr_iid", "mr_url", "error_message", "error_code", "policy_version")}
    if public:
        data["target_branch"] = _branch_data(manifest)["branch"]
        # MR URLs are created after the artifact seal. Without a privacy key the
        # API cannot prove that a later DB URL is safe, so it remains withheld.
        data["mr_url"] = None
        data["error_message"] = "publication_failed" if publication.error_message else None
        codes = {"write_conflict", "invalid_diff", "invalid_mode", "unsafe_ancestor", "content_mismatch", "unowned_manifest",
                 "human_file", "human_edit", "invalid_artifact", "source_advanced", "publication_failed", "unprotected_source_branch"}
        data["error_code"] = publication.error_code if publication.error_code in codes else ("publication_failed" if publication.error_code else None)
    return data


@router.post("/builds", status_code=202, dependencies=[_WRITE])
async def request_build(project_id: int, request: BuildRequest, session: AsyncSession = Depends(get_session)) -> dict:
    """首建接入分支；显式 SHA 仅归档历史版本，不自动发布。"""
    _enabled()
    if project_id <= 0:
        raise HTTPException(422, "GitLab 项目 ID 无效")
    if not request.commit_sha and request.branch not in settings.main_branch_list:
        raise HTTPException(422, "自动维护分支必须在 MAIN_BRANCHES 中")
    client = GitLabClient(purpose="read")
    info = await client.get_project(project_id)
    head = await client.get_branch(project_id, request.branch)
    sha = (request.commit_sha or head["commit"]["id"]).lower()
    if request.commit_sha:
        commit = await client.brain_request("GET", f"/projects/{project_id}/repository/commits/{sha}")
        sha = commit["id"]
    for attempt in range(2):
        try:
            build = await session.run_sync(lambda sync: create_build(sync, project_id, request.branch, info["path_with_namespace"], sha,
                                                                   request.mode, historical=request.commit_sha is not None))
            await session.commit()
            break
        except IntegrityError:
            await session.rollback()
            if attempt:
                raise HTTPException(409, "知识构建并发登记冲突，请重试") from None
    if build is None:
        raise HTTPException(409, "该分支已暂停，请先恢复自动维护")
    await asyncio.to_thread(dispatch_pending, build.id)
    return {"build_id": build.id, "status": build.status, "url": f"/api/v1/projects/{project_id}/brain/builds/{build.id}"}


@router.get("", dependencies=[_READ])
@router.get("/", dependencies=[_READ], include_in_schema=False)
async def get_brain(project_id: int, branch: str = Query(...), session: AsyncSession = Depends(get_session)) -> dict:
    """查看当前版本、覆盖、新鲜度和独立发布状态。"""
    project = await _project(session, project_id, branch)
    build = await session.get(BrainBuild, project.active_build_id) if project.active_build_id else None
    if build and build.status == "ready" and (build.coverage or {}).get("recovery_protocol"):
        build = await _build(session, project_id, build.id)
    publication = await session.scalar(select(BrainPublication).where(BrainPublication.build_id == build.id)) if build else None
    public = build is None or _public_protocol(build)
    manifest = await asyncio.to_thread(_public_manifest, build) if build else None
    return {"project_id": project_id, **(_branch_data(manifest) if public else {"branch": branch}), "auto_update": project.auto_update, "gitlab_sync": project.gitlab_sync,
            "maintenance_state": project.maintenance_state, "event_seq": project.event_seq,
            "source_status": project.source_status, "last_observed_head": project.last_observed_head,
            "last_checked_at": project.last_checked_at,
            "request_seq": project.request_seq, "freshness": freshness(project, build), "current": _build_data(build, manifest) if build else None,
            "publication": _publication_data(publication, public=public, manifest=manifest)}


@router.patch("", dependencies=[_WRITE])
async def patch_brain(project_id: int, patch: ProjectPatch, branch: str = Query(...), session: AsyncSession = Depends(get_session)) -> dict:
    """修改接入分支开关。"""
    _enabled()
    project = await _project(session, project_id, branch, lock=True)
    for key, value in patch.model_dump(exclude_none=True).items():
        setattr(project, key, value)
    if patch.auto_update is not None:
        if patch.auto_update and branch not in settings.main_branch_list:
            raise HTTPException(422, "自动维护分支必须在 MAIN_BRANCHES 中")
        project.maintenance_state = "active" if patch.auto_update else "paused"
        project.request_seq += 1
        if patch.auto_update:
            from src.core.project_brain.lifecycle import mark_dirty

            await session.run_sync(lambda sync: mark_dirty(sync, project_id, branch))
        else:
            project.dirty_at = project.dirty_since = None
    if patch.gitlab_sync is True and project.active_build_id:
        publication = await session.scalar(select(BrainPublication).where(BrainPublication.build_id == project.active_build_id))
        if publication is None:
            session.add(BrainPublication(build_id=project.active_build_id, target_branch=branch))
    await session.commit()
    active = await session.get(BrainBuild, project.active_build_id) if project.active_build_id else None
    public = active is None or _public_protocol(active)
    manifest = await asyncio.to_thread(_public_manifest, active) if active else None
    return {**(_branch_data(manifest) if public else {"branch": branch}), "auto_update": project.auto_update,
            "maintenance_state": project.maintenance_state, "gitlab_sync": project.gitlab_sync}


@router.get("/builds", dependencies=[_READ])
async def list_builds(project_id: int, branch: str = Query(...), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                      session: AsyncSession = Depends(get_session)) -> dict:
    """分页列出该分支的历史构建。"""
    project = await _project(session, project_id, branch)
    query = select(BrainBuild).where(BrainBuild.project_id == project.id)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    items = (await session.scalars(query.order_by(BrainBuild.brain_version.desc()).offset((page - 1) * page_size).limit(page_size))).all()
    # Keep reads bounded by the page size; each schema-2 display comes from its
    # own authenticated immutable artifact rather than the latest branch.
    data = [_build_data(b, await asyncio.to_thread(_public_manifest, b)) for b in items]
    return {"total": total, "page": page, "page_size": page_size, "items": data}


@router.get("/builds/{build_id}", dependencies=[_READ])
async def get_build(project_id: int, build_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """查看阶段、检查点进度、Token 与发布状态。"""
    build = await _build(session, project_id, build_id)
    units = (await session.scalars(select(BrainUnit).where(BrainUnit.build_id == build.id).order_by(BrainUnit.id))).all()
    publication = await session.scalar(select(BrainPublication).where(BrainPublication.build_id == build.id))
    public = _public_protocol(build)
    manifest = await asyncio.to_thread(_public_manifest, build)
    return {**_build_data(build, manifest), "progress": {"completed": sum(u.status == "completed" for u in units), "total": len(units)},
            "units": [{"key": f"unit:{u.id}" if public else u.key, "kind": u.kind, "status": u.status, "attempts": u.attempts,
                       "error": _error_code(u.error_message, "unit_failed") if public else u.error_message} for u in units],
            "publication": _publication_data(publication, public=public, manifest=manifest)}


def _available(build: BrainBuild) -> None:
    if build.status != "ready":
        raise HTTPException(409, "知识构建尚未就绪")
    if not build.artifact_path:
        raise HTTPException(410, "该历史版本产物已按保留策略清理")


@router.get("/builds/{build_id}/usage", dependencies=[_READ])
async def get_model_usage(project_id: int, build_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """按模型/角色/阶段展示用量，旧账和未知费用明确保留。"""
    await _build(session, project_id, build_id)
    # Build counters and call rows come from one statement snapshot. Refresh
    # identity-map objects loaded by authorization, avoiding mixed-time shares.
    rows = (await session.execute(select(BrainBuild, BrainModelCall, BrainUnit).select_from(BrainBuild)
        .outerjoin(BrainModelCall, BrainModelCall.build_id == BrainBuild.id)
        .outerjoin(BrainUnit, BrainUnit.id == BrainModelCall.unit_id)
        .where(BrainBuild.id == build_id).execution_options(populate_existing=True))).all()
    if not rows:
        raise HTTPException(404, "知识构建不存在")
    build = rows[0][0]
    data = usage_data(build, [(call, unit) for _, call, unit in rows if call is not None])
    if (build.coverage or {}).get("recovery_protocol"):
        from src.core.project_brain.recovery import usage_lineage
        data["recovery"] = await session.run_sync(lambda sync: usage_lineage(sync, build))
    if _public_protocol(build):
        # Configured model aliases are free text and can collide with a source
        # secret. Machine labels preserve the numeric attribution, not aliases.
        labels = {(model["role"], model["requested_model"]): f"model:{index}"
                  for index, model in enumerate(data["models"], 1)}
        for model in data["models"] + data["breakdown"]:
            model["requested_model"] = labels[(model["role"], model["requested_model"])]
            if "response_model" in model:
                model["response_model"] = None
        data["identity_policy"] = "公开Schema 2仅显示构建内模型机器标签与角色；配置别名和供应商名称不公开"
    return {"build_id": build_id, **data}


@router.get("/builds/{build_id}/cards", dependencies=[_READ])
async def list_cards(project_id: int, build_id: str, kind: Literal["project", "architecture", "module", "interface", "data", "flow"] | None = None,
                     module_id: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     session: AsyncSession = Depends(get_session)) -> dict:
    """按卡片类型和模块分页查看规范 JSON。"""
    build = await _build(session, project_id, build_id)
    _available(build)
    await asyncio.to_thread(_public_manifest, build)
    cards = await asyncio.to_thread(ArtifactStore().read_cards, build.id)
    items = [c for c in cards if (kind is None or c.kind == kind) and (module_id is None or c.module_id == module_id)]
    return {"total": len(items), "page": page, "page_size": page_size, "items": [c.model_dump() for c in items[(page - 1) * page_size:page * page_size]]}


@router.get("/builds/{build_id}/cards/{card_id}", dependencies=[_READ], response_model=None)
async def get_card(project_id: int, build_id: str, card_id: str, format: Literal["json", "markdown"] = "json",
                   session: AsyncSession = Depends(get_session)) -> dict | PlainTextResponse:
    """取得单一卡片 JSON 或已验证 Markdown。"""
    build = await _build(session, project_id, build_id)
    _available(build)
    await asyncio.to_thread(_public_manifest, build)
    store = ArtifactStore()
    cards = await asyncio.to_thread(store.read_cards, build.id)
    card = next((c for c in cards if c.id == card_id), None)
    if not card:
        raise HTTPException(404, "知识卡片不存在")
    if format == "json":
        return card.model_dump()
    repository = await asyncio.to_thread(store.repository, build.id)
    return PlainTextResponse(repository[card_path(card)], media_type="text/markdown")


@router.get("/builds/{build_id}/export", dependencies=[_READ])
async def export_build(project_id: int, build_id: str, session: AsyncSession = Depends(get_session)) -> FileResponse:
    """下载完整知识 ZIP，含事实和关系、不含源码全文。"""
    build = await _build(session, project_id, build_id)
    _available(build)
    await asyncio.to_thread(_public_manifest, build)
    try:
        path = await asyncio.to_thread(ArtifactStore().export_path, build.id, (build.coverage or {}).get("export_sha256"))
    except (ValueError, OSError):
        raise HTTPException(409, "知识导出产物损坏或校验失败，请重新构建") from None
    return FileResponse(path, media_type="application/zip", filename=f"project-brain-{build.id}.zip")


@router.post("/builds/{build_id}/retry", status_code=202, dependencies=[_WRITE])
async def retry_build(project_id: int, build_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """只重试失败阶段，保留成功编译检查点。"""
    _enabled()
    build = await _build(session, project_id, build_id)
    if build.execution_state != "running":
        raise HTTPException(409, "构建已请求暂停，请先等待暂停完成并使用继续接口")
    if build.status != "failed":
        raise HTTPException(409, "只有失败构建可以重试")
    if _public_protocol(build) and _error_code(build.error_message, "build_failed") == "privacy_seal_conflict":
        raise HTTPException(409, "隐私封存冲突的构建不能续用旧检查点，请创建新构建")
    if _error_code(build.error_message, "build_failed") == "known_privacy_regression":
        raise HTTPException(409, "安全验收失败的构建已隔离，请在修复后创建新构建")
    if build.recipe_hash != recipe_hash():
        raise HTTPException(409, "知识生成配方已改变，不能续用旧构建检查点")
    from src.core.project_brain.exceptions import ModelCallsIncomplete
    from src.core.project_brain.usage import assert_calls_terminal
    try:
        await session.run_sync(lambda sync: assert_calls_terminal(sync, build.id))
    except ModelCallsIncomplete:
        raise HTTPException(409, "存在未恢复的调用或响应不确定，不能通过阶段重试再次生成；请保留账目并创建新构建") from None
    project = await session.scalar(select(BrainProject).where(BrainProject.id == build.project_id).with_for_update())
    from src.models.brain import BrainRecovery
    dependency = await session.scalar(select(BrainRecovery.child_build_id).where(
        BrainRecovery.parent_build_id == build.id).limit(1))
    if dependency is not None:
        raise HTTPException(409, "该构建已成为恢复依赖，请新建恢复记录，不能原地改变父检查点")
    if not build.historical and (project.request_seq != build.request_seq or project.dirty_at or project.maintenance_state != "active"):
        raise HTTPException(409, "源码维护请求已推进，请发起最新构建或显式历史构建")
    for unit in await session.scalars(select(BrainUnit).where(BrainUnit.build_id == build.id, BrainUnit.status == "failed")):
        unit.status, unit.attempts, unit.dispatched_at, unit.lease_token, unit.lease_until = "pending", 0, None, None, None
    build.status, build.error_message = "pending", None
    await session.commit()
    await asyncio.to_thread(dispatch_pending, build.id)
    return {"build_id": build.id, "status": "pending"}


@router.post("/builds/{build_id}/pause", status_code=202, dependencies=[_WRITE])
async def pause_execution(project_id: int, build_id: str,
                          request: EmptyControlRequest = Body(default_factory=EmptyControlRequest),
                          session: AsyncSession = Depends(get_session)) -> dict:
    """Stop taking new units, then retain checkpoints after active units drain."""
    from src.core.project_brain.execution import ExecutionConflict, pause_build
    from src.core.project_brain.exceptions import ModelCallsIncomplete

    await _build(session, project_id, build_id)
    try:
        build = await session.run_sync(lambda sync: pause_build(sync, build_id))
        await session.commit()
    except (ExecutionConflict, ModelCallsIncomplete):
        await session.rollback()
        raise HTTPException(409, "当前构建无法安全暂停，请检查构建状态、租约和未确认调用") from None
    return _build_data(build)


@router.post("/builds/{build_id}/resume", status_code=202, dependencies=[_WRITE])
async def resume_execution(project_id: int, build_id: str,
                           request: EmptyControlRequest = Body(default_factory=EmptyControlRequest),
                           session: AsyncSession = Depends(get_session)) -> dict:
    """Resume this fixed build without creating a new build or resetting costs."""
    _enabled()
    from src.core.project_brain.execution import ExecutionConflict, resume_build
    from src.core.project_brain.exceptions import ModelCallsIncomplete

    observed = await _build(session, project_id, build_id)

    def transition(sync):
        # Observe admission under the same locks as pause and claim. A pause
        # committed after the scope read must still receive prompt dispatch
        # when this request resumes it; a duplicate resume stays idempotent.
        sync.scalar(select(BrainProject).where(BrainProject.id == observed.project_id)
                    .with_for_update().execution_options(populate_existing=True))
        current = sync.scalar(select(BrainBuild).where(BrainBuild.id == build_id)
                              .with_for_update().execution_options(populate_existing=True))
        was_paused = current is not None and current.execution_state == "paused"
        build = resume_build(sync, build_id)
        return build, was_paused and build.execution_state == "running"

    try:
        build, transitioned = await session.run_sync(transition)
        await session.commit()
    except (ExecutionConflict, ModelCallsIncomplete):
        await session.rollback()
        raise HTTPException(409, "当前构建无法安全继续，请等待暂停完成并核对配方、租约和未确认调用") from None
    if transitioned:
        try:
            await asyncio.to_thread(dispatch_pending, build.id)
        except Exception:
            # The durable running intent survives a broker outage. Beat can
            # dispatch the same preserved pending checkpoints later.
            pass
    return _build_data(build)


@router.get("/builds/{build_id}/recovery-plan", dependencies=[_READ])
async def recovery_plan(project_id: int, build_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """List candidates only; private authentication happens before any model I/O."""
    from src.core.project_brain.recovery import candidate_plan
    parent = await _build(session, project_id, build_id)
    return await session.run_sync(lambda sync: candidate_plan(sync, parent))


@router.post("/builds/{build_id}/recovery", status_code=202, dependencies=[_WRITE])
async def recover_build(project_id: int, build_id: str,
                        request: RecoveryRequest = Body(default_factory=RecoveryRequest),
                        session: AsyncSession = Depends(get_session)) -> dict:
    """Create a historical execution; retain all parent results and charges."""
    _enabled()
    from src.core.project_brain.recovery import create_recovery, RecoveryRejected
    await _build(session, project_id, build_id)
    try:
        child = await session.run_sync(lambda sync: create_recovery(sync, build_id))
        await session.commit()
    except RecoveryRejected:
        await session.rollback()
        raise HTTPException(409, "父构建尚在执行或已被安全隔离，不能继承检查点") from None
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "恢复请求并发冲突，请重试") from None
    try:
        await asyncio.to_thread(dispatch_pending, child.id)
    except Exception:
        # Registration is durable; Beat re-dispatches the same child later.
        pass
    return {"build_id": child.id, "parent_build_id": build_id, "status": child.status,
            "historical": True, "url": f"/api/v1/projects/{project_id}/brain/builds/{child.id}"}


@router.post("/builds/{build_id}/publication/retry", status_code=202, dependencies=[_WRITE])
async def retry_publication(project_id: int, build_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """单独重试发布，复用 ready 产物。"""
    _enabled()
    build = await _build(session, project_id, build_id)
    _available(build)
    project = await session.scalar(select(BrainProject).where(BrainProject.id == build.project_id).with_for_update())
    if (project.active_build_id != build.id or not project.gitlab_sync or project.dirty_at
            or project.maintenance_state != "active" or project.source_status == "missing"):
        raise HTTPException(409, "只有开启同步的当前版本可以发布")
    publication = await session.scalar(select(BrainPublication).where(BrainPublication.build_id == build.id).with_for_update())
    if publication and publication.status in {"publishing", "published"}:
        raise HTTPException(409, "该版本正在发布或已经发布")
    if publication:
        if publication.status == "failed":
            # 用户显式重试才允许为已关闭 MR 建立新 MR；恢复任务不会自动重新打开。
            publication.mr_iid, publication.mr_url = None, None
        publication.status, publication.error_message, publication.lease_token, publication.lease_until = "pending", None, None, None
        publication.error_code = None
    else:
        publication = BrainPublication(build_id=build.id, target_branch=project.branch)
        session.add(publication)
    await session.commit()
    await asyncio.to_thread(dispatch_publications)
    return {"build_id": build.id, "publication_id": publication.id, "status": publication.status}
