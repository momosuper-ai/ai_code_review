"""Authenticated compile checkpoint reuse across compatible commits."""

import re

from sqlalchemy import func, select

from src.core.project_brain.trusted_packet import checkpoint_digest
from src.models.brain import BrainBuild, BrainModelCall, BrainUnit


def _base_build(session, build, base_build_id):
    base = session.get(BrainBuild, base_build_id)
    if (base is None or base.status != "ready" or base.project_id != build.project_id
            or base.recipe_hash != build.recipe_hash
            or (base.coverage or {}).get("public_schema_version") != "2.0"):
        return None
    return base


def _parent_is_trusted(session, parent, seen=None):
    seen = set() if seen is None else seen
    if parent.id in seen or len(seen) >= 8:
        return False
    seen.add(parent.id)
    calls = session.scalars(select(BrainModelCall).where(
        BrainModelCall.build_id == parent.build_id, BrainModelCall.unit_id == parent.id)).all()
    if calls:
        from src.core.project_brain.recovery import _successful_unit_calls
        return _successful_unit_calls(session, parent)
    owner = session.get(BrainBuild, parent.build_id)
    diagnostic = owner.diagnostic if owner and isinstance(owner.diagnostic, dict) else {}
    imports = diagnostic.get("incremental_imports", {})
    marker = imports.get(str(parent.id)) if isinstance(imports, dict) else None
    if not marker:
        return False
    try:
        verify_incremental_dependencies(session, owner, _seen=seen)
    except IncrementalRejected:
        return False
    return True


class IncrementalRejected(ValueError):
    pass


def _requires_incremental_summary(build, imports, coverage):
    if imports or "incremental" in coverage:
        return True
    diagnostic = build.diagnostic if isinstance(build.diagnostic, dict) else {}
    if "generator_version" not in diagnostic:
        return False
    version = diagnostic["generator_version"]
    match = re.fullmatch(r"project-brain/(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", version) if isinstance(version, str) else None
    return match is None or tuple(map(int, match.groups())) >= (2, 4, 1)


async def incremental_compile_checkpoint(unit, build, project, token, store):
    if unit.kind != "compile" or not build.base_build_id:
        return None
    from src.core.project_brain.compiler import packet_hash, project_compilation_texts
    from src.core.project_brain.schema import Compilation
    from src.core.project_brain.snapshot import read_snapshot
    from src.core.project_brain.trusted_packet import VerifiedPacket
    from src.tasks import brain_task as worker

    with worker.SyncSession() as session:
        base = _base_build(session, build, build.base_build_id)
        if base is None:
            return None
        candidates = session.scalars(select(BrainUnit).where(
            BrainUnit.build_id == base.id, BrainUnit.kind == "compile",
            BrainUnit.status == "completed", BrainUnit.input_hash == unit.input_hash
        ).order_by(BrainUnit.id)).all()
        if not candidates:
            return None
        old = candidates[0]
        old_result = {key: value for key, value in (old.result or {}).items()
                      if key not in {"_recovery_import", "_incremental_import"}}
        old_result_hash = checkpoint_digest(old_result)
        if any(checkpoint_digest({key: value for key, value in (candidate.result or {}).items()
                                  if key not in {"_recovery_import", "_incremental_import"}}) != old_result_hash
               for candidate in candidates[1:]):
            return None
        if not _parent_is_trusted(session, old):
            return None

    snapshot = read_snapshot(store.directory(build.id, "snapshots"))
    catalog = worker._source_catalog(build, project, snapshot)
    bundle = worker._read_source_bundle(store, build, catalog)
    contexts = worker._contexts(build, bundle, catalog.snapshot)
    index = unit.payload["index"]
    if index >= len(contexts):
        return None
    context = contexts[index]
    if (context["module_id"] != unit.payload["module_id"]
            or [item["fact"]["id"] for item in context["items"]] != unit.payload["fact_ids"]
            or packet_hash(bundle, build.recipe_hash, context) != unit.input_hash):
        return None
    with worker.SyncSession() as session:
        final = session.scalar(select(BrainUnit).where(
            BrainUnit.build_id == build.id, BrainUnit.kind == "finalize",
            BrainUnit.status == "completed"))
        privacy_states = session.scalars(select(BrainUnit.status).where(
            BrainUnit.build_id == build.id, BrainUnit.kind == "privacy")).all()
        source_states = session.scalars(select(BrainUnit.status).where(
            BrainUnit.build_id == build.id, BrainUnit.kind == "source_privacy")).all()
        if (final is None or not (final.result or {}).get("bundle_mac") or not source_states
                or any(state != "completed" for state in source_states)):
            return None
        proof = VerifiedPacket.from_checkpoint(context, bundle.model_dump(),
            source_commit=build.source_commit, recipe_hash=build.recipe_hash,
            finalize=final.result or {}, finalize_state=final.status,
            privacy_states=privacy_states, planned_hash=unit.input_hash)
    proof.validate(context)
    compilation = Compilation.model_validate(old_result, strict=True)
    evidence_ids = {e["id"] for item in context["items"] for e in item["evidence"]}
    if any(not set(claim.evidence_ids) <= evidence_ids for claim in compilation.claims):
        return None
    guard = worker._guard(unit, token, new_mode=True, project_id=project.gitlab_project_id)
    try:
        worker._bind_source_identity(bundle, guard, catalog)
        safe = Compilation.model_validate(
            project_compilation_texts(compilation.model_dump(), guard.filter), strict=True)
        projected_matches = checkpoint_digest(safe.model_dump()) == old_result_hash
        guard.check_discoveries()
        if not projected_matches:
            return None
        return {**safe.model_dump(), "_incremental_import": {
            "base_build_id": base.id, "parent_unit_id": old.id,
            "parent_input_hash": old.input_hash, "result_hash": old_result_hash,
            "input_hash": unit.input_hash}}
    finally:
        await guard.close()


def accept_incremental(session, build, unit, result):
    if "_incremental_import" not in result:
        return result
    marker = result["_incremental_import"]
    safe = {key: value for key, value in result.items() if key != "_incremental_import"}
    if (type(marker) is not dict or set(marker) != {"base_build_id", "parent_unit_id",
            "parent_input_hash", "result_hash", "input_hash"}
            or type(marker["parent_unit_id"]) is not int
            or marker["input_hash"] != unit.input_hash
            or marker["result_hash"] != checkpoint_digest(safe)):
        raise IncrementalRejected("incremental_dependency_invalid")
    base = _base_build(session, build, marker["base_build_id"])
    parent = session.get(BrainUnit, marker["parent_unit_id"])
    if (base is None or build.base_build_id != base.id or parent is None
            or parent.build_id != base.id or parent.kind != "compile" or parent.status != "completed"
            or parent.input_hash != marker["parent_input_hash"]
            or parent.input_hash != unit.input_hash
            or checkpoint_digest(parent.result) != marker["result_hash"]
            or not _parent_is_trusted(session, parent)):
        raise IncrementalRejected("incremental_dependency_invalid")
    diagnostic = build.diagnostic if isinstance(build.diagnostic, dict) else {}
    imports = diagnostic.get("incremental_imports", {})
    if not isinstance(imports, dict):
        raise IncrementalRejected("incremental_dependency_invalid")
    build.diagnostic = {**diagnostic, "incremental_imports": {
        **imports, str(unit.id): dict(marker)}}
    return safe


def verify_incremental_dependencies(session, build, _seen=None):
    seen = set() if _seen is None else _seen
    if build.diagnostic is not None and not isinstance(build.diagnostic, dict):
        raise IncrementalRejected("incremental_dependency_invalid")
    diagnostic = build.diagnostic or {}
    imports = diagnostic.get("incremental_imports", {})
    if not isinstance(imports, dict):
        raise IncrementalRejected("incremental_dependency_invalid")
    coverage = build.coverage if isinstance(build.coverage, dict) else {}
    if build.status == "ready" and build.base_build_id and _requires_incremental_summary(build, imports, coverage):
        summary = coverage.get("incremental")
        compile_count = session.scalar(select(func.count(BrainUnit.id)).where(
            BrainUnit.build_id == build.id, BrainUnit.kind == "compile", BrainUnit.status == "completed"))
        if (type(summary) is not dict or set(summary) != {
                "base_build_id", "reused_compile_units", "compiled_units"}
                or summary["base_build_id"] != build.base_build_id
                or type(summary["reused_compile_units"]) is not int
                or type(summary["compiled_units"]) is not int
                or summary["reused_compile_units"] != len(imports)
                or summary["compiled_units"] < 0
                or summary["reused_compile_units"] + summary["compiled_units"] != compile_count):
            raise IncrementalRejected("incremental_dependency_invalid")
    for unit_key, marker in imports.items():
        if (not isinstance(unit_key, str) or not unit_key.isdecimal()
                or type(marker) is not dict or set(marker) != {"base_build_id", "parent_unit_id",
                    "parent_input_hash", "result_hash", "input_hash"}
                or type(marker["parent_unit_id"]) is not int
                or type(marker["base_build_id"]) is not str
                or any(type(marker[key]) is not str for key in ("parent_input_hash", "result_hash", "input_hash"))):
            raise IncrementalRejected("incremental_dependency_invalid")
        child = session.get(BrainUnit, int(unit_key))
        base = _base_build(session, build, marker["base_build_id"])
        parent = session.get(BrainUnit, marker["parent_unit_id"])
        if (child is None or child.build_id != build.id or child.kind != "compile"
                or child.status != "completed" or child.input_hash != marker["input_hash"]
                or parent is None or base is None or build.base_build_id != base.id
                or parent.build_id != base.id
                or parent.kind != "compile" or parent.status != "completed"
                or parent.input_hash != marker["parent_input_hash"]
                or parent.input_hash != child.input_hash
                or checkpoint_digest(child.result) != marker["result_hash"]
                or checkpoint_digest(parent.result) != marker["result_hash"]
                or not _parent_is_trusted(session, parent, set(seen))):
            raise IncrementalRejected("incremental_dependency_invalid")
