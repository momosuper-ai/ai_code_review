import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.config import settings
from src.core.project_brain.exceptions import ModelTransportFailed, PrivacyResponseInvalid
from src.core.project_brain.execution import pause_build, resume_build
from src.core.project_brain.incremental import IncrementalRejected, accept_incremental
from src.core.project_brain.lifecycle import BudgetExhausted, create_build
from src.core.project_brain.trusted_packet import checkpoint_digest
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainUnit, utcnow
from src.tasks import brain_task
from tests.unit.test_project_brain import database  # noqa: F401


def _build(database, count=1, commit="a" * 40):
    with database() as session:
        build = create_build(session, 1, "main", "synthetic/retry", commit, historical=True)
        units = [session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))]
        for index in range(1, count):
            units.append(BrainUnit(build_id=build.id, key=f"transient:{index}", kind="privacy"))
        session.add_all(units[1:])
        session.commit()
        return build.id, [unit.id for unit in units]


def _incremental_trial(database):
    parent_id, (parent_unit_id,) = _build(database, commit="d" * 40)
    child_id, (child_unit_id,) = _build(database, commit="e" * 40)
    safe = {"title": "Module", "claims": [{"text": "claim", "evidence_ids": ["evidence"],
            "source_scope": None}], "unknowns": [], "document_path": None}
    with database() as session:
        parent = session.get(BrainBuild, parent_id)
        parent.status = "ready"
        parent.coverage = {"public_schema_version": "2.0"}
        parent_unit = session.get(BrainUnit, parent_unit_id)
        parent_unit.kind, parent_unit.status = "compile", "completed"
        parent_unit.input_hash, parent_unit.result = "input", safe
        child = session.get(BrainBuild, child_id)
        child.base_build_id, child.recipe_hash = parent_id, parent.recipe_hash
        child_unit = session.get(BrainUnit, child_unit_id)
        child_unit.kind, child_unit.input_hash = "compile", "input"
        session.commit()
    return parent_id, parent_unit_id, child_id, child_unit_id, safe


def test_incremental_accept_strips_marker_and_records_proof(database, monkeypatch):
    import src.core.project_brain.incremental as incremental

    parent_id, parent_unit_id, child_id, child_unit_id, safe = _incremental_trial(database)
    monkeypatch.setattr(incremental, "_parent_is_trusted", lambda *args: True)
    marker = {"base_build_id": parent_id, "parent_unit_id": parent_unit_id,
              "parent_input_hash": "input", "result_hash": checkpoint_digest(safe), "input_hash": "input"}
    with database() as session:
        build, unit = session.get(BrainBuild, child_id), session.get(BrainUnit, child_unit_id)
        assert accept_incremental(session, build, unit, {**safe, "_incremental_import": marker}) == safe
        assert build.diagnostic["incremental_imports"][str(unit.id)] == marker


def test_incremental_accept_rejects_tampered_parent(database):
    parent_id, parent_unit_id, child_id, child_unit_id, safe = _incremental_trial(database)
    marker = {"base_build_id": parent_id, "parent_unit_id": parent_unit_id,
              "parent_input_hash": "input", "result_hash": checkpoint_digest(safe), "input_hash": "input"}
    with database() as session:
        session.get(BrainUnit, parent_unit_id).result = {**safe, "title": "tampered"}
        build, unit = session.get(BrainBuild, child_id), session.get(BrainUnit, child_unit_id)
        with pytest.raises(IncrementalRejected):
            accept_incremental(session, build, unit, {**safe, "_incremental_import": marker})


def test_incremental_base_requires_ready_schema2_recipe_and_project(database):
    from src.core.project_brain.incremental import _base_build

    parent_id, _, child_id, _, _ = _incremental_trial(database)
    with database() as session:
        parent, child = session.get(BrainBuild, parent_id), session.get(BrainBuild, child_id)
        assert _base_build(session, child, parent_id) is parent
        child.project_id = child.project_id + 1
        assert _base_build(session, child, parent_id) is None
        child.project_id = parent.project_id
        parent.status = "failed"
        assert _base_build(session, child, parent_id) is None
        parent.status = "ready"
        parent.coverage = {"public_schema_version": "1.0"}
        assert _base_build(session, child, parent_id) is None
        parent.coverage = {"public_schema_version": "2.0"}
        parent.recipe_hash = "different"
        assert _base_build(session, child, parent_id) is None


def test_legacy_ready_incremental_build_without_summary_remains_valid(database):
    import src.core.project_brain.incremental as incremental

    _, _, child_id, _, _ = _incremental_trial(database)
    with database() as session:
        child = session.get(BrainBuild, child_id)
        child.status = "ready"
        child.coverage = {"public_schema_version": "1.0"}
        child.diagnostic = None
        incremental.verify_incremental_dependencies(session, child)


def test_current_ready_incremental_build_requires_summary(database):
    import src.core.project_brain.incremental as incremental

    _, _, child_id, _, _ = _incremental_trial(database)
    with database() as session:
        child = session.get(BrainBuild, child_id)
        child.status = "ready"
        child.coverage = {"public_schema_version": "2.0"}
        child.diagnostic = {"generator_version": "project-brain/2.4.1"}
        with pytest.raises(IncrementalRejected):
            incremental.verify_incremental_dependencies(session, child)


def test_incremental_import_requires_summary(database):
    import src.core.project_brain.incremental as incremental

    _, _, child_id, _, _ = _incremental_trial(database)
    with database() as session:
        child = session.get(BrainBuild, child_id)
        child.status = "ready"
        child.coverage = {"public_schema_version": "1.0"}
        child.diagnostic = {"incremental_imports": {"1": {}}}
        with pytest.raises(IncrementalRejected):
            incremental.verify_incremental_dependencies(session, child)


def test_unparseable_generator_version_requires_incremental_summary(database):
    import src.core.project_brain.incremental as incremental

    _, _, child_id, _, _ = _incremental_trial(database)
    with database() as session:
        child = session.get(BrainBuild, child_id)
        child.status = "ready"
        child.coverage = {"public_schema_version": "1.0"}
        child.diagnostic = {"generator_version": "unparseable"}
        with pytest.raises(IncrementalRejected):
            incremental.verify_incremental_dependencies(session, child)


def test_recovery_does_not_treat_incremental_no_call_as_recovery_proof(database):
    from src.core.project_brain.recovery import _successful_unit_calls

    parent_id, parent_unit_id, _, _, _ = _incremental_trial(database)
    with database() as session:
        parent = session.get(BrainBuild, parent_id)
        parent.diagnostic = {"incremental_imports": {str(parent_unit_id): {"input_hash": "input"}}}
        unit = session.get(BrainUnit, parent_unit_id)
        assert _successful_unit_calls(session, unit) is False


def test_chained_incremental_checkpoint_remains_a_valid_parent(database, monkeypatch):
    import src.core.project_brain.incremental as incremental
    import src.core.project_brain.recovery as recovery

    root_id, root_unit_id, middle_id, middle_unit_id, safe = _incremental_trial(database)
    monkeypatch.setattr(recovery, "_successful_unit_calls", lambda *args: True)
    marker = {"base_build_id": root_id, "parent_unit_id": root_unit_id,
              "parent_input_hash": "input", "result_hash": checkpoint_digest(safe), "input_hash": "input"}
    with database() as session:
        middle, unit = session.get(BrainBuild, middle_id), session.get(BrainUnit, middle_unit_id)
        middle.status = "ready"
        middle.coverage = {"public_schema_version": "2.0", "incremental": {
            "base_build_id": root_id, "reused_compile_units": 1, "compiled_units": 0}}
        middle.diagnostic = {"incremental_imports": {str(unit.id): marker}}
        unit.status, unit.result = "completed", safe
        session.add(BrainModelCall(id=str(uuid4()), build_id=root_id, unit_id=root_unit_id,
            lease_token=str(uuid4()), stage="compile", role="semantic", purpose="generic",
            requested_model="synthetic", attempt=1, state="settled", reserved_tokens=0,
            settled_at=utcnow()))
        assert incremental._parent_is_trusted(session, unit)
        middle.diagnostic = {}
        with pytest.raises(incremental.IncrementalRejected):
            incremental.verify_incremental_dependencies(session, middle)


def test_incremental_import_increments_finalize_cache_hits(database, monkeypatch):
    import src.core.project_brain.incremental as incremental

    build_id, (unit_id,) = _build(database)
    unit, build, _, token = brain_task.claim_unit(unit_id)
    with database() as session:
        session.get(BrainBuild, build_id).status = "compiling"
        session.get(BrainUnit, unit_id).kind = "compile"
        session.add(BrainUnit(build_id=build_id, key="finalize", kind="finalize", status="completed",
                              result={"cache_hits": 7}))
        session.commit()

    monkeypatch.setattr(incremental, "accept_incremental",
        lambda session, build, unit, result: {key: value for key, value in result.items()
                                              if key != "_incremental_import"})
    result = {"title": "Module", "_incremental_import": {"parent_unit_id": 1}}
    assert brain_task._complete(unit.id, token, result)
    with database() as session:
        stored = session.get(BrainUnit, unit_id)
        final = session.scalar(select(BrainUnit).where(
            BrainUnit.build_id == build_id, BrainUnit.kind == "finalize"))
        assert stored.result == {"title": "Module"}
        assert final.result["cache_hits"] == 8


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("change", ["unchanged", "blob_changed", "dependency_changed", "privacy_projection_changed"])
def test_incremental_reuses_matching_input_hash_despite_index_shift(database, monkeypatch, index, change):
    import src.core.project_brain.compiler as compiler
    import src.core.project_brain.incremental as incremental
    from src.core.project_brain.trusted_packet import VerifiedPacket

    parent_id, parent_unit_id, child_id, child_unit_id, safe = _incremental_trial(database)
    contexts = [
        {"module_id": "other", "items": []},
        {"module_id": "module", "items": [{"fact": {"id": "fact"}, "evidence": [{"id": "evidence"}]}]},
    ]
    if index == 0:
        contexts = [contexts[1]]

    class Bundle:
        source_commit = "e" * 40

        def model_dump(self):
            return {"source_commit": self.source_commit}

    class Guard:
        filter = object()

        def __init__(self):
            self.checked = False

        def check_discoveries(self):
            self.checked = True

        async def close(self):
            pass

    class Proof:
        def validate(self, packet):
            assert packet == contexts[index]

    class Store:
        def directory(self, *_):
            return "/snapshot"

    class Catalog:
        snapshot = object()

    guard = Guard()
    monkeypatch.setattr(incremental, "_parent_is_trusted", lambda *args: True)
    monkeypatch.setattr("src.core.project_brain.snapshot.read_snapshot", lambda *_: object())
    monkeypatch.setattr(brain_task, "_source_catalog", lambda *args: Catalog())
    monkeypatch.setattr(brain_task, "_read_source_bundle", lambda *args: Bundle())
    monkeypatch.setattr(brain_task, "_contexts", lambda *args: contexts)
    monkeypatch.setattr(brain_task, "_bind_source_identity", lambda *args: None)
    monkeypatch.setattr(brain_task, "_guard", lambda *args, **kwargs: guard)
    packet_hash = "input" if change in {"unchanged", "privacy_projection_changed"} else "changed-input"
    monkeypatch.setattr(compiler, "packet_hash", lambda *_: packet_hash)
    projected = safe if change != "privacy_projection_changed" else {**safe, "title": "redacted"}
    monkeypatch.setattr(compiler, "project_compilation_texts", lambda value, _filter: projected)
    monkeypatch.setattr(VerifiedPacket, "from_checkpoint", classmethod(lambda cls, *args, **kwargs: Proof()))
    with database() as session:
        unit = session.get(BrainUnit, child_unit_id)
        unit.payload = {"index": index, "module_id": "module", "fact_ids": ["fact"]}
        session.add_all([
            BrainUnit(build_id=child_id, key="finalize", kind="finalize", status="completed",
                      result={"bundle_mac": "mac"}),
            BrainUnit(build_id=child_id, key="privacy:0", kind="privacy", status="completed"),
            BrainUnit(build_id=child_id, key="source_privacy:0", kind="source_privacy", status="completed"),
        ])
        session.commit()
        build = session.get(BrainBuild, child_id)
        project = session.get(BrainProject, build.project_id)
        result = asyncio.run(incremental.incremental_compile_checkpoint(
            unit, build, project, "lease", Store()))
    if change in {"blob_changed", "dependency_changed", "privacy_projection_changed"}:
        assert result is None
    else:
        assert result == {**safe, "_incremental_import": {
            "base_build_id": parent_id, "parent_unit_id": parent_unit_id,
            "parent_input_hash": "input", "result_hash": checkpoint_digest(safe), "input_hash": "input"}}
    assert guard.checked is (change in {"unchanged", "privacy_projection_changed"})
    with database() as session:
        assert not session.scalars(select(BrainModelCall).where(
            BrainModelCall.build_id == child_id, BrainModelCall.unit_id == child_unit_id)).all()


@pytest.mark.parametrize("failure,countdowns", [
    (ModelTransportFailed("model_http_retry_exhausted"), [60, 120, 240]),
    (PrivacyResponseInvalid("invalid_json"), [60, 120, 240]),
])
def test_transient_unit_failures_retry_three_times_without_consuming_attempts(
        database, monkeypatch, failure, countdowns):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_ENABLED", True)
    build_id, (unit_id,) = _build(database, commit="c" * 40)
    monkeypatch.setattr(brain_task, "_execute", AsyncMock(side_effect=failure))
    dispatch = MagicMock()
    monkeypatch.setattr(brain_task.execute_brain_unit, "apply_async", dispatch)

    for index in range(4):
        assert brain_task.execute_brain_unit.run(unit_id) == {"status": "deferred" if index < 3 else "failed"}
        with database() as session:
            unit = session.get(BrainUnit, unit_id)
            if index < 3:
                assert unit.status == "pending" and unit.attempts == 0
                assert unit.lease_token is None and unit.dispatched_at is not None
                assert unit.diagnostic["transient_retries"] == index + 1
            else:
                assert unit.status == "failed" and unit.diagnostic["transient_retries"] == 3
                assert session.get(BrainBuild, build_id).status == "failed"
    assert [call.kwargs["countdown"] for call in dispatch.call_args_list] == countdowns
    assert all(call.kwargs == {"args": [unit_id], "countdown": delay, "queue": "brain", "retry": False}
               and call.args == () for call, delay in zip(dispatch.call_args_list, countdowns))


def test_unknown_call_preserves_original_diagnostic_and_never_retries(database, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_ENABLED", True)
    build_id, (unit_id,) = _build(database, commit="b" * 40)

    async def fail(unit, build, project, token):
        with database() as session:
            session.add(BrainModelCall(
                id=str(uuid4()), build_id=build.id, unit_id=unit.id, lease_token=token,
                stage=unit.kind, role="semantic", purpose="generic", requested_model="synthetic",
                response_model=None, attempt=1, state="unknown", reserved_tokens=0,
            ))
            session.commit()
        raise ModelTransportFailed()

    monkeypatch.setattr(brain_task, "_execute", fail)
    dispatch = MagicMock()
    monkeypatch.setattr(brain_task.execute_brain_unit, "apply_async", dispatch)
    assert brain_task.execute_brain_unit.run(unit_id) == {"status": "failed"}
    dispatch.assert_not_called()
    with database() as session:
        unit, build = session.get(BrainUnit, unit_id), session.get(BrainBuild, build_id)
        assert build.status == "failed" and unit.status == "failed"
        assert unit.diagnostic["last_failure"]["exception"] == "ModelTransportFailed"
        assert unit.diagnostic["last_failure"]["failure_code"] == "model_http_retry_exhausted"
        assert unit.diagnostic["last_failure"]["unknown_guard"] is True
        assert unit.diagnostic.get("transient_retries", 0) == 0
        assert "model_calls_incomplete" in build.error_message


def test_budget_exhaustion_is_terminal_and_broker_failure_releases_dispatch(database, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_ENABLED", True)
    build_id, (unit_id,) = _build(database, commit="b" * 40)
    monkeypatch.setattr(brain_task, "_execute", AsyncMock(side_effect=BudgetExhausted("synthetic")))
    dispatch = MagicMock()
    monkeypatch.setattr(brain_task.execute_brain_unit, "apply_async", dispatch)
    assert brain_task.execute_brain_unit.run(unit_id) == {"status": "failed"}
    dispatch.assert_not_called()
    with database() as session:
        assert session.get(BrainBuild, build_id).status == "failed"

    build_id, (unit_id,) = _build(database, commit="c" * 40)
    monkeypatch.setattr(brain_task, "_execute", AsyncMock(side_effect=ModelTransportFailed()))
    monkeypatch.setattr(brain_task.execute_brain_unit, "apply_async", MagicMock(side_effect=ConnectionError()))
    assert brain_task.execute_brain_unit.run(unit_id) == {"status": "deferred"}
    with database() as session:
        unit = session.get(BrainUnit, unit_id)
        assert session.get(BrainBuild, build_id).status in {"pending", "scanning"}
        assert unit.status == "pending" and unit.dispatched_at is None


def _recover(database, monkeypatch, build_id, *, now, state="tripped", trips=1, cooldown=30, elapsed=None):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_ENABLED", True)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_BREAKER_COOLDOWN_SECONDS", cooldown)
    monkeypatch.setattr(brain_task, "utcnow", lambda: now)
    monkeypatch.setattr(brain_task, "dispatch_pending", lambda *args: None)
    monkeypatch.setattr("src.tasks.brain_publication_task.dispatch_publications", lambda: None)
    monkeypatch.setattr(brain_task, "_retention", lambda: None)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        project = session.get(BrainProject, build.project_id)
        project.last_checked_at = now
        build.diagnostic = {"breaker": {"events": [], "trips": trips, "state": state, "tripped_at": now.isoformat()}}
        if build.execution_state != "paused":
            pause_build(session, build.id)
        build.paused_at = now - timedelta(seconds=elapsed if elapsed is not None else cooldown + 1)
        session.commit()
    return brain_task.recover_brain.run()


def test_manual_resume_closes_breaker_and_beat_respects_trip_cap(database, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_ENABLED", True)
    build_id, _ = _build(database)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        pause_build(session, build.id)
        build.diagnostic = {"breaker": {"events": [], "trips": 1, "state": "tripped", "tripped_at": utcnow().isoformat()}}
        session.commit()
    with database() as session:
        resume_build(session, build_id)
        session.commit()
    with database() as session:
        assert session.get(BrainBuild, build_id).diagnostic["breaker"]["state"] == "closed"

    now = utcnow() + timedelta(minutes=10)
    _recover(database, monkeypatch, build_id, now=now, trips=settings.PROJECT_BRAIN_BREAKER_MAX_TRIPS + 1)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        assert build.status == "failed"
        assert build.execution_state == "running"
        assert build.error_message == "上游模型服务持续异常，熔断次数已达上限；failure_code=upstream_unhealthy"


def test_six_transient_failures_trip_and_cooldown_resumes(database, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_ENABLED", True)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_BREAKER_THRESHOLD", 6)
    build_id, unit_ids = _build(database, count=6)
    monkeypatch.setattr(brain_task, "_execute", AsyncMock(side_effect=ModelTransportFailed()))
    monkeypatch.setattr(brain_task.execute_brain_unit, "apply_async", MagicMock())
    for unit_id in unit_ids:
        brain_task.execute_brain_unit.run(unit_id)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        assert build.execution_state == "paused"
        assert build.diagnostic["breaker"]["state"] == "tripped"
        assert build.diagnostic["breaker"]["trips"] == 1

    now = utcnow() + timedelta(minutes=10)
    _recover(database, monkeypatch, build_id, now=now, elapsed=10)
    with database() as session:
        assert session.get(BrainBuild, build_id).execution_state == "paused"
    _recover(database, monkeypatch, build_id, now=now, elapsed=31)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        assert build.execution_state == "running"
        assert build.diagnostic["breaker"]["state"] == "closed"


def test_manual_pause_with_closed_breaker_is_not_auto_resumed(database, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_ENABLED", True)
    build_id, _ = _build(database)
    now = utcnow() + timedelta(minutes=10)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        pause_build(session, build_id)
        build.diagnostic = {"breaker": {"events": [], "trips": 0, "state": "closed", "tripped_at": None}}
        build.paused_at = now - timedelta(seconds=31)
        session.commit()
    _recover(database, monkeypatch, build_id, now=now, state="closed", trips=0)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        assert build.execution_state == "paused"
        assert build.diagnostic["breaker"]["state"] == "closed"
