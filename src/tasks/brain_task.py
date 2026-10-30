"""Independent recoverable Project Brain stages and maintenance scheduling."""

import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import Text, cast, func, or_, select

from src.config import settings
from src.core.gitlab_client import GitLabClient
from src.core.project_brain import GENERATOR_VERSION
from src.core.project_brain import timing
from src.core.project_brain.artifacts import ArtifactStore, assemble_cards, validate
from src.core.project_brain.compiler import CompilationGuard, CompilationLimitError, compile_packet, encoded, packet_hash, packets, sanitize_structure
from src.core.project_brain.extractor import FactExtractor
from src.core.project_brain.exceptions import ModelCallsIncomplete, ModelTransportFailed, ModelWorkAdmissionDenied, PrivacyCheckpointMissing, PrivacyIdentityConflict, PrivacySealConflict, SourceBindingError, PrivacyResponseInvalid
from src.core.project_brain.execution import ExecutionConflict, finish_pause, pause_build
from src.core.project_brain.lifecycle import ACTIVE_STATES, BudgetBusy, BudgetExhausted, account_tokens, claim_unit, create_build, recipe_hash, reserve_tokens
from src.core.project_brain.publisher import branch_fingerprint
from src.core.project_brain.privacy import PrivacyLedger, PrivacyScanDeferred
from src.core.project_brain.privacy_history import HistoryCandidate, PrivacyHistory
from src.core.project_brain.privacy_identity import SourceIdentityGuard
from src.core.project_brain.privacy_projection import bundle_privacy_records, card_privacy_records, project_records
from src.core.project_brain.planning import compilation_plan
from src.core.project_brain.usage import BuildCallMeter, abandon_reservation, assert_calls_terminal
from src.core.project_brain.schema import BrainManifest, Compilation, FactBundle
from src.core.project_brain.snapshot import read_snapshot, run_git
from src.core.project_brain.source_catalog import SourceCatalog, identity_key
from src.core.project_brain.trusted_packet import VerifiedPacket, VerifiedSummaryPacket, checkpoint_digest
from src.core.project_brain.summary import SUMMARY_KINDS, hierarchy_for_assembly, next_summary_tasks, wrap_summary_result
from src.core.repo_manager import RepoManager
from src.core.privacy_filter import PrivacyFilter
from src.models import SyncSession
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainPublication, BrainUnit, utcnow
from src.tasks.celery_app import celery_app
from src.utils.logger import logger

_EXTRACTION_CACHE: tuple[tuple[str, str, str], FactBundle, int, bool] | None = None
_CONTEXT_CACHE: tuple[tuple[str, str, str, str], list[dict]] | None = None
_SOURCE_CACHE: tuple[tuple, SourceCatalog] | None = None


def _safe_failure_metadata(exc: Exception) -> dict:
    metadata = {"exception": type(exc).__name__}
    for name in ("failure_code", "reason", "phase", "after_response", "reservation_created",
                 "remaining_seconds", "required_seconds"):
        if hasattr(exc, name):
            value = getattr(exc, name)
            if value is None or type(value) in {str, int, bool}:
                metadata[name] = value
    return metadata


def _persist_timing(unit: BrainUnit) -> None:
    diagnostic = unit.diagnostic if isinstance(unit.diagnostic, dict) else {}
    previous = diagnostic.get("segments", {})
    current = timing.snapshot()
    segments = {}
    for name in timing.SEGMENT_NAMES:
        old = previous.get(name, {}) if isinstance(previous, dict) else {}
        old_seconds = old.get("seconds", 0) if isinstance(old, dict) else 0
        old_count = old.get("count", 0) if isinstance(old, dict) else 0
        if type(old_seconds) not in {int, float} or type(old_count) is not int:
            old_seconds, old_count = 0, 0
        segments[name] = {
            "seconds": round(float(old_seconds) + current[name]["seconds"], 3),
            "count": old_count + current[name]["count"],
        }
    unit.diagnostic = {**diagnostic, "segments": segments}


def _transient_failure_detail(exc: Exception) -> str:
    if isinstance(exc, ModelTransportFailed):
        if exc.failure_code == "model_http_retry_exhausted":
            return f"临时HTTP错误已达到重试次数上限，费用预留保留；failure_code={exc.failure_code}"
        return f"HTTP错误后剩余工作窗口不足以继续重试，费用预留保留；failure_code={exc.failure_code}"
    if isinstance(exc, ModelWorkAdmissionDenied):
        return f"本地模型工作窗口不足，未发送新请求，费用预留保留；failure_code={exc.failure_code}"
    return f"隐私响应经有界纠正仍未通过完整校验；failure_code={exc.failure_code} reason={exc.reason}"


def _record_breaker_event(session, build: BrainBuild, now: datetime) -> None:
    diagnostic = build.diagnostic if isinstance(build.diagnostic, dict) else {}
    prior = diagnostic.get("breaker", {})
    if not isinstance(prior, dict):
        prior = {}
    events = []
    cutoff = now - timedelta(seconds=settings.PROJECT_BRAIN_BREAKER_WINDOW_SECONDS)
    old_events = prior.get("events")
    for value in old_events if isinstance(old_events, list) else []:
        if type(value) is not str:
            continue
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
        if timestamp >= cutoff:
            events.append(value)
    trips = prior.get("trips", 0)
    if type(trips) is not int or trips < 0:
        trips = 0
    old_state = prior.get("state")
    state = old_state if type(old_state) is str and old_state in {"closed", "tripped"} else "closed"
    tripped_at = prior.get("tripped_at") if type(prior.get("tripped_at")) is str else None
    now_iso = now.replace(tzinfo=timezone.utc).isoformat()
    if state == "closed":
        events.append(now_iso)
    breaker = {"events": events, "trips": trips, "state": state, "tripped_at": tripped_at}
    if (state == "closed" and build.execution_state == "running"
            and len(events) >= settings.PROJECT_BRAIN_BREAKER_THRESHOLD):
        breaker = {"events": [], "trips": trips + 1, "state": "tripped", "tripped_at": now_iso}
    build.diagnostic = {**diagnostic, "breaker": breaker}
    if breaker["state"] == "tripped" and build.execution_state == "running":
        pause_build(session, build.id)


def _source_identity_create(build, *, create: bool) -> bool:
    """Read registration on the task thread, before offloading static work."""
    if not create:
        return False
    # A completed extraction anchors registration across builds. Losing the
    # whole directory cannot silently rotate an existing project's identity.
    with SyncSession() as session:
        owner = session.get(BrainProject, build.project_id)
        if owner is None:
            raise SourceBindingError()
        registered = session.scalar(select(BrainUnit.id).join(BrainBuild).join(
            BrainProject, BrainBuild.project_id == BrainProject.id).where(
            BrainProject.gitlab_project_id == owner.gitlab_project_id, BrainUnit.kind == "extract",
            BrainUnit.status == "completed",
            BrainUnit.result["source_identity_registered"].as_boolean().is_(True)).limit(1))
    return registered is None


def _source_catalog(build, project, snapshot, *, create: bool = False) -> SourceCatalog:
    """Reconstruct canonical observations in RAM; public graphs are not caches."""
    global _SOURCE_CACHE
    key = identity_key(ArtifactStore().work_base, project.gitlab_project_id, create=create)
    signature = (build.id, build.recipe_hash, build.source_commit, project.gitlab_project_id,
                 project.branch, hashlib.sha256(key).digest())
    if _SOURCE_CACHE is None or _SOURCE_CACHE[0] != signature:
        # A previous public projection cannot restore true imports or paths.
        # Static extraction is deterministic and cheap compared with model I/O.
        raw = FactExtractor(snapshot).extract()
        catalog = SourceCatalog(snapshot, raw, key, project.gitlab_project_id, project.branch)
        _SOURCE_CACHE = (signature, catalog)
    return _SOURCE_CACHE[1]


_source_catalog_impl = _source_catalog


def _timed_source_catalog(*args, **kwargs):
    if not timing.active():
        return _source_catalog_impl(*args, **kwargs)
    with timing.segment("source_catalog"):
        return _source_catalog_impl(*args, **kwargs)


_source_catalog = _timed_source_catalog
_catalog_bundle_impl = SourceCatalog.bundle


def _timed_catalog_bundle(catalog, *args, **kwargs):
    if not timing.active():
        return _catalog_bundle_impl(catalog, *args, **kwargs)
    with timing.segment("bundle"):
        return _catalog_bundle_impl(catalog, *args, **kwargs)


SourceCatalog.bundle = _timed_catalog_bundle
_extract_impl = FactExtractor.extract


def _timed_extract(extractor, *args, **kwargs):
    if not timing.active():
        return _extract_impl(extractor, *args, **kwargs)
    with timing.segment("bundle"):
        return _extract_impl(extractor, *args, **kwargs)


FactExtractor.extract = _timed_extract


def _read_source_bundle(store, build, catalog) -> FactBundle:
    bundle = FactBundle.model_validate(store.read_work(build.id, "facts.json"), strict=True)
    with SyncSession() as session:
        finalized = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id, BrainUnit.kind == "finalize"))
        proof = finalized.result if finalized and finalized.status == "completed" else None
    if (not proof or proof.get("bundle_hash") != checkpoint_digest(bundle.model_dump())
            or proof.get("source_commit") != build.source_commit or proof.get("recipe_hash") != build.recipe_hash):
        raise SourceBindingError()
    catalog.authenticate_checkpoint(bundle, proof.get("bundle_mac"))
    return bundle


def _contexts(build: BrainBuild, bundle: FactBundle, snapshot) -> list[dict]:
    # Raw fixed-SHA snippets remain in this prefork process only. The complete
    # safe bundle hash prevents stale metadata reuse after checkpoint changes.
    global _CONTEXT_CACHE
    key = (build.id, build.recipe_hash, snapshot.commit_sha, checkpoint_digest(bundle.model_dump()))
    if _CONTEXT_CACHE is None or _CONTEXT_CACHE[0] != key:
        _CONTEXT_CACHE = (key, packets(bundle, snapshot))
    return _CONTEXT_CACHE[1]


def dispatch_pending(build_id: str | None = None) -> None:
    """DB 提交之后尝试投递，Broker 故障由定期扫描补发。"""
    if not settings.PROJECT_BRAIN_ENABLED:
        return
    with SyncSession() as session:
        # Bound broker backlog as well as worker concurrency. A whole repository
        # can now have hundreds of units; re-enqueuing them every 120s is unsafe.
        threshold = utcnow() - timedelta(seconds=900)
        in_flight = session.scalar(select(func.count()).select_from(BrainUnit).join(BrainBuild).where(
            BrainBuild.status.in_(ACTIVE_STATES), or_(BrainUnit.status == "running",
                (BrainBuild.execution_state == "running") & (BrainUnit.status == "pending")
                & (BrainUnit.dispatched_at >= threshold))))
        capacity = max(0, settings.PROJECT_BRAIN_CONCURRENCY * 2 - in_flight)
        if not capacity:
            return
        query = select(BrainUnit.id, BrainBuild.project_id, BrainUnit.build_id).join(BrainBuild).where(
            BrainUnit.status == "pending", BrainBuild.status.in_(ACTIVE_STATES), BrainBuild.execution_state == "running")
        if build_id:
            query = query.where(BrainUnit.build_id == build_id)
        query = query.where(or_(BrainUnit.dispatched_at.is_(None), BrainUnit.dispatched_at < threshold))
        candidates = session.execute(query.order_by(BrainUnit.id).limit(capacity)).all()
        for unit_id, project_id, candidate_build_id in candidates:
            # Use the same lock order as claim/pause. A queued stale message is
            # harmless, but never enqueue past a committed pause request.
            project = session.scalar(select(BrainProject).where(BrainProject.id == project_id)
                                     .with_for_update(skip_locked=True))
            if project is None:
                session.rollback()
                continue
            build = session.scalar(select(BrainBuild).where(BrainBuild.id == candidate_build_id)
                                   .with_for_update().execution_options(populate_existing=True))
            unit = session.scalar(select(BrainUnit).where(BrainUnit.id == unit_id)
                                  .with_for_update().execution_options(populate_existing=True))
            if (build.status not in ACTIVE_STATES or build.execution_state != "running"
                    or unit.status != "pending" or unit.dispatched_at is not None and unit.dispatched_at >= threshold):
                session.rollback()
                continue
            try:
                execute_brain_unit.apply_async(args=[unit.id], queue="brain", retry=False)
                unit.dispatched_at = utcnow()
                session.commit()
            except Exception:
                session.rollback()
                logger.warning("知识任务投递失败，等待恢复：build_id=%s unit_id=%s", unit.build_id, unit.id)
                break


def _history_for(unit: BrainUnit, project_id: int) -> PrivacyHistory:
    """Resolve history only from the same GitLab project's private records."""
    store = ArtifactStore()
    candidates, required, unavailable, expected = [], [], 0, None
    with SyncSession() as session:
        build = session.get(BrainBuild, unit.build_id)
        owner = session.get(BrainProject, build.project_id)
        if owner.gitlab_project_id != project_id:
            raise SourceBindingError()
        create = unit.kind == "extract"
        if not create:
            extraction = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id,
                BrainUnit.kind == "extract", BrainUnit.status == "completed"))
            expected = (extraction.result or {}).get("privacy_history_digest") if extraction else None
            if not isinstance(expected, str):
                raise PrivacyCheckpointMissing()
        if create:
            previous = session.scalars(select(BrainBuild).join(BrainProject).where(
                BrainProject.gitlab_project_id == project_id, BrainBuild.id != build.id)
                .order_by(BrainBuild.created_at, BrainBuild.id)).all()
            for old in previous:
                extraction = session.scalar(select(BrainUnit).where(BrainUnit.build_id == old.id,
                    BrainUnit.kind == "extract", BrainUnit.status == "completed"))
                if extraction is None:
                    continue
                proof = (extraction.result or {}).get("privacy_history_digest")
                if proof is not None:
                    required.append((old.id, proof))
                audit = store.work_base / "source-audits" / old.id
                work = store.directory(old.id, "work")
                schema2 = (old.coverage or {}).get("public_schema_version") == "2.0"
                sealed_audit = audit.exists() or audit.is_symlink() or (old.status == "ready" and schema2)
                directory = audit if sealed_audit else work
                if not (directory / "privacy/key").is_file():
                    if sealed_audit or proof is not None or schema2:
                        raise PrivacyCheckpointMissing()
                    unavailable += 1
                    continue
                if directory == audit and not (directory / "privacy/fingerprints.json").is_file():
                    raise PrivacyCheckpointMissing()
                candidates.append(HistoryCandidate(project_id, old.id, old.source_commit, directory,
                    sealed_audit=sealed_audit,
                    require_index=sealed_audit or proof is not None or schema2))
    history = PrivacyHistory(store.work_base, project_id, build.id, build.source_commit,
        identity_key(store.work_base, project_id, create=False), create=create,
        candidates=candidates, required=required)
    if expected is not None and expected != history.digest:
        raise PrivacyCheckpointMissing()
    history.legacy_unavailable = unavailable
    return history


def _guard_base(unit: BrainUnit, token: str, *, new_mode: bool = False, project_id: int | None = None) -> CompilationGuard:
    def account(usage, estimated: bool, bound: int) -> None:
        account_tokens(unit.build_id, usage, estimated, bound, unit_id=unit.id, lease_token=token)
        logger.info("知识模型用量：build_id=%s unit_id=%s prompt=%s completion=%s cached=%s estimated=%s",
                    unit.build_id, unit.id, usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens, estimated)
    directory = ArtifactStore().directory(unit.build_id, "work")
    require_existing = unit.kind in {"privacy", "finalize", "source_privacy", "compile", "assemble"} | SUMMARY_KINDS
    # Only extract establishes this build's key. Later units never recreate a
    # lost volume/key and mistake it for a valid empty discovery index.
    options = {"seal_directory": ArtifactStore().work_base / "source-seals" / unit.build_id} if new_mode else {}
    ledger = PrivacyLedger(directory, create=not require_existing, require_index=new_mode, **options)
    history = None
    if new_mode:
        if type(project_id) is not int or project_id <= 0:
            raise SourceBindingError()
        history = _history_for(unit, project_id)
    guard = CompilationGuard(account, lambda amount: reserve_tokens(unit.build_id, token, unit.id, amount), ledger,
                            call_meter=BuildCallMeter(unit.build_id, unit.id, token),
                            discovery_context={"build_id": unit.build_id, "unit_id": unit.id, "stage": unit.kind})
    if new_mode:
        guard.privacy_history = history
        guard.persist_rule_findings = True
    return guard


def _guard(unit: BrainUnit, token: str, *, new_mode: bool = False, project_id: int | None = None) -> CompilationGuard:
    guard = _guard_base(unit, token, new_mode=new_mode, project_id=project_id)
    if new_mode:
        from src.core.project_brain.recovery import bind_unit_guard, track_parent_discoveries
        guard = track_parent_discoveries(guard, unit.build_id)
        guard = bind_unit_guard(guard, unit.build_id)
    return guard


def _bind_source_identity(bundle: FactBundle, guard, catalog: SourceCatalog | None = None) -> None:
    """Attach a per-unit pre-I/O gate without requiring a specific guard type."""
    if bundle.source_identity is not None:
        if catalog is None:
            raise ValueError("source_identity_catalog_missing")
        catalog.authenticate(bundle)
        guard.discovery_check = lambda: catalog.authenticate(bundle)
        guard.packet_projection = catalog.project_packet
        guard.source_catalog = catalog
        return
    identity = SourceIdentityGuard(bundle, guard.filter)
    identity.check()
    guard.discovery_check = identity.check


def privacy_targets(bundle: FactBundle) -> list[dict]:
    """Recompute unique complete records; persist only deterministic ranges."""
    return bundle_privacy_records(bundle)


def _source_privacy_targets(build, bundle: FactBundle, snapshot) -> list[dict]:
    """Classify every eventual excerpt before persisting a safe fact snapshot."""
    records = privacy_targets(bundle)
    excerpts = {entry["excerpt"] for packet in _contexts(build, bundle, snapshot)
                for item in packet["items"] for entry in item["evidence"]}
    complete = {(record["field"], record["text"]): record for record in records}
    for excerpt in excerpts:
        complete[("source_excerpt", excerpt)] = {"field": "source_excerpt", "text": excerpt}
    return [complete[key] for key in sorted(complete)]


def _assert_calls_complete(session, build_id: str, assembler_id: int) -> None:
    """A lost/old response may still discover private content after its lease.

    Check both call attribution and retained unit reservations. In particular,
    finishing a later retry does not make an earlier unknown call disappear.
    The completion caller holds the project/build lock used by reservations.
    """
    assert_calls_terminal(session, build_id)
    from src.core.project_brain.recovery import verify_dependencies, load_recovery
    recovery_build = session.get(BrainBuild, build_id)
    if (recovery_build.coverage or {}).get("recovery_write_intents"):
        raise ModelCallsIncomplete()
    if (recovery_build.coverage or {}).get("recovery_protocol"):
        verify_dependencies(session, recovery_build)
        load_recovery(session, recovery_build, session.get(BrainProject, recovery_build.project_id), ArtifactStore())
    for unit in session.scalars(select(BrainUnit).where(BrainUnit.build_id == build_id)):
        if unit.id != assembler_id and unit.status != "completed":
            raise ModelCallsIncomplete()
        for field in ("_reserved_tokens", "_unknown_tokens"):
            value = (unit.payload or {}).get(field, 0)
            if type(value) is not int or value != 0:
                raise ModelCallsIncomplete()


def _assert_privacy_calls_settled(session, build_id: str, ledger: PrivacyLedger) -> None:
    """Only an actual classified result can be the origin of a finding."""
    calls = {call.id: call for call in session.scalars(select(BrainModelCall).where(
        BrainModelCall.build_id == build_id))}
    for call_id, origin in ledger.origins["calls"].items():
        call = calls.get(call_id)
        if (call is None or call.build_id != build_id or call.role != "privacy" or call.state != "settled"
                or origin["context"] != {"build_id": build_id, "unit_id": call.unit_id, "stage": call.stage}):
            raise ModelCallsIncomplete()


def _model_usage_snapshot(session, build_id: str, assembler_id: int) -> dict:
    _assert_calls_complete(session, build_id, assembler_id)
    build = session.get(BrainBuild, build_id)
    calls = session.scalars(select(BrainModelCall).where(BrainModelCall.build_id == build_id)).all()
    closed = [call for call in calls if call.state == "http_error"]
    data = {"model_calls_settled": sum(call.state == "settled" for call in calls),
            "model_calls_closed_http_errors": len(closed),
            "model_usage_unknown_bound_tokens": sum(call.reserved_tokens for call in closed),
            "model_usage_settled_tokens": build.total_prompt_tokens + build.total_completion_tokens,
            "model_usage_budget_occupied_tokens": build.reserved_tokens,
            "model_usage_financially_exact": not closed and not build.usage_estimated}
    from src.core.project_brain.recovery import recovery_coverage
    data.update(recovery_coverage(session, build_id))
    return data


async def _safe_bundle(bundle: FactBundle, guard: CompilationGuard) -> FactBundle:
    await guard.clean(encoded(project_records(privacy_targets(bundle))))
    return _finish_safe_bundle(bundle, guard)


def _finish_safe_bundle(bundle: FactBundle, guard: CompilationGuard) -> FactBundle:
    # Refresh discoveries committed by all completed scan units before final use.
    if guard.ledger:
        guard.ledger.refresh()
    discovery_check = getattr(guard, "discovery_check", None)
    if callable(discovery_check):
        discovery_check()
    if bundle.source_identity is not None:
        catalog = getattr(guard, "source_catalog", None)
        if not isinstance(catalog, SourceCatalog):
            raise ValueError("source_identity_catalog_missing")
        return catalog.safe_bundle(bundle, guard.filter)
    # 事实 ID、哈希、证据引用是结构数据；名称、签名和提取信息则需扫描。
    for ref in bundle.source_refs:
        if guard.filter.sanitize_text(ref.path) != ref.path:
            raise ValueError("源码路径包含敏感值，不能生成可安全引用的知识")
    # 后续本地扫描可能发现新模式；路径必须保持原样才能生成真实证据链接。
    # 包括二进制等没有 SourceRef 的覆盖文件，不能在清单中泄露敏感路径。
    paths = set(bundle.files) | set(bundle.modules.values())
    paths.update(ref.path for ref in bundle.source_refs)
    paths.update(bundle.observations)
    if any(guard.filter.sanitize_text(path) != path for path in paths):
        raise ValueError("源码路径包含敏感值，不能生成可安全引用的知识")
    # Include unknown adapter fields/keys and frameworks in the final pass.
    # The original graph identifiers and source metadata must stay invariant.
    safe = FactBundle.model_validate(sanitize_structure(bundle.model_dump(), guard.filter))
    if (safe.source_commit != bundle.source_commit or safe.source_fingerprint != bundle.source_fingerprint
            or safe.modules != bundle.modules or set(safe.files) != set(bundle.files)
            or [r.model_dump() for r in safe.source_refs] != [r.model_dump() for r in bundle.source_refs]):
        raise ValueError("隐私过滤改变了固定源码或证据结构")
    fact_fields = ("id", "kind", "module_id", "source_ref_ids", "extractor")
    relation_fields = ("id", "from_id", "type", "to_id", "resolution", "knowledge_type", "source_ref_ids", "candidates")
    if (any(any(getattr(before, key) != getattr(after, key) for key in fact_fields)
            for before, after in zip(bundle.facts, safe.facts, strict=True))
            or any(any(getattr(before, key) != getattr(after, key) for key in relation_fields)
                   for before, after in zip(bundle.relations, safe.relations, strict=True))):
        raise ValueError("隐私过滤改变了事实或关系标识")
    return safe


async def _execute(unit: BrainUnit, build: BrainBuild, project: BrainProject, token: str) -> dict:
    if build.recipe_hash != recipe_hash():
        raise ValueError("知识生成配方已改变，请发起新构建，避免混用旧检查点")
    store = ArtifactStore()
    if unit.kind == "scan":
        gitlab = GitLabClient(purpose="read")
        info = await gitlab.get_project(project.gitlab_project_id)
        snapshot = await RepoManager().ensure_snapshot(project.gitlab_project_id, info["path_with_namespace"], project.branch,
                                                       build.source_commit, str(store.directory(build.id, "snapshots")))
        return {"fingerprint": snapshot.fingerprint, "project_url": info["web_url"], "project_path": info["path_with_namespace"]}
    snapshot = read_snapshot(store.directory(build.id, "snapshots"))
    if snapshot.commit_sha != build.source_commit:
        raise ValueError("知识快照与构建源码版本不一致")
    if unit.kind == "planning":
        # The paid child has already committed. Rebuilding/authenticating the
        # complete graph runs under this unit's lease, outside project locks.
        # This unit is a synchronous graph calculation in its own Celery
        # process. Keep it on the task thread so the 600-second soft limit can
        # interrupt it directly, without an executor retaining graph work.
        return _plan_frontier(build, snapshot)
    new_mode = (getattr(build, "coverage", None) or {}).get("public_schema_version") == "2.0"
    catalog = None
    if new_mode:
        create_identity = _source_identity_create(build, create=unit.kind == "extract")
        catalog = await asyncio.to_thread(_source_catalog, build, project, snapshot, create=create_identity)
        snapshot = catalog.snapshot
    guard = _guard(unit, token, new_mode=True, project_id=project.gitlab_project_id) if new_mode else _guard(unit, token)
    try:
        if unit.kind in {"extract", "privacy", "finalize"}:
            if catalog is not None:
                bundle = catalog.bundle()
                reused_files, incremental = 0, False
            else:
                global _EXTRACTION_CACHE
                cache_key = (build.id, build.recipe_hash, build.source_commit)
                if _EXTRACTION_CACHE is None or _EXTRACTION_CACHE[0] != cache_key:
                    extractor = FactExtractor(snapshot)
                    raw = await asyncio.to_thread(extractor.extract)
                    _EXTRACTION_CACHE = (cache_key, raw, extractor.reused_files, False)
                _, raw, reused_files, incremental = _EXTRACTION_CACHE
                bundle = raw.model_copy(deep=True) if unit.kind == "finalize" else raw
            if unit.kind == "extract":
                _bind_source_identity(bundle, guard, catalog)
                targets = (_source_privacy_targets(build, bundle, snapshot) if catalog is not None else privacy_targets(bundle))
                return {"privacy_batches": [{"start": i, "end": min(len(targets), i + 128)}
                                             for i in range(0, max(1, len(targets)), 128)],
                        "reused_files": reused_files, "incremental": incremental,
                        **({"source_identity_registered": True, "privacy_index_required": True,
                            "privacy_history_digest": guard.privacy_history.digest,
                            "privacy_history_parents": len(guard.privacy_history.parents),
                            "privacy_history_legacy_unavailable": guard.privacy_history.legacy_unavailable}
                           if catalog is not None else {})}
            if unit.kind == "privacy":
                _bind_source_identity(bundle, guard, catalog)
                targets = (_source_privacy_targets(build, bundle, snapshot) if catalog is not None else privacy_targets(bundle))
                await guard.clean(encoded(project_records(targets[unit.payload["start"]:unit.payload["end"]])))
                return {"calls": guard.calls, "cache_hits": guard.cache_hits}
            with SyncSession() as session:
                privacy_states = session.scalars(select(BrainUnit.status).where(
                    BrainUnit.build_id == build.id, BrainUnit.kind == "privacy")).all()
            if not privacy_states or any(state != "completed" for state in privacy_states):
                raise ValueError("知识隐私检查点尚未完成")
            _bind_source_identity(bundle, guard, catalog)
            # Scan units classified every unique record. Finalize replays their
            # cumulative discoveries without another model classification pass.
            bundle = _finish_safe_bundle(bundle, guard)
            # 在编译前验证事实，避免模型看到悬空关系。
            validate(bundle, [], snapshot)
            store.write_work(build.id, "facts.json", bundle.model_dump())
            contexts = _contexts(build, bundle, snapshot)
            return {"packets": [{"module_id": p["module_id"], "index": i,
                                  "fact_ids": [item["fact"]["id"] for item in p["items"]],
                                  "packet_digest": checkpoint_digest(p),
                                  "hash": packet_hash(bundle, build.recipe_hash, p)}
                                 for i, p in enumerate(contexts)], "reused_files": reused_files,
                    "incremental": incremental, "bundle_hash": checkpoint_digest(bundle.model_dump()),
                    **({"bundle_mac": catalog.checkpoint_mac(bundle)} if catalog is not None else {}),
                    "source_commit": build.source_commit, "recipe_hash": build.recipe_hash,
                    "cost_plan": compilation_plan(contexts)}
        bundle = (_read_source_bundle(store, build, catalog) if catalog is not None else
                  FactBundle.model_validate(store.read_work(build.id, "facts.json")))
        if unit.kind in {"source_privacy", "compile"}:
            validate(bundle, [], snapshot)
            contexts = _contexts(build, bundle, snapshot)
            index = unit.payload["index"]
            if index >= len(contexts):
                raise ValueError("知识包分区与检查点不一致")
            context = contexts[index]
            if context["module_id"] != unit.payload["module_id"] or [i["fact"]["id"] for i in context["items"]] != unit.payload["fact_ids"]:
                raise ValueError("知识包声明与检查点不一致")
            digest = packet_hash(bundle, build.recipe_hash, context)
            if digest != unit.input_hash:
                raise ValueError("知识包与检查点输入不一致")
            _bind_source_identity(bundle, guard, catalog)
            if unit.kind == "source_privacy":
                excerpts = sorted({entry["excerpt"] for item in context["items"] for entry in item["evidence"]})
                await guard.clean(encoded({"source_excerpt": excerpts}))
                return {"calls": guard.calls, "cache_hits": guard.cache_hits}
            with SyncSession() as session:
                finalized = session.scalar(select(BrainUnit).where(
                    BrainUnit.build_id == build.id, BrainUnit.kind == "finalize"))
                privacy_states = session.scalars(select(BrainUnit.status).where(
                    BrainUnit.build_id == build.id, BrainUnit.kind == "privacy")).all()
                if finalized is None:
                    raise ValueError("知识编译缺少已完成的隐私检查点")
                source_units = session.scalars(select(BrainUnit).where(
                    BrainUnit.build_id == build.id, BrainUnit.kind == "source_privacy")).all()
                if catalog is not None and (not source_units or any(u.status != "completed" for u in source_units)):
                    raise ValueError("source_privacy_checkpoint_incomplete")
                proof = VerifiedPacket.from_checkpoint(context, bundle.model_dump(),
                    source_commit=build.source_commit, recipe_hash=build.recipe_hash,
                    finalize=finalized.result or {}, finalize_state=finalized.status,
                    privacy_states=privacy_states, planned_hash=unit.input_hash)
            result = await compile_packet(context, guard, proof=proof)
            return result.model_dump()
        if unit.kind in SUMMARY_KINDS:
            with SyncSession() as session:
                inputs = session.scalars(select(BrainUnit).where(
                    BrainUnit.build_id == build.id).order_by(BrainUnit.id)).all()
            # Reconstruct exact children and partitions before scanning or paid
            # generation. Persisted payloads alone do not authenticate sources.
            context, proof = VerifiedSummaryPacket.from_checkpoints(bundle, unit, inputs,
                source_commit=build.source_commit, recipe_hash=build.recipe_hash)
            validate(bundle, [], snapshot)
            _bind_source_identity(bundle, guard, catalog)
            result = await compile_packet(context, guard, summary_proof=proof)
            # Compilation has already passed complete input/output privacy and
            # source-binding checks. The envelope is immutable machine identity;
            # inherited unknowns are reconstructed from authenticated children,
            # never persisted/redacted as a second, divergent result copy.
            return wrap_summary_result(context, result)
        if unit.kind == "assemble":
            with SyncSession() as session:
                if catalog is not None:
                    _assert_calls_complete(session, build.id, unit.id)
                # Completion/update order must not select a different first
                # document path or reorder module/project conclusions.
                units = session.scalars(select(BrainUnit).where(
                    BrainUnit.build_id == build.id).order_by(BrainUnit.id)).all()
                scan = next(u for u in units if u.kind == "scan")
                extract = next(u for u in units if u.kind == "extract")
                finalized = next(u for u in units if u.kind == "finalize")
                compiled = [(u.payload["module_id"], Compilation.model_validate(u.result)) for u in units if u.kind == "compile"]
            hierarchy = hierarchy_for_assembly(bundle, units)
            _bind_source_identity(bundle, guard, catalog)
            cards = assemble_cards(bundle, compiled, [], hierarchy=hierarchy)
            # 所有最终结论和未知项再次扫描；不反脱敏。
            for card in cards:
                await guard.clean(encoded(project_records(card_privacy_records(card))))
            await guard.clean(encoded({"publication_metadata": {"branch": project.branch,
                                                               "project_url": scan.result["project_url"]}}))
            # A later card can identify a value occurring in an earlier card or
            # fact checkpoint. Reapply the complete build index before rendering.
            bundle = _finish_safe_bundle(bundle, guard)
            # Public projection redacts card prose and owned destinations while
            # retaining the exact source IDs/scope bindings. Never generic-mask
            # the service identifiers in an authenticated card.
            coverage = validate(bundle, cards, snapshot)
            coverage.update(reused_files=extract.result["reused_files"], incremental=extract.result["incremental"])
            if catalog is not None:
                coverage["public_schema_version"] = "2.0"
                with SyncSession() as session:
                    coverage.update(_model_usage_snapshot(session, build.id, unit.id))
                    _assert_privacy_calls_settled(session, build.id, guard.ledger)
            coverage["cache_hits"] = finalized.result.get("cache_hits", 0)
            if catalog is not None:
                coverage.update({key: extract.result.get(key, 0) for key in (
                    "privacy_history_parents", "privacy_history_legacy_unavailable")})
                coverage["privacy_history_digest"] = extract.result["privacy_history_digest"]
                coverage["privacy_policy_findings"] = sum(bool(f.get("policy_records"))
                    for f in guard.ledger.origins["findings"].values())
            coverage["cost_plan"] = finalized.result.get("cost_plan", {})
            coverage["summary_hierarchy"] = {**hierarchy["coverage"],
                "summary_nodes": sum(u.kind in SUMMARY_KINDS for u in units),
                "details_preserved": True, "semantic_support_status": "unreviewed"}
            manifest = BrainManifest(generator=GENERATOR_VERSION, build_id=build.id, project_id=project.gitlab_project_id,
                                     branch=project.branch, source_commit=build.source_commit, brain_version=build.brain_version,
                                     recipe_hash=build.recipe_hash, source_fingerprint=bundle.source_fingerprint,
                                     files={}, coverage=coverage, issues=bundle.issues)
            if catalog is not None:
                from src.core.project_brain.public_projection import project_public
                public_bundle, public_cards, public_manifest = project_public(
                    bundle, cards, manifest, scan.result["project_url"], guard.filter, source_paths=catalog.paths)
                privacy_seal = guard.ledger.seal(public_manifest.projection.privacy_seal,
                                               public_manifest.projection.content_digest)
                guard.ledger.retain_audit(store.work_base / "source-audits" / build.id)
                path = await asyncio.to_thread(store.finalize, public_manifest, public_bundle, public_cards,
                                              scan.result["project_url"])
            else:
                cards = [type(card).model_validate(sanitize_structure(card.model_dump(serialize_as_any=True), guard.filter))
                         for card in cards]
                path = await asyncio.to_thread(store.finalize, manifest, bundle, cards, scan.result["project_url"])
            manifest_text = store.repository(build.id)["docs/project-brain/manifest.json"]
            coverage["manifest_sha256"] = hashlib.sha256(manifest_text.encode()).hexdigest()
            with (path / "knowledge.zip").open("rb") as archive:
                coverage["export_sha256"] = hashlib.file_digest(archive, "sha256").hexdigest()
            return {"artifact_path": str(path), "coverage": coverage, "fingerprint": bundle.source_fingerprint,
                    **({"privacy_seal": privacy_seal} if catalog is not None else {})}
        raise ValueError("未知知识构建阶段")
    finally:
        await guard.close()


def _dependency_signature(session, build_id: str) -> str:
    """Identify a complete DB snapshot without replanning under write locks.

    PostgreSQL hashes JSON on the server, so the completion transaction reads
    only compact numeric/state/hash rows. This is a concurrency check; full
    canonical graph authentication still happens in the planning execution.
    """
    query = select(BrainUnit).where(BrainUnit.build_id == build_id,
                                   BrainUnit.kind != "planning").order_by(BrainUnit.id)
    if session.get_bind().dialect.name == "postgresql":
        def json_hash(column):
            return func.encode(func.sha256(func.convert_to(cast(column, Text), "UTF8")), "hex")
        records = session.execute(query.with_only_columns(
            BrainUnit.id, BrainUnit.key, BrainUnit.kind, BrainUnit.status, BrainUnit.input_hash,
            json_hash(BrainUnit.payload), json_hash(BrainUnit.result))).all()
        values = [list(row) for row in records]
    else:
        # SQLite is used only by existing offline unit tests. Production and
        # concurrency acceptance both use the server-side PostgreSQL branch.
        values = [[row.id, row.key, row.kind, row.status, row.input_hash,
                   checkpoint_digest(row.payload), checkpoint_digest(row.result)]
                  for row in session.scalars(query)]
    return checkpoint_digest(values)


def _plan_frontier(build: BrainBuild, snapshot) -> dict:
    """Read one consistent frontier, close the transaction, then authenticate."""
    with SyncSession() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        inputs = session.scalars(select(BrainUnit).where(
            BrainUnit.build_id == build.id).order_by(BrainUnit.id)).all()
        project = session.get(BrainProject, build.project_id)
        dependency_signature = _dependency_signature(session, build.id)
    # No database locks or connections are held during file/hash/graph work.
    bundle = FactBundle.model_validate(ArtifactStore().read_work(build.id, "facts.json"))
    if bundle.source_identity is not None:
        catalog = _source_catalog(build, project, snapshot)
        bundle = _read_source_bundle(ArtifactStore(), build, catalog)
        snapshot = catalog.snapshot
    finalized = [unit for unit in inputs if unit.kind == "finalize"]
    privacy = [unit for unit in inputs if unit.kind == "privacy"]
    compilations = [unit for unit in inputs if unit.kind == "compile"]
    summaries = [unit for unit in inputs if unit.kind in SUMMARY_KINDS]
    if (len(finalized) != 1 or finalized[0].status != "completed" or not privacy
            or any(unit.status != "completed" for unit in privacy)
            or not compilations or any(unit.status != "completed" for unit in compilations)
            or any(unit.status != "completed" for unit in summaries)
            or (finalized[0].result or {}).get("bundle_hash") != checkpoint_digest(bundle.model_dump())
            or (finalized[0].result or {}).get("source_commit") != build.source_commit
            or (finalized[0].result or {}).get("recipe_hash") != build.recipe_hash):
        raise ValueError("知识规划缺少完整且匹配的事实隐私检查点")
    planned = (finalized[0].result or {}).get("packets")
    if not isinstance(planned, list) or len(planned) != len(compilations):
        raise ValueError("知识规划编译依赖与最终事实检查点不匹配")
    expected = {f"compile:{packet['index']}": packet for packet in planned}
    if len(expected) != len(planned) or set(expected) != {unit.key for unit in compilations}:
        raise ValueError("知识规划编译依赖与最终事实检查点不匹配")
    for unit in compilations:
        packet = expected[unit.key]
        payload = dict(unit.payload)
        for field in ("_reserved_tokens", "_unknown_tokens"):
            value = payload.pop(field, 0)
            if type(value) is not int or value < 0:
                raise ValueError("知识规划编译依赖与最终事实检查点不匹配")
        if (payload != {"index": packet["index"], "module_id": packet["module_id"], "fact_ids": packet["fact_ids"]}
                or unit.input_hash != packet["hash"]):
            raise ValueError("知识规划编译依赖与最终事实检查点不匹配")
    validate(bundle, [], snapshot)
    tasks = next_summary_tasks(bundle, inputs)
    if not tasks:
        # A missing/incomplete root is a failed planning unit, never a partial
        # assembly shortcut. Its child checkpoints remain completed.
        hierarchy_for_assembly(bundle, inputs)
    return {"dependency_signature": dependency_signature,
            "tasks": [{"key": task.key, "kind": task.kind, "payload": task.payload,
                       "input_hash": task.input_hash} for task in tasks],
            "assemble_ready": not bool(tasks)}


def _advance(session, build: BrainBuild, completed_unit_id: int) -> None:
    """Use a cheap global frontier barrier to enqueue exactly one planner."""
    units = session.execute(select(BrainUnit.id, BrainUnit.kind, BrainUnit.status, BrainUnit.key).where(
        BrainUnit.build_id == build.id).order_by(BrainUnit.id)).all()
    privacy = [unit for unit in units if unit.kind == "privacy"]
    if privacy and all(unit.status == "completed" for unit in privacy) and not any(unit.kind == "finalize" for unit in units):
        session.add(BrainUnit(build_id=build.id, key="finalize", kind="finalize"))
        return
    source_privacy = [unit for unit in units if unit.kind == "source_privacy"]
    if (source_privacy and all(unit.status == "completed" for unit in source_privacy)
            and not any(unit.kind == "compile" for unit in units)):
        finalized = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id, BrainUnit.kind == "finalize"))
        if finalized is None or finalized.status != "completed" or not finalized.result.get("bundle_mac"):
            raise ValueError("source_privacy_checkpoint_incomplete")
        for packet in finalized.result["packets"]:
            session.add(BrainUnit(build_id=build.id, key=f"compile:{packet['index']}", kind="compile",
                input_hash=packet["hash"], payload={"index": packet["index"], "module_id": packet["module_id"], "fact_ids": packet["fact_ids"]}))
        return
    compilations = [unit for unit in units if unit.kind == "compile"]
    summaries = [unit for unit in units if unit.kind in SUMMARY_KINDS]
    if (not compilations or any(unit.status != "completed" for unit in compilations)
            or any(unit.status != "completed" for unit in summaries)
            or any(unit.kind == "assemble" for unit in units)
            or any(unit.kind == "planning" and unit.status != "completed" for unit in units)):
        return
    key = f"planning:{completed_unit_id}"
    if not any(unit.key == key for unit in units):
        payload = {"trigger_unit_id": completed_unit_id}
        session.add(BrainUnit(build_id=build.id, key=key, kind="planning", payload=payload,
                              input_hash=checkpoint_digest([build.recipe_hash, build.source_commit, payload])))


def _apply_plan(session, build: BrainBuild, result: dict) -> None:
    """Atomically apply an authenticated frontier while holding build locks."""
    if (set(result) != {"dependency_signature", "tasks", "assemble_ready"}
            or result["dependency_signature"] != _dependency_signature(session, build.id)
            or type(result["assemble_ready"]) is not bool or not isinstance(result["tasks"], list)
            or bool(result["tasks"]) == result["assemble_ready"]):
        raise ValueError("知识规划依赖已改变或规划结果契约无效")
    keys = set()
    for task in result["tasks"]:
        if (not isinstance(task, dict) or set(task) != {"key", "kind", "payload", "input_hash"}
                or task["kind"] not in SUMMARY_KINDS or type(task["key"]) is not str
                or not task["key"].startswith(f"{task['kind']}:") or task["key"] in keys
                or not isinstance(task["payload"], dict)
                or task["input_hash"] != checkpoint_digest(task["payload"])):
            raise ValueError("知识规划下一阶段契约无效")
        keys.add(task["key"])
        session.add(BrainUnit(build_id=build.id, key=task["key"], kind=task["kind"],
                              payload=task["payload"], input_hash=task["input_hash"]))
    if result["assemble_ready"]:
        session.add(BrainUnit(build_id=build.id, key="assemble", kind="assemble"))


def _abandon_live_reservation(unit: BrainUnit, session=None) -> None:
    # A missing response is still charged to build.reserved_tokens. Only its
    # classification changes: it cannot be mistaken for a settling live call.
    abandon_reservation(unit, session)


def _cancel_unfinished(session, build_id: str) -> None:
    for other in session.scalars(select(BrainUnit).where(BrainUnit.build_id == build_id,
            BrainUnit.status.in_(["pending", "running"])).with_for_update()):
        _abandon_live_reservation(other, session)
        other.status, other.lease_token, other.lease_until, other.dispatched_at = "failed", None, None, None
        other.error_message = "同一知识构建已终止，请重试失败阶段"


def _finish_execution_control(session, build: BrainBuild) -> None:
    """Drain a requested pause; uncertain calls still terminate the build."""
    try:
        finish_pause(session, build)
    except ModelCallsIncomplete:
        build.status, build.completed_at, build.execution_state = "failed", utcnow(), "running"
        build.error_message = "暂停排空发现未确认调用，检查点及费用保留；failure_code=model_calls_incomplete"
        _cancel_unfinished(session, build.id)


def _complete(unit_id: int, token: str, result: dict) -> bool:
    with SyncSession() as session:
        original = session.get(BrainUnit, unit_id)
        observed_build = session.get(BrainBuild, original.build_id)
        project = session.scalar(select(BrainProject).where(BrainProject.id == observed_build.project_id).with_for_update())
        build = session.scalar(select(BrainBuild).where(BrainBuild.id == original.build_id).with_for_update().execution_options(populate_existing=True))
        unit = session.scalar(select(BrainUnit).where(BrainUnit.id == unit_id).with_for_update().execution_options(populate_existing=True))
        if (unit.lease_token != token or unit.status != "running" or build.status not in ACTIVE_STATES
                or build.execution_state not in {"running", "pausing"}):
            return False
        if not build.historical and (build.request_seq != project.request_seq or project.maintenance_state != "active"
                                     or project.branch not in settings.main_branch_list or project.source_status == "missing"):
            build.status = "superseded"
            with timing.segment("checkpoint_commit"):
                session.flush()
            _persist_timing(unit)
            _finish_execution_control(session, build)
            session.commit()
            return False
        from src.core.project_brain.recovery import accept_import
        inherited = "_recovery_import" in result
        result = accept_import(session, build, unit, result)
        if inherited:
            final = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id, BrainUnit.kind == "finalize"))
            final.result = {**final.result, "cache_hits": final.result.get("cache_hits", 0) + 1}
        unit.result, unit.status, unit.lease_until = result, "completed", None
        if unit.kind == "scan":
            build.source_fingerprint = result["fingerprint"]
            session.add(BrainUnit(build_id=build.id, key="extract", kind="extract"))
        elif unit.kind == "extract":
            for index, payload in enumerate(result["privacy_batches"]):
                session.add(BrainUnit(build_id=build.id, key=f"privacy:{index}", kind="privacy", payload=payload,
                                      input_hash=hashlib.sha256(encoded([build.recipe_hash, build.source_commit, payload]).encode()).hexdigest()))
        elif unit.kind == "finalize":
            build.coverage = {**(build.coverage or {}), "cost_plan": result.get("cost_plan", {})}
            if result.get("bundle_mac"):
                for packet in result["packets"]:
                    session.add(BrainUnit(build_id=build.id, key=f"source_privacy:{packet['index']}", kind="source_privacy",
                        input_hash=packet["hash"], payload={"index": packet["index"], "module_id": packet["module_id"], "fact_ids": packet["fact_ids"]}))
                unit.result = {**result, "cache_hits": 0}
                with timing.segment("checkpoint_commit"):
                    session.flush()
                _persist_timing(unit)
                _finish_execution_control(session, build)
                session.commit()
                return True
            cached = []
            if build.base_build_id:
                cached = session.scalars(select(BrainUnit).where(BrainUnit.build_id == build.base_build_id,
                                                               BrainUnit.kind == "compile", BrainUnit.status == "completed")).all()
            cache = {u.input_hash: u.result for u in cached}
            cache_hits = 0
            for packet in result["packets"]:
                hit = cache.get(packet["hash"])
                cache_hits += bool(hit)
                session.add(BrainUnit(build_id=build.id, key=f"compile:{packet['index']}", kind="compile",
                                      input_hash=packet["hash"], payload={"index": packet["index"], "module_id": packet["module_id"], "fact_ids": packet["fact_ids"]},
                                      result=hit, status="completed" if hit else "pending"))
            unit.result = {**result, "cache_hits": cache_hits}
        elif unit.kind == "planning":
            _apply_plan(session, build, result)
        elif unit.kind == "assemble":
            if (build.coverage or {}).get("public_schema_version") == "2.0":
                _assert_calls_complete(session, build.id, unit.id)
                sealed = result.get("privacy_seal")
                if not isinstance(sealed, dict) or set(sealed) != {"version", "revision", "content_digest", "binding_hmac"}:
                    raise PrivacySealConflict()
                ledger = PrivacyLedger(ArtifactStore().directory(build.id, "work"), create=False,
                    seal_directory=ArtifactStore().work_base / "source-seals" / build.id)
                _assert_privacy_calls_settled(session, build.id, ledger)
                if "model_usage_budget_occupied_tokens" in result["coverage"]:
                    snapshot = _model_usage_snapshot(session, build.id, unit.id)
                    if any(result["coverage"].get(key) != value for key, value in snapshot.items()):
                        raise ModelCallsIncomplete()
                if ledger.seal(sealed["revision"], sealed["content_digest"]) != sealed:
                    raise PrivacySealConflict()
            original_coverage = build.coverage or {}
            build.artifact_path, build.coverage = result["artifact_path"], {**result["coverage"],
                **{key: original_coverage[key] for key in ("component_recipes", "recovery_protocol") if key in original_coverage}}
            if original_coverage.get("recovery_protocol"):
                from src.models.brain import BrainRecovery
                recovery = session.get(BrainRecovery, build.id)
                recovery.status, recovery.completed_at = "completed", utcnow()
            build.status, build.completed_at = "ready", utcnow()
            if not build.historical and build.request_seq == project.request_seq:
                project.active_build_id = build.id
                if not project.dirty_at:
                    project.observed_fingerprint = result["fingerprint"]
                if project.gitlab_sync:
                    session.add(BrainPublication(build_id=build.id, target_branch=project.branch))
        with timing.segment("checkpoint_commit"):
            session.flush()
        _persist_timing(unit)
        if unit.kind in {"privacy", "finalize", "source_privacy", "compile"} | SUMMARY_KINDS:
            _advance(session, build, unit.id)
        _finish_execution_control(session, build)
        session.commit()
        return True


@celery_app.task(bind=True, name="src.tasks.brain_task.execute_brain_unit", soft_time_limit=600,
                 time_limit=660, max_retries=2, default_retry_delay=60, acks_late=True, reject_on_worker_lost=True)
def execute_brain_unit(self, unit_id: int) -> dict:
    """执行独立阶段，检查点和失败记录均以租约令牌防护。"""
    if not settings.PROJECT_BRAIN_ENABLED:
        return {"status": "disabled"}
    timing.start()
    claim_started = utcnow()
    claimed = claim_unit(unit_id)
    if claimed is None:
        return {"status": "skipped"}
    unit, build, project, token = claimed
    if unit.dispatched_at is not None:
        timing.record("claim", (claim_started - unit.dispatched_at).total_seconds())
    started = time.monotonic()
    logger.info("知识阶段开始：project_id=%s build_id=%s sha=%s stage=%s", project.gitlab_project_id, build.id, build.source_commit, unit.kind)
    try:
        from src.core.project_brain.recovery import execute_recoverable
        with timing.segment("execute_total"):
            result = asyncio.run(execute_recoverable(_execute, unit, build, project, token))
        completed = _complete(unit.id, token, result)
    except (PrivacyScanDeferred, BudgetBusy) as exc:
        # Normal continuation, not a failed attempt. Durable receipts skip all
        # completed calls; the same lease cannot publish after this handoff.
        with SyncSession() as session:
            session.scalar(select(BrainProject).where(BrainProject.id == project.id).with_for_update())
            failed = session.scalar(select(BrainBuild).where(BrainBuild.id == build.id).with_for_update())
            current = session.scalar(select(BrainUnit).where(BrainUnit.id == unit_id).with_for_update())
            if current.lease_token != token or failed.status not in ACTIVE_STATES:
                return {"status": "lease_lost"}
            _abandon_live_reservation(current, session)
            current.status, current.lease_until, current.lease_token = "pending", None, None
            current.dispatched_at = utcnow() if isinstance(exc, BudgetBusy) else None
            current.attempts = max(0, current.attempts - 1)
            _persist_timing(current)
            _finish_execution_control(session, failed)
            can_dispatch = failed.status in ACTIVE_STATES and failed.execution_state == "running"
            if not can_dispatch:
                current.dispatched_at = None
            session.commit()
        logger.info("知识单元检查点续跑：build_id=%s unit_id=%s stage=%s reason=%s",
                    build.id, unit.id, unit.kind, type(exc).__name__)
        if not can_dispatch:
            if failed.status not in ACTIVE_STATES:
                return {"status": "failed"}
            return {"status": "paused" if failed.execution_state == "paused" else "deferred"}
        if isinstance(exc, BudgetBusy):
            try:
                execute_brain_unit.apply_async(args=[unit.id], countdown=5, queue="brain", retry=False)
            except Exception:
                # The DB checkpoint remains authoritative during Broker outage.
                with SyncSession() as session:
                    current = session.get(BrainUnit, unit.id)
                    if current.status == "pending" and current.lease_token is None:
                        current.dispatched_at = None
                        session.commit()
                logger.warning("知识预算等待任务投递失败，等待定期恢复：build_id=%s unit_id=%s", build.id, unit.id)
        else:
            dispatch_pending(build.id)
        return {"status": "deferred"}
    except Exception as exc:
        if isinstance(exc, PrivacySealConflict):
            # A valid late discovery can arrive after its old lease was fenced
            # or after ready. The immutable artifact cannot silently be edited;
            # close its availability and preserve all settled/unknown charges.
            with SyncSession() as session:
                owner = session.scalar(select(BrainProject).where(BrainProject.id == project.id).with_for_update())
                affected = session.scalar(select(BrainBuild).where(BrainBuild.id == build.id).with_for_update())
                if affected.status in ACTIVE_STATES | {"ready"}:
                    current = session.scalar(select(BrainUnit).where(BrainUnit.id == unit_id).with_for_update())
                    if current is not None and current.lease_token == token:
                        _persist_timing(current)
                    affected.status, affected.completed_at = "failed", utcnow()
                    affected.execution_state = "running"
                    affected.error_message = "隐私封存冲突；failure_code=privacy_seal_conflict"
                    if owner.active_build_id == affected.id:
                        owner.active_build_id = None
                    for publication in session.scalars(select(BrainPublication).where(
                            BrainPublication.build_id == affected.id).with_for_update()):
                        publication.status, publication.lease_token, publication.lease_until = "failed", None, None
                        publication.error_code = "privacy_seal_conflict"
                        publication.error_message = "隐私封存冲突，产物已隔离"
                    _cancel_unfinished(session, affected.id)
                    session.commit()
            logger.warning("知识隐私封存冲突：build_id=%s failure_code=privacy_seal_conflict", build.id)
            return {"status": "failed"}
        with SyncSession() as session:
            session.scalar(select(BrainProject).where(BrainProject.id == project.id).with_for_update())
            failed = session.scalar(select(BrainBuild).where(BrainBuild.id == build.id).with_for_update())
            current = session.scalar(select(BrainUnit).where(BrainUnit.id == unit_id).with_for_update())
            if current.lease_token != token:
                return {"status": "lease_lost"}
            if failed.status not in ACTIVE_STATES:
                return {"status": "superseded"}
            _abandon_live_reservation(current, session)
            # A failed await cannot supply this call's response later through
            # the retry. Preserve its charge and stop before billing unrelated
            # work which the final complete-call gate cannot publish. A real
            # late response can still settle its original UUID independently.
            unresolved = session.scalar(select(BrainModelCall.id).where(
                BrainModelCall.build_id == build.id, BrainModelCall.unit_id == unit_id,
                BrainModelCall.lease_token == token, BrainModelCall.state == "unknown").limit(1))
            unknown_guard = unresolved is not None
            original = _safe_failure_metadata(exc)
            diagnostic = current.diagnostic if isinstance(current.diagnostic, dict) else {}
            current.diagnostic = {
                **diagnostic,
                "last_failure": {
                **original,
                    "unknown_guard": unknown_guard,
                    "attempt": current.attempts,
                    "at": utcnow().replace(tzinfo=timezone.utc).isoformat(),
                },
            }
            if unresolved is not None:
                exc = ModelCallsIncomplete()
            transient = isinstance(exc, (ModelTransportFailed, PrivacyResponseInvalid, ModelWorkAdmissionDenied))
            diagnostic = current.diagnostic if isinstance(current.diagnostic, dict) else {}
            transient_retries = diagnostic.get("transient_retries", 0)
            if type(transient_retries) is not int or transient_retries < 0:
                transient_retries = settings.PROJECT_BRAIN_UNIT_TRANSIENT_RETRIES
            if (transient and not unknown_guard
                    and transient_retries < settings.PROJECT_BRAIN_UNIT_TRANSIENT_RETRIES
                    and failed.status in ACTIVE_STATES):
                retry_number = transient_retries + 1
                current.diagnostic = {**(current.diagnostic or {}), "transient_retries": retry_number}
                current.status, current.lease_until, current.lease_token = "pending", None, None
                current.attempts = max(0, current.attempts - 1)
                current.dispatched_at = utcnow()
                detail = _transient_failure_detail(exc)
                current.error_message = f"{type(exc).__name__}：{detail}"
                _record_breaker_event(session, failed, utcnow())
                _finish_execution_control(session, failed)
                can_dispatch = failed.status in ACTIVE_STATES and failed.execution_state == "running"
                if not can_dispatch:
                    current.dispatched_at = None
                countdown = min(900, settings.PROJECT_BRAIN_TRANSIENT_BACKOFF_SECONDS * 2 ** (retry_number - 1))
                _persist_timing(current)
                session.commit()
                if can_dispatch:
                    try:
                        execute_brain_unit.apply_async(args=[unit_id], countdown=countdown,
                                                       queue="brain", retry=False)
                    except Exception as broker_error:
                        with SyncSession() as session:
                            current = session.scalar(select(BrainUnit).where(
                                BrainUnit.id == unit_id).with_for_update())
                            if current.status == "pending" and current.lease_token is None:
                                current.dispatched_at = None
                                session.commit()
                        logger.warning("知识瞬时重试投递失败，等待定期恢复：build_id=%s unit_id=%s error_type=%s",
                                       build.id, unit_id, type(broker_error).__name__)
                return {"status": "deferred" if failed.status in ACTIVE_STATES else "failed"}
            terminal = isinstance(exc, (BudgetExhausted, CompilationLimitError, PrivacyIdentityConflict,
                                       PrivacyCheckpointMissing, SourceBindingError, ModelCallsIncomplete, ModelTransportFailed,
                                        ModelWorkAdmissionDenied, PrivacyResponseInvalid)) or current.attempts >= 3
            current.status, current.lease_until, current.lease_token = "failed" if terminal else "pending", None, None
            # self.retry owns the delayed delivery. Beat must not immediately
            # re-enqueue the same pending row during its countdown.
            current.dispatched_at = None if terminal else utcnow()
            # 不写供应商响应/上下文/秘密到日志或 DB。
            detail = ("知识构建 Token 预算耗尽" if isinstance(exc, BudgetExhausted)
                      else f"源码身份与隐私过滤冲突，构建已停止；failure_code={PrivacyIdentityConflict.failure_code}" if isinstance(exc, PrivacyIdentityConflict)
                      else f"知识隐私检查点缺失或损坏，构建已停止；failure_code={PrivacyCheckpointMissing.failure_code}" if isinstance(exc, PrivacyCheckpointMissing)
                      else f"源码来源认证失败；failure_code={SourceBindingError.failure_code}" if isinstance(exc, SourceBindingError)
                      else f"知识构建仍有在途或未知调用，请先核对费用与响应；failure_code={ModelCallsIncomplete.failure_code}" if isinstance(exc, ModelCallsIncomplete)
                      else _transient_failure_detail(exc) if isinstance(
                          exc, (ModelTransportFailed, ModelWorkAdmissionDenied, PrivacyResponseInvalid))
                      else f"概览输出未满足有界字段或字节预算；failure_code={exc.failure_code}" if isinstance(exc, CompilationLimitError) and exc.failure_code == "summary_output_capacity"
                      else f"知识单元超出完整字段、上下文或输出硬上限，请检查容量配置；failure_code={exc.failure_code}" if isinstance(exc, CompilationLimitError)
                      else PrivacyFilter().sanitize_text(str(exc))[:300] if type(exc) is ValueError
                      else "请检查模型连接、预算和源码覆盖")
            current.error_message = f"{type(exc).__name__}：{detail}"
            if terminal:
                failed.status, failed.error_message = "failed", current.error_message
                failed.completed_at = utcnow()
                _cancel_unfinished(session, build.id)
            _finish_execution_control(session, failed)
            terminal = terminal or failed.status not in ACTIVE_STATES
            can_retry = not terminal and failed.execution_state == "running"
            if not can_retry:
                current.dispatched_at = None
            _persist_timing(current)
            session.commit()
        logger.warning("知识阶段失败：build_id=%s stage=%s error_type=%s", build.id, unit.kind, type(exc).__name__)
        if isinstance(exc, PrivacyIdentityConflict):
            logger.warning("知识源码隐私身份冲突：failure_code=%s counts=%s",
                           PrivacyIdentityConflict.failure_code, exc.counts)
        if can_retry:
            raise self.retry(exc=RuntimeError("知识阶段暂时失败"))
        return {"status": "failed" if terminal else "paused" if failed.execution_state == "paused" else "deferred"}
    dispatch_pending(build.id)
    if completed and unit.kind == "assemble":
        store = ArtifactStore()
        store.remove(store.directory(build.id, "snapshots"))
        store.remove(store.directory(build.id, "work"))
    logger.info("知识阶段完成：project_id=%s build_id=%s sha=%s unit_id=%s stage=%s completed=%s duration_seconds=%.2f",
                project.gitlab_project_id, build.id, build.source_commit, unit.id, unit.kind, completed, time.monotonic() - started)
    return {"status": "completed" if completed else "lease_lost"}


async def _schedule_project(project: BrainProject) -> None:
    client = GitLabClient(purpose="read")
    try:
        info = await client.get_branch(project.gitlab_project_id, project.branch)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        from src.core.project_brain.lifecycle import mark_dirty

        with SyncSession() as session:
            current = session.scalar(select(BrainProject).where(BrainProject.id == project.id).with_for_update())
            if current.event_seq == project.event_seq and current.request_seq == project.request_seq and current.maintenance_state == "active":
                mark_dirty(session, project.gitlab_project_id, project.branch, deleted=True)
                session.commit()
        return
    sha = info["commit"]["id"]
    fingerprint = await branch_fingerprint(client, project.gitlab_project_id, sha)
    with SyncSession() as session:
        prior = session.scalar(select(BrainBuild).where(BrainBuild.project_id == project.id, BrainBuild.status == "ready",
            BrainBuild.historical.is_(False), BrainBuild.artifact_path.is_not(None), BrainBuild.recipe_hash == recipe_hash(),
            BrainBuild.source_fingerprint == fingerprint).order_by(BrainBuild.brain_version.desc()).limit(1))
        session.expunge_all()
    reusable_id = None
    if prior:
        try:
            base = await client.brain_request("GET", f"/projects/{project.gitlab_project_id}/repository/merge_base",
                                              params=[("refs[]", prior.source_commit), ("refs[]", sha)])
            if base.get("id") == prior.source_commit:
                ArtifactStore().repository(prior.id)
                reusable_id = prior.id
        except (httpx.HTTPStatusError, OSError, ValueError):
            pass  # 旧产物/祖先不可用时执行全量扫描。
    with SyncSession() as session:
        create_build(session, project.gitlab_project_id, project.branch, project.project_name, sha,
                     observed_seq=project.event_seq, fingerprint=fingerprint, reusable_id=reusable_id,
                     observed_request_seq=project.request_seq)
        session.commit()


@celery_app.task(name="src.tasks.brain_task.recover_brain", soft_time_limit=600, max_retries=2)
def recover_brain() -> dict:
    """Beat 恢复持久化请求、丢失租约及发布，按分支保留受引用版本。"""
    if not settings.PROJECT_BRAIN_ENABLED:
        return {"status": "disabled"}
    with SyncSession() as session:
        now = utcnow()
        projects = session.scalars(select(BrainProject).where(BrainProject.maintenance_state == "active",
                                                              BrainProject.branch.in_(settings.main_branch_list))).all()
        due = [p for p in projects if (p.dirty_at and (
                   p.dirty_at <= now - timedelta(seconds=settings.PROJECT_BRAIN_DEBOUNCE_SECONDS)
                   or p.dirty_since <= now - timedelta(seconds=300)))
               or (not p.dirty_at and (not p.last_checked_at or
                   p.last_checked_at <= now - timedelta(seconds=settings.PROJECT_BRAIN_RECONCILE_SECONDS)))]
        expired = session.scalars(select(BrainUnit.id).where(BrainUnit.status == "running", BrainUnit.lease_until < now)).all()
        for unit_id in expired:
            observed = session.get(BrainUnit, unit_id)
            observed_build = session.get(BrainBuild, observed.build_id)
            session.scalar(select(BrainProject).where(BrainProject.id == observed_build.project_id).with_for_update())
            build = session.scalar(select(BrainBuild).where(BrainBuild.id == observed.build_id).with_for_update().execution_options(populate_existing=True))
            unit = session.scalar(select(BrainUnit).where(BrainUnit.id == unit_id).with_for_update().execution_options(populate_existing=True))
            if unit.status != "running" or not unit.lease_until or unit.lease_until >= now:
                session.commit()
                continue
            if build.status not in ACTIVE_STATES:
                session.commit()
                continue
            _abandon_live_reservation(unit, session)
            unresolved = session.scalar(select(BrainModelCall.id).where(
                BrainModelCall.build_id == build.id, BrainModelCall.unit_id == unit.id,
                BrainModelCall.state == "unknown").limit(1))
            incomplete = unresolved is not None or (unit.payload or {}).get("_unknown_tokens", 0) != 0
            unit.status = "failed" if incomplete or unit.attempts >= 3 else "pending"
            unit.lease_token, unit.lease_until, unit.dispatched_at = None, None, None
            if unit.status == "failed":
                build.status = "failed"
                build.error_message = ("知识任务租约丢失且调用未确认，费用保留；failure_code=model_calls_incomplete"
                                       if incomplete else "知识任务多次失去租约，请手动重试失败阶段")
                build.completed_at = utcnow()
                _cancel_unfinished(session, build.id)
            _finish_execution_control(session, build)
            session.commit()
        pausing = session.scalars(select(BrainBuild.id).where(BrainBuild.execution_state == "pausing")).all()
        for paused_id in pausing:
            observed = session.get(BrainBuild, paused_id)
            session.scalar(select(BrainProject).where(BrainProject.id == observed.project_id).with_for_update())
            build = session.scalar(select(BrainBuild).where(BrainBuild.id == paused_id).with_for_update()
                                   .execution_options(populate_existing=True))
            _finish_execution_control(session, build)
            session.commit()
        paused_ids = session.scalars(select(BrainBuild.id).where(
            BrainBuild.status.in_(ACTIVE_STATES), BrainBuild.execution_state == "paused",
            BrainBuild.paused_at.is_not(None))).all()
        from src.core.project_brain.execution import resume_build
        for paused_id in paused_ids:
            observed = session.get(BrainBuild, paused_id)
            diagnostic = observed.diagnostic if isinstance(observed.diagnostic, dict) else {}
            breaker = diagnostic.get("breaker")
            if (not isinstance(breaker, dict) or breaker.get("state") != "tripped"
                    or observed.paused_at > now - timedelta(seconds=settings.PROJECT_BRAIN_BREAKER_COOLDOWN_SECONDS)):
                continue
            session.scalar(select(BrainProject).where(
                BrainProject.id == observed.project_id).with_for_update())
            build = session.scalar(select(BrainBuild).where(
                BrainBuild.id == paused_id).with_for_update().execution_options(populate_existing=True))
            diagnostic = build.diagnostic if isinstance(build.diagnostic, dict) else {}
            breaker = diagnostic.get("breaker")
            if (build.status not in ACTIVE_STATES or build.execution_state != "paused"
                    or not isinstance(breaker, dict) or breaker.get("state") != "tripped"
                    or build.paused_at is None
                    or build.paused_at > now - timedelta(seconds=settings.PROJECT_BRAIN_BREAKER_COOLDOWN_SECONDS)):
                session.commit()
                continue
            trips = breaker.get("trips", 0)
            if type(trips) is not int or trips < 0:
                trips = settings.PROJECT_BRAIN_BREAKER_MAX_TRIPS + 1
            if trips > settings.PROJECT_BRAIN_BREAKER_MAX_TRIPS:
                build.status, build.completed_at = "failed", now
                build.error_message = "上游模型服务持续异常，熔断次数已达上限；failure_code=upstream_unhealthy"
                _cancel_unfinished(session, build.id)
                session.commit()
                continue
            try:
                resume_build(session, build.id)
            except (ExecutionConflict, ModelCallsIncomplete) as exc:
                session.rollback()
                reason = getattr(exc, "reason", ModelCallsIncomplete.failure_code)
                logger.warning("知识熔断自动恢复暂不可用：build_id=%s reason=%s", paused_id, reason)
                continue
            session.commit()
        session.commit()
        session.expunge_all()
    for project in due:
        try:
            asyncio.run(_schedule_project(project))
        except Exception as exc:
            logger.warning("知识更新调度失败：project_id=%s error_type=%s", project.gitlab_project_id, type(exc).__name__)
    dispatch_pending()
    from src.tasks.brain_publication_task import dispatch_publications

    dispatch_publications()
    _retention()
    return {"status": "recovered", "projects": len(due)}


def _retention() -> None:
    store = ArtifactStore()
    with SyncSession() as session:
        for project in session.scalars(select(BrainProject).with_for_update(skip_locked=True)):
            builds = session.scalars(select(BrainBuild).where(BrainBuild.project_id == project.id, BrainBuild.status == "ready")
                                     .order_by(BrainBuild.brain_version.desc())).all()
            retained = {b.id for b in builds[:settings.PROJECT_BRAIN_RETENTION_BUILDS]}
            retained.add(project.active_build_id)
            retained.update(session.scalars(select(BrainBuild.base_build_id).where(BrainBuild.project_id == project.id, BrainBuild.status.in_(ACTIVE_STATES))))
            retained.update(session.scalars(select(BrainPublication.build_id).where(BrainPublication.status.in_(["pending", "publishing", "mr_open", "auto_merge_pending"]))))
            for build in builds:
                # DB 已提交但 Worker 在清理前退出时，定期移除临时源码。
                store.remove(store.directory(build.id, "snapshots"))
                store.remove(store.directory(build.id, "work"))
                if build.id in retained or not build.artifact_path:
                    continue
                store.remove(store.directory(build.id))
                build.artifact_path = None
                build.coverage = {**build.coverage, "artifacts_expired": True}
        session.commit()
