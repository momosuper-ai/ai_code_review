"""Budget handoffs preserve unknown charges and fence delayed task deliveries."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from src.core.llm_client import TokenUsage
from src.core.project_brain.lifecycle import BudgetBusy, BudgetExhausted, account_tokens, create_build
from src.models.brain import BrainBuild, BrainUnit, utcnow
from src.tasks.brain_task import _abandon_live_reservation, _guard, dispatch_pending, execute_brain_unit, recover_brain
from tests.unit.test_project_brain import database  # noqa: F401


def test_unknown_handoff_is_idempotent_without_releasing_budget() -> None:
    unit = BrainUnit(payload={"_reserved_tokens": 42, "_unknown_tokens": 17})
    _abandon_live_reservation(unit)
    _abandon_live_reservation(unit)
    assert unit.payload == {"_reserved_tokens": 0, "_unknown_tokens": 59}


def test_budget_busy_continues_after_five_seconds_without_failed_attempt(database, monkeypatch) -> None:
    with database() as session:
        build = create_build(session, 1, "main", "fixture/project", "a" * 40)
        session.commit()
        unit_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build.id))
    monkeypatch.setattr("src.tasks.brain_task._execute", AsyncMock(side_effect=BudgetBusy()))
    delayed = MagicMock()
    monkeypatch.setattr(execute_brain_unit, "apply_async", delayed)
    dispatch = MagicMock()
    monkeypatch.setattr("src.tasks.brain_task.dispatch_pending", dispatch)
    assert execute_brain_unit.run(unit_id) == {"status": "deferred"}
    delayed.assert_called_once_with(args=[unit_id], countdown=5, queue="brain", retry=False)
    dispatch.assert_not_called()
    with database() as session:
        unit = session.get(BrainUnit, unit_id)
        assert unit.status == "pending" and unit.attempts == 0
        assert unit.lease_token is None and unit.lease_until is None
        assert unit.dispatched_at is not None
    # The durable timestamp gives the delayed Celery delivery ownership; Beat
    # does not immediately duplicate the delivery while the countdown runs.
    dispatch_pending(build.id)
    delayed.assert_called_once()


def test_failed_budget_wait_delivery_returns_ownership_to_durable_recovery(database, monkeypatch) -> None:
    with database() as session:
        build = create_build(session, 1, "main", "fixture/project", "a" * 40)
        session.commit()
        unit_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build.id))
    monkeypatch.setattr("src.tasks.brain_task._execute", AsyncMock(side_effect=BudgetBusy()))
    monkeypatch.setattr(execute_brain_unit, "apply_async", MagicMock(side_effect=ConnectionError()))
    assert execute_brain_unit.run(unit_id) == {"status": "deferred"}
    with database() as session:
        unit = session.get(BrainUnit, unit_id)
        assert unit.status == "pending" and unit.attempts == 0
        assert unit.lease_token is None and unit.dispatched_at is None


def test_budget_exhaustion_is_terminal_and_cancels_other_leases(database, monkeypatch) -> None:
    with database() as session:
        build = create_build(session, 1, "main", "fixture/project", "a" * 40)
        build.reserved_tokens = 120
        session.flush()
        current = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        running = BrainUnit(build_id=build.id, key="other-running", kind="privacy", status="running",
                            lease_token="other-token", lease_until=utcnow() + timedelta(seconds=120),
                            payload={"_reserved_tokens": 90})
        pending = BrainUnit(build_id=build.id, key="other-pending", kind="privacy", status="pending")
        session.add_all([running, pending])
        session.commit()
        ids = current.id, running.id, pending.id
    monkeypatch.setattr("src.tasks.brain_task._execute", AsyncMock(side_effect=BudgetExhausted("预算不足")))
    retry = MagicMock()
    monkeypatch.setattr(execute_brain_unit, "retry", retry)
    assert execute_brain_unit.run(ids[0]) == {"status": "failed"}
    retry.assert_not_called()
    with database() as session:
        stored = session.get(BrainBuild, build.id)
        assert stored.status == "failed" and stored.reserved_tokens == 120
        assert stored.completed_at is not None
        units = [session.get(BrainUnit, uid) for uid in ids]
        assert all(u.status == "failed" and u.lease_token is None for u in units)
        assert units[0].attempts == 1
        assert units[1].payload["_reserved_tokens"] == 0
        assert units[1].payload["_unknown_tokens"] == 90


def test_retry_retains_delayed_delivery_ownership_and_unknown_charge(database, monkeypatch) -> None:
    with database() as session:
        build = create_build(session, 1, "main", "fixture/project", "a" * 40)
        build.reserved_tokens = 42
        session.flush()
        unit = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        unit.payload = {"_reserved_tokens": 42}
        session.commit()
        unit_id = unit.id
    monkeypatch.setattr("src.tasks.brain_task._execute", AsyncMock(side_effect=TimeoutError()))
    retry = MagicMock(side_effect=RuntimeError("已安排重试"))
    monkeypatch.setattr(execute_brain_unit, "retry", retry)
    with pytest.raises(RuntimeError, match="已安排重试"):
        execute_brain_unit.run(unit_id)
    with database() as session:
        unit = session.get(BrainUnit, unit_id)
        assert unit.status == "pending" and unit.lease_token is None
        assert unit.dispatched_at is not None
        assert unit.payload["_reserved_tokens"] == 0 and unit.payload["_unknown_tokens"] == 42
        assert session.get(BrainBuild, build.id).reserved_tokens == 42


def test_expired_lease_moves_live_to_unknown_once(database, monkeypatch) -> None:
    """Lost paid work is terminal; repeated recovery never frees its charge."""
    with database() as session:
        build = create_build(session, 1, "main", "fixture/project", "a" * 40)
        build.reserved_tokens = 40
        session.flush()
        unit = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        unit.status, unit.attempts, unit.lease_token = "running", 1, "expired-token"
        unit.lease_until = utcnow() - timedelta(seconds=1)
        unit.payload = {"_reserved_tokens": 40}
        session.commit()
        unit_id = unit.id
    monkeypatch.setattr("src.tasks.brain_task._schedule_project", AsyncMock())
    monkeypatch.setattr("src.tasks.brain_task.dispatch_pending", lambda *args: None)
    monkeypatch.setattr("src.tasks.brain_task._retention", lambda: None)
    monkeypatch.setattr("src.tasks.brain_publication_task.dispatch_publications", lambda: None)
    recover_brain.run()
    recover_brain.run()
    with database() as session:
        unit = session.get(BrainUnit, unit_id)
        assert unit.status == "failed" and unit.lease_token is None
        assert unit.payload["_reserved_tokens"] == 0 and unit.payload["_unknown_tokens"] == 40
        stored = session.get(BrainBuild, build.id)
        assert stored.reserved_tokens == 40 and stored.status == "failed"
        assert "failure_code=model_calls_incomplete" in stored.error_message


def test_guard_attributes_usage_to_unit_and_current_lease(database, tmp_path, monkeypatch) -> None:
    from src.config import settings

    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", False)
    account = MagicMock()
    monkeypatch.setattr("src.tasks.brain_task.account_tokens", account)
    unit = BrainUnit(id=123, build_id="5ed1a75b-dba8-4258-b801-c643ea5b2be6", payload={})
    guard = _guard(unit, "current-token")
    usage = TokenUsage(5, 2)
    guard.account(usage, False, 100)
    account.assert_called_once_with(unit.build_id, usage, False, 100,
                                   unit_id=123, lease_token="current-token")


def test_late_response_settles_unknown_charge_without_releasing_new_lease(database) -> None:
    """A lost old request may settle while its successor already has a reservation."""
    with database() as session:
        build = create_build(session, 1, "main", "fixture/project", "a" * 40)
        build.reserved_tokens = 92
        session.flush()
        unit = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        unit.status, unit.lease_token = "running", "successor-token"
        unit.payload = {"_reserved_tokens": 50, "_unknown_tokens": 42}
        session.commit()
        unit_id, build_id = unit.id, build.id
    account_tokens(build_id, TokenUsage(5, 2), False, 42, unit_id=unit_id, lease_token="old-token")
    with database() as session:
        unit = session.get(BrainUnit, unit_id)
        stored = session.get(BrainBuild, build_id)
        assert unit.payload == {"_reserved_tokens": 50, "_unknown_tokens": 0}
        assert unit.lease_token == "successor-token" and unit.status == "running"
        assert stored.reserved_tokens == 57
    account_tokens(build_id, TokenUsage(8, 3), False, 50, unit_id=unit_id, lease_token="successor-token")
    with database() as session:
        stored = session.get(BrainBuild, build_id)
        assert stored.reserved_tokens == 18
        assert (stored.total_prompt_tokens, stored.total_completion_tokens) == (13, 5)
        assert session.get(BrainUnit, unit_id).payload == {"_reserved_tokens": 0, "_unknown_tokens": 0}


def test_usage_with_foreign_unit_rolls_back_budget_and_token_totals(database) -> None:
    with database() as session:
        build = create_build(session, 1, "main", "fixture/project", "a" * 40)
        other = create_build(session, 2, "main", "fixture/other", "b" * 40)
        build.reserved_tokens = 42
        session.commit()
        foreign_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == other.id))
    with pytest.raises(ValueError, match="任务归属无效"):
        account_tokens(build.id, TokenUsage(5, 2), False, 42, unit_id=foreign_id, lease_token="foreign-token")
    with database() as session:
        stored = session.get(BrainBuild, build.id)
        assert stored.reserved_tokens == 42
        assert stored.total_prompt_tokens == stored.total_completion_tokens == 0
