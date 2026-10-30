"""A pause drains leased work without changing checkpoints or accounting."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.core.project_brain import lifecycle
from src.core.project_brain.exceptions import ModelCallsIncomplete
from src.core.project_brain.execution import ExecutionConflict, finish_pause, pause_build, resume_build
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainRecovery, BrainUnit, utcnow
from tests.unit.test_project_brain import database  # noqa: F401


@pytest.fixture
def trial(database):
    with database() as session:
        build = lifecycle.create_build(session, 1, "main", "synthetic/execution", "a" * 40, historical=True)
        session.flush()
        unit = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        unit.dispatched_at = utcnow()
        session.commit()
        return build.id, unit.id


def _pause(database, build_id):
    with database() as session:
        build = pause_build(session, build_id)
        session.commit()
        return build


def _resume(database, build_id):
    with database() as session:
        build = resume_build(session, build_id)
        session.commit()
        return build


def _ledger(session, build_id):
    build = session.get(BrainBuild, build_id)
    return deepcopy({"build": {key: getattr(build, key) for key in (
        "source_commit", "recipe_hash", "request_seq", "brain_version", "generation", "historical",
        "coverage", "reserved_tokens", "total_prompt_tokens", "total_completion_tokens", "total_cached_tokens")},
        "units": [{column.name: getattr(unit, column.name) for column in BrainUnit.__table__.columns
                   if column.name != "dispatched_at"} for unit in session.scalars(
                       select(BrainUnit).where(BrainUnit.build_id == build_id).order_by(BrainUnit.id))],
        "calls": [{column.name: getattr(call, column.name) for column in BrainModelCall.__table__.columns}
                  for call in session.scalars(select(BrainModelCall).where(BrainModelCall.build_id == build_id))]})


def test_idle_pause_is_durable_and_resume_keeps_the_same_execution(database, trial):
    build_id, unit_id = trial
    with database() as session:
        before = _ledger(session, build_id)
        project_before = {column.name: getattr(session.get(BrainProject, session.get(BrainBuild, build_id).project_id), column.name)
                          for column in BrainProject.__table__.columns}
    paused = _pause(database, build_id)
    assert paused.execution_state == "paused" and paused.status == "pending"
    assert paused.pause_requested_at is not None and paused.paused_at >= paused.pause_requested_at
    with database() as session:
        assert session.get(BrainUnit, unit_id).dispatched_at is None
        assert _ledger(session, build_id) == before
        project = session.get(BrainProject, paused.project_id)
        assert {column.name: getattr(project, column.name) for column in BrainProject.__table__.columns} == project_before
    resumed = _resume(database, build_id)
    assert resumed.id == build_id and resumed.execution_state == "running"
    assert resumed.resumed_at >= paused.paused_at
    with database() as session:
        assert _ledger(session, build_id) == before


def test_pause_and_resume_are_idempotent_without_resetting_timestamps(database, trial):
    build_id, _ = trial
    first = _pause(database, build_id)
    again = _pause(database, build_id)
    assert (first.pause_requested_at, first.paused_at) == (again.pause_requested_at, again.paused_at)
    first = _resume(database, build_id)
    again = _resume(database, build_id)
    assert first.resumed_at == again.resumed_at


def test_pause_drains_every_lease_and_flushes_the_final_checkpoint(database, trial):
    build_id, first_id = trial
    with database() as session:
        second = BrainUnit(build_id=build_id, key="compile:0", kind="compile")
        session.add(second)
        session.commit()
        second_id = second.id
    first = lifecycle.claim_unit(first_id)
    second = lifecycle.claim_unit(second_id)
    assert first is not None and second is not None
    assert _pause(database, build_id).execution_state == "pausing"
    with pytest.raises(ExecutionConflict, match="pause_in_progress"):
        _resume(database, build_id)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        unit = session.get(BrainUnit, first_id)
        unit.status, unit.lease_until, unit.result = "completed", None, {"checkpoint": "first"}
        assert finish_pause(session, build) is False
        session.commit()
    with database() as session:
        build = session.get(BrainBuild, build_id)
        unit = session.get(BrainUnit, second_id)
        unit.status, unit.lease_until, unit.result = "completed", None, {"checkpoint": "last"}
        assert finish_pause(session, build) is True
        session.commit()
    with database() as session:
        assert session.get(BrainBuild, build_id).execution_state == "paused"
        assert session.get(BrainUnit, second_id).result == {"checkpoint": "last"}


@pytest.mark.parametrize("state", ["unknown", "reserved"])
def test_uncertain_calls_never_become_a_successful_pause(database, trial, state):
    build_id, unit_id = trial
    with database() as session:
        build = session.get(BrainBuild, build_id)
        build.execution_state, build.reserved_tokens = "pausing", 17
        session.add(BrainModelCall(id=str(uuid4()), build_id=build_id, unit_id=unit_id,
            lease_token=str(uuid4()), stage="scan", role="privacy", purpose="generic",
            requested_model="local_model", attempt=1, state=state, reserved_tokens=17))
        session.commit()
    with database() as session:
        with pytest.raises(ModelCallsIncomplete):
            finish_pause(session, session.get(BrainBuild, build_id))
    with database() as session:
        assert session.get(BrainBuild, build_id).execution_state == "pausing"
        assert session.get(BrainBuild, build_id).reserved_tokens == 17


@pytest.mark.parametrize("field,value", [("_unknown_tokens", 17), ("_unknown_tokens", -1),
                                        ("_reserved_tokens", 17), ("_reserved_tokens", "17")])
def test_unaccounted_or_invalid_payload_counters_prevent_paused(database, trial, field, value):
    build_id, unit_id = trial
    with database() as session:
        build = session.get(BrainBuild, build_id)
        build.execution_state = "pausing"
        session.get(BrainUnit, unit_id).payload = {field: value}
        session.commit()
    with database() as session:
        with pytest.raises(ModelCallsIncomplete):
            finish_pause(session, session.get(BrainBuild, build_id))


def test_expired_running_lease_still_requires_fencing_before_pause(database, trial):
    build_id, unit_id = trial
    assert lifecycle.claim_unit(unit_id) is not None
    with database() as session:
        session.get(BrainUnit, unit_id).lease_until = utcnow() - timedelta(seconds=10)
        session.commit()
    assert _pause(database, build_id).execution_state == "pausing"
    with database() as session:
        unit = session.get(BrainUnit, unit_id)
        unit.status, unit.lease_until, unit.lease_token = "pending", None, None
        assert finish_pause(session, session.get(BrainBuild, build_id)) is True
        session.commit()


def test_valid_lease_on_a_pending_row_cannot_be_hidden_by_pause(database, trial):
    build_id, unit_id = trial
    with database() as session:
        session.get(BrainUnit, unit_id).lease_until = utcnow() + timedelta(seconds=10)
        session.commit()
    assert _pause(database, build_id).execution_state == "pausing"


@pytest.mark.parametrize("lease_state", ["expired", "completed"])
def test_draining_build_cannot_reserve_again_from_a_stale_unit_lease(database, trial, lease_state):
    build_id, unit_id = trial
    claimed = lifecycle.claim_unit(unit_id)
    assert claimed is not None
    token = claimed[3]
    assert _pause(database, build_id).execution_state == "pausing"
    with database() as session:
        unit = session.get(BrainUnit, unit_id)
        if lease_state == "expired":
            unit.lease_until = utcnow() - timedelta(seconds=1)
        else:
            unit.status, unit.lease_until = "completed", None
        session.commit()
        before = _ledger(session, build_id)
    with pytest.raises(ValueError):
        lifecycle.reserve_tokens(build_id, token, unit_id, 17)
    with database() as session:
        assert _ledger(session, build_id) == before


@pytest.mark.parametrize("status", ["ready", "failed", "superseded"])
def test_terminal_outcome_wins_over_draining_execution(database, trial, status):
    build_id, _ = trial
    with database() as session:
        build = session.get(BrainBuild, build_id)
        build.status, build.execution_state, build.completed_at = status, "pausing", utcnow()
        completion = build.completed_at
        assert finish_pause(session, build) is False
        assert build.status == status and build.completed_at == completion
        assert build.execution_state == "running" and build.paused_at is None
        session.commit()
    for operation in (_pause, _resume):
        with pytest.raises(ExecutionConflict, match="build_terminal"):
            operation(database, build_id)


def test_resume_refuses_a_changed_runtime_recipe(database, trial, monkeypatch):
    build_id, _ = trial
    _pause(database, build_id)
    monkeypatch.setattr(lifecycle, "recipe_hash", lambda: "b" * 64)
    with pytest.raises(ExecutionConflict, match="recipe_changed"):
        _resume(database, build_id)


@pytest.mark.parametrize("change", ["request", "maintenance", "missing", "branch"])
def test_resume_does_not_revive_stale_nonhistorical_maintenance(database, trial, change):
    build_id, _ = trial
    _pause(database, build_id)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        build.historical = False
        project = session.get(BrainProject, build.project_id)
        project.maintenance_state, project.source_status = "active", "available"
        if change == "request":
            project.request_seq += 1
        elif change == "maintenance":
            project.maintenance_state = "paused"
        elif change == "missing":
            project.source_status = "missing"
        else:
            project.branch = "unmanaged-fixture-branch"
        session.commit()
    with pytest.raises(ExecutionConflict, match="maintenance_stale"):
        _resume(database, build_id)


def test_resume_allows_the_current_nonhistorical_request(database, trial):
    build_id, _ = trial
    _pause(database, build_id)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        build.historical = False
        project = session.get(BrainProject, build.project_id)
        project.maintenance_state, project.source_status = "active", "available"
        project.request_seq = build.request_seq
        session.commit()
    assert _resume(database, build_id).execution_state == "running"


def test_resume_rejects_privacy_write_intent(database, trial):
    build_id, _ = trial
    _pause(database, build_id)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        build.coverage = {**build.coverage, "recovery_write_intents": [str(uuid4())]}
        session.commit()
    with pytest.raises(ExecutionConflict, match="recovery_dependency_invalid"):
        _resume(database, build_id)


def test_resume_rechecks_the_ledger_even_if_the_row_says_paused(database, trial):
    build_id, unit_id = trial
    _pause(database, build_id)
    with database() as session:
        session.get(BrainUnit, unit_id).payload = {"_unknown_tokens": 5}
        session.commit()
    with pytest.raises(ModelCallsIncomplete):
        _resume(database, build_id)


def test_running_resume_is_idempotent_and_has_no_implicit_pause(database, trial):
    build_id, _ = trial
    build = _resume(database, build_id)
    assert build.execution_state == "running" and build.resumed_at is None


def test_noop_finish_does_not_change_a_running_build(database, trial):
    build_id, _ = trial
    with database() as session:
        build = session.get(BrainBuild, build_id)
        assert finish_pause(session, build) is False
        assert build.execution_state == "running" and build.paused_at is None


def test_missing_build_has_a_fixed_conflict_reason(database):
    for operation in (_pause, _resume):
        with pytest.raises(ExecutionConflict) as error:
            operation(database, str(uuid4()))
        assert error.value.reason == str(error.value) == "build_not_found"


def test_a_new_pause_cycle_has_new_request_time_and_retains_prior_resume(database, trial):
    build_id, _ = trial
    first = _pause(database, build_id)
    resumed = _resume(database, build_id)
    second = _pause(database, build_id)
    assert second.pause_requested_at >= first.pause_requested_at
    assert second.resumed_at == resumed.resumed_at
    assert second.paused_at >= second.pause_requested_at


def test_pending_recovery_can_pause_before_its_scan_freezes_the_parent(database, trial):
    from src.core.project_brain.recovery import create_recovery

    parent_id, unit_id = trial
    with database() as session:
        parent = session.get(BrainBuild, parent_id)
        parent.status, parent.error_message = "failed", "synthetic_failure"
        session.get(BrainUnit, unit_id).status = "failed"
        child = create_recovery(session, parent_id)
        session.commit()
        child_id = child.id
    _pause(database, child_id)
    assert _resume(database, child_id).execution_state == "running"
    with database() as session:
        assert session.get(BrainRecovery, child_id).status == "pending"


def test_pending_recovery_refuses_a_changed_parent_privacy_intent(database, trial):
    from src.core.project_brain.recovery import create_recovery

    parent_id, unit_id = trial
    with database() as session:
        parent = session.get(BrainBuild, parent_id)
        parent.status, parent.error_message = "failed", "synthetic_failure"
        session.get(BrainUnit, unit_id).status = "failed"
        child = create_recovery(session, parent_id)
        session.commit()
        child_id = child.id
    _pause(database, child_id)
    with database() as session:
        parent = session.get(BrainBuild, parent_id)
        parent.coverage = {**parent.coverage, "recovery_write_intents": [str(uuid4())]}
        session.commit()
    with pytest.raises(ExecutionConflict, match="recovery_dependency_invalid"):
        _resume(database, child_id)


def test_paused_recovery_missing_its_database_dependency_is_rejected(database, trial):
    build_id, _ = trial
    _pause(database, build_id)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        build.coverage = {**build.coverage, "recovery_protocol": "brain-recovery/1"}
        session.commit()
    with pytest.raises(ExecutionConflict, match="recovery_dependency_invalid"):
        _resume(database, build_id)


def test_pause_resume_preserve_successful_calls_closed_http_bounds_and_checkpoints(database, trial):
    build_id, unit_id = trial
    group, lease = str(uuid4()), str(uuid4())
    with database() as session:
        build = session.get(BrainBuild, build_id)
        build.total_prompt_tokens, build.total_completion_tokens = 3, 2
        build.total_cached_tokens, build.reserved_tokens = 1, 22
        unit = session.get(BrainUnit, unit_id)
        unit.status, unit.attempts, unit.result = "completed", 2, {"checkpoint": "authenticated"}
        unit.payload = {"_reserved_tokens": 0, "_unknown_tokens": 0}
        for attempt, state, bound, prompt, completion in ((1, "http_error", 17, 0, 0), (2, "settled", 19, 3, 2)):
            session.add(BrainModelCall(id=str(uuid4()), build_id=build_id, unit_id=unit_id,
                lease_token=lease, stage="scan", role="privacy", purpose="generic",
                requested_model="local_model", attempt=1, state=state, reserved_tokens=bound,
                prompt_tokens=prompt, completion_tokens=completion, cached_tokens=1 if state == "settled" else 0,
                transport_group=group, transport_attempt=attempt,
                http_status=500 if state == "http_error" else None,
                closed_at=utcnow() if state == "http_error" else None,
                settled_at=utcnow() if state == "settled" else None))
        session.commit()
        before = _ledger(session, build_id)
    assert _pause(database, build_id).execution_state == "paused"
    assert _resume(database, build_id).execution_state == "running"
    with database() as session:
        assert _ledger(session, build_id) == before


def test_a_closed_http_error_without_its_own_success_cannot_pause_or_resume(database, trial):
    build_id, unit_id = trial
    with database() as session:
        build = session.get(BrainBuild, build_id)
        build.reserved_tokens = 17
        session.add(BrainModelCall(id=str(uuid4()), build_id=build_id, unit_id=unit_id,
            lease_token=str(uuid4()), stage="scan", role="privacy", purpose="generic",
            requested_model="local_model", attempt=1, state="http_error", reserved_tokens=17,
            transport_group=str(uuid4()), transport_attempt=1, http_status=500, closed_at=utcnow()))
        session.commit()
    assert _pause(database, build_id).execution_state == "pausing"
    with database() as session:
        session.get(BrainBuild, build_id).execution_state = "paused"
        session.commit()
    with pytest.raises(ModelCallsIncomplete):
        _resume(database, build_id)


def test_pause_intent_survives_existing_unknown_accounting(database, trial):
    build_id, unit_id = trial
    with database() as session:
        session.get(BrainUnit, unit_id).payload = {"_unknown_tokens": 17}
        session.commit()
    paused = _pause(database, build_id)
    assert paused.execution_state == "pausing" and paused.paused_at is None
    assert lifecycle.claim_unit(unit_id) is None
    with database() as session:
        build = session.get(BrainBuild, build_id)
        assert build.execution_state == "pausing" and build.pause_requested_at is not None
        with pytest.raises(ModelCallsIncomplete):
            finish_pause(session, build)


def test_pause_intent_survives_unknown_call_without_a_payload_counter(database, trial):
    build_id, unit_id = trial
    with database() as session:
        build = session.get(BrainBuild, build_id)
        build.reserved_tokens = 17
        session.add(BrainModelCall(id=str(uuid4()), build_id=build_id, unit_id=unit_id,
            lease_token=str(uuid4()), stage="scan", role="privacy", purpose="generic",
            requested_model="local_model", attempt=1, state="unknown", reserved_tokens=17))
        session.commit()
        before = _ledger(session, build_id)
    requested = _pause(database, build_id)
    assert requested.execution_state == "pausing" and requested.paused_at is None
    assert lifecycle.claim_unit(unit_id) is None
    with database() as session:
        assert _ledger(session, build_id) == before
        with pytest.raises(ModelCallsIncomplete):
            finish_pause(session, session.get(BrainBuild, build_id))


@pytest.mark.parametrize("change", ["plan", "imports", "manifest", "scan", "protocol"])
def test_pending_recovery_resume_rejects_partial_or_unknown_dependency_state(database, trial, change):
    from src.core.project_brain.recovery import create_recovery

    parent_id, unit_id = trial
    with database() as session:
        parent = session.get(BrainBuild, parent_id)
        parent.status, parent.error_message = "failed", "synthetic_failure"
        session.get(BrainUnit, unit_id).status = "failed"
        child = create_recovery(session, parent_id)
        session.commit()
        child_id = child.id
    _pause(database, child_id)
    with database() as session:
        child = session.get(BrainBuild, child_id)
        dependency = session.get(BrainRecovery, child_id)
        if change == "plan":
            dependency.plan = {"synthetic": "partial"}
        elif change == "imports":
            dependency.imports = {"synthetic": "partial"}
        elif change == "manifest":
            dependency.manifest_hash = "a" * 64
        elif change == "scan":
            scan = session.scalar(select(BrainUnit).where(BrainUnit.build_id == child_id, BrainUnit.kind == "scan"))
            scan.status = "completed"
        else:
            child.coverage = {**child.coverage, "recovery_protocol": "synthetic-unsupported-protocol"}
        session.commit()
    with pytest.raises(ExecutionConflict, match="recovery_dependency_invalid"):
        _resume(database, child_id)
    with database() as session:
        assert session.get(BrainBuild, child_id).execution_state == "paused"


def test_resume_does_not_trust_a_frozen_label_without_dependency_proofs(database, trial):
    from src.core.project_brain.recovery import create_recovery

    parent_id, unit_id = trial
    with database() as session:
        parent = session.get(BrainBuild, parent_id)
        parent.status, parent.error_message = "failed", "synthetic_failure"
        session.get(BrainUnit, unit_id).status = "failed"
        child = create_recovery(session, parent_id)
        session.commit()
        child_id = child.id
    _pause(database, child_id)
    with database() as session:
        session.get(BrainRecovery, child_id).status = "frozen"
        session.commit()
    with pytest.raises(ExecutionConflict, match="recovery_dependency_invalid"):
        _resume(database, child_id)
