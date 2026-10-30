from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.config import settings
from src.core.project_brain.exceptions import ModelTransportFailed, PrivacyResponseInvalid
from src.core.project_brain.execution import pause_build, resume_build
from src.core.project_brain.lifecycle import BudgetExhausted, create_build
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
