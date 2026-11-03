"""Cooperative, durable execution control independent of branch maintenance.

A requested pause stops admission of new units. Already leased work may finish
its model/output-privacy chain and commit a checkpoint. Only a fully drained,
accounted build becomes paused; uncertain responses keep their original charge.
The caller owns the transaction and commits all returned state transitions.
"""

from sqlalchemy import select

from src.config import settings
from src.core.project_brain.exceptions import ModelCallsIncomplete
from src.core.project_brain.usage import assert_calls_terminal
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainRecovery, BrainUnit, utcnow


class ExecutionConflict(ValueError):
    """A fixed operational reason, suitable for an API without source data."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _locked_build(session, build_id: str) -> tuple[BrainProject, BrainBuild]:
    observed = session.get(BrainBuild, build_id)
    if observed is None:
        raise ExecutionConflict("build_not_found")
    project = session.scalar(select(BrainProject).where(BrainProject.id == observed.project_id)
        .with_for_update().execution_options(populate_existing=True))
    build = session.scalar(select(BrainBuild).where(BrainBuild.id == build_id)
        .with_for_update().execution_options(populate_existing=True))
    if project is None or build is None:
        raise ExecutionConflict("build_not_found")
    return project, build


def _active(build: BrainBuild) -> bool:
    # Lifecycle hooks import this module too, so the common contract is loaded
    # only after both modules have finished their initialization.
    from src.core.project_brain.lifecycle import ACTIVE_STATES

    return build.status in ACTIVE_STATES


def _units(session, build_id: str) -> list[BrainUnit]:
    return session.scalars(select(BrainUnit).where(BrainUnit.build_id == build_id)
        .execution_options(populate_existing=True)).all()


def _counters(units: list[BrainUnit]) -> bool:
    """Reject uncertainty; report whether any genuine reservation remains."""
    live = False
    for unit in units:
        payload = unit.payload or {}
        reserved, unknown = payload.get("_reserved_tokens", 0), payload.get("_unknown_tokens", 0)
        if (type(reserved) is not int or reserved < 0 or type(unknown) is not int
                or unknown != 0):
            raise ModelCallsIncomplete()
        live = live or reserved > 0
    return live


def _accounted(session, build: BrainBuild, units: list[BrainUnit]) -> None:
    if _counters(units):
        raise ModelCallsIncomplete()
    assert_calls_terminal(session, build.id)


def finish_pause(session, build: BrainBuild) -> bool:
    """Finish a drain under the caller's project/build locks; never commit.

    Flush first so the final checkpoint/deferred lease is visible to the query.
    A running row must be fenced by the normal completion or lease-recovery
    path, even when its deadline has passed. A terminal outcome always wins.
    """
    if build.execution_state != "pausing":
        return False
    if not _active(build):
        build.execution_state = "running"
        return False
    session.flush()
    units = _units(session, build.id)
    live = _counters(units)
    unknown = session.scalar(select(BrainModelCall.id).where(
        BrainModelCall.build_id == build.id, BrainModelCall.state == "unknown").limit(1))
    if unknown is not None:
        raise ModelCallsIncomplete()
    now = utcnow()
    if any(unit.status == "running" or unit.lease_until is not None and unit.lease_until > now for unit in units):
        return False
    if live:
        raise ModelCallsIncomplete()
    _accounted(session, build, units)
    build.execution_state, build.paused_at = "paused", now
    return True


def pause_build(session, build_id: str) -> BrainBuild:
    """Persist admission stop, then acknowledge paused only after safe drain."""
    _, build = _locked_build(session, build_id)
    if not _active(build):
        raise ExecutionConflict("build_terminal")
    if build.execution_state not in {"running", "pausing", "paused"}:
        raise ExecutionConflict("invalid_execution_state")
    if build.execution_state == "paused":
        return build
    if build.execution_state == "running":
        build.execution_state, build.pause_requested_at, build.paused_at = "pausing", utcnow(), None
    for unit in session.scalars(select(BrainUnit).where(
            BrainUnit.build_id == build.id, BrainUnit.status == "pending").with_for_update()
            .execution_options(populate_existing=True)):
        unit.dispatched_at = None
    try:
        finish_pause(session, build)
    except ModelCallsIncomplete:
        # The stop-admission intent must survive even when existing uncertain
        # calls prevent a safe acknowledgement. The task/Beat failure path
        # retains the original unknown charges and closes the build normally.
        # Rolling this request back would admit unrelated work after "pause".
        pass
    return build


def _dependencies(session, build: BrainBuild) -> None:
    from src.core.project_brain.recovery import PROTOCOL, RecoveryRejected, _parent_ok, verify_dependencies

    if (build.coverage or {}).get("recovery_write_intents"):
        raise ExecutionConflict("recovery_dependency_invalid")
    protocol = (build.coverage or {}).get("recovery_protocol")
    if protocol is None:
        return
    if protocol != PROTOCOL:
        raise ExecutionConflict("recovery_dependency_invalid")
    record = session.get(BrainRecovery, build.id)
    if record is None:
        raise ExecutionConflict("recovery_dependency_invalid")
    try:
        if record.status in {"frozen", "completed"}:
            verify_dependencies(session, build)
        elif record.status == "pending":
            # A pause before scanning has no frozen private state yet. Resume
            # the same scan, which authenticates the complete chain before I/O.
            scan = session.scalar(select(BrainUnit).where(
                BrainUnit.build_id == build.id, BrainUnit.kind == "scan"))
            if scan is None or scan.status == "completed" or record.manifest_hash or record.imports or record.plan:
                raise ExecutionConflict("recovery_dependency_invalid")
            parent = session.get(BrainBuild, record.parent_build_id)
            _parent_ok(session, parent)
            if (parent.coverage or {}).get("recovery_protocol") == PROTOCOL:
                verify_dependencies(session, parent)
        else:
            raise ExecutionConflict("recovery_dependency_invalid")
    except RecoveryRejected:
        raise ExecutionConflict("recovery_dependency_invalid") from None


def resume_build(session, build_id: str) -> BrainBuild:
    """Resume this exact build without retrying, rebuilding or resetting bills."""
    from src.core.project_brain.lifecycle import recipe_hash

    project, build = _locked_build(session, build_id)
    if not _active(build):
        raise ExecutionConflict("build_terminal")
    if build.execution_state == "pausing":
        raise ExecutionConflict("pause_in_progress")
    if build.execution_state not in {"running", "paused"}:
        raise ExecutionConflict("invalid_execution_state")
    if build.recipe_hash != recipe_hash():
        raise ExecutionConflict("recipe_changed")
    if not build.historical and (build.request_seq != project.request_seq
            or project.maintenance_state != "active" or project.branch not in settings.main_branch_list
            or project.source_status == "missing"):
        raise ExecutionConflict("maintenance_stale")
    _dependencies(session, build)
    if build.execution_state == "running":
        return build
    session.flush()
    units = _units(session, build.id)
    if any(unit.status == "running" or unit.lease_until is not None and unit.lease_until > utcnow() for unit in units):
        raise ModelCallsIncomplete()
    _accounted(session, build, units)
    for unit in units:
        if unit.status == "pending":
            unit.dispatched_at = None
    build.execution_state, build.resumed_at = "running", utcnow()
    diagnostic = build.diagnostic if isinstance(build.diagnostic, dict) else {}
    breaker = diagnostic.get("breaker")
    if isinstance(breaker, dict) and breaker.get("state") == "tripped":
        build.diagnostic = {
            **diagnostic,
            "breaker": {**breaker, "events": [], "state": "closed"},
        }
    return build
