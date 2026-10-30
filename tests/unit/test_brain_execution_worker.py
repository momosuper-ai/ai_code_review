"""Exercise durable pause through real claim, task completion, and Beat hooks."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.config import settings
from src.core.llm_client import TokenUsage
from src.core.project_brain.execution import pause_build
from src.core.project_brain.lifecycle import claim_unit, create_build
from src.core.project_brain.privacy import PrivacyScanDeferred
from src.core.project_brain.exceptions import BudgetBusy
from src.core.project_brain.usage import BuildCallMeter, CallSpec
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainUnit, utcnow
from src.tasks import brain_task
from tests.unit.test_project_brain import database  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_worker(monkeypatch):
    monkeypatch.setattr("src.core.llm_client.LLMClient.chat", AsyncMock(side_effect=AssertionError("unexpected_model_io")))
    monkeypatch.setattr("src.core.gitlab_client.GitLabClient.get_project", AsyncMock(side_effect=AssertionError("unexpected_gitlab_io")))
    monkeypatch.setattr(brain_task, "_retention", MagicMock())
    monkeypatch.setattr("src.tasks.brain_publication_task.dispatch_publications", MagicMock())


def seed(database, *kinds):
    with database() as session:
        project_id = (session.scalar(select(func.count()).select_from(BrainProject)) or 0) + 1
        build = create_build(session, project_id, "main", "fixture/control", "a" * 40,
                             "full", historical=True)
        # These tests isolate scheduling and checkpoint control. Actual source
        # privacy sealing has its own integration coverage; no source I/O runs.
        build.coverage = {}
        scan = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        scan.key, scan.kind = f"{kinds[0]}:0", kinds[0]
        ids = [scan.id]
        for index, kind in enumerate(kinds[1:], 1):
            unit = BrainUnit(build_id=build.id, key=f"{kind}:{index}", kind=kind)
            session.add(unit)
            session.flush()
            ids.append(unit.id)
        session.commit()
        return SimpleNamespace(build_id=build.id, project_id=build.project_id, units=ids)


def pause(database, build_id):
    with database() as session:
        build = pause_build(session, build_id)
        state = build.execution_state
        session.commit()
        return state


def test_committed_pause_fences_dispatch_and_stale_queued_claim(database, monkeypatch):
    trial = seed(database, "scan")
    delivered = MagicMock()
    monkeypatch.setattr(brain_task.execute_brain_unit, "apply_async", delivered)
    assert pause(database, trial.build_id) == "paused"
    brain_task.dispatch_pending(trial.build_id)
    assert claim_unit(trial.units[0]) is None
    assert brain_task.execute_brain_unit.run(trial.units[0]) == {"status": "skipped"}
    delivered.assert_not_called()
    with database() as session:
        unit = session.get(BrainUnit, trial.units[0])
        assert unit.status == "pending" and unit.attempts == 0 and unit.lease_token is None


def test_two_claimed_units_drain_and_keep_new_frontier_pending(database):
    trial = seed(database, "scan", "compile")
    first, second = (claim_unit(unit_id) for unit_id in trial.units)
    assert first and second
    assert pause(database, trial.build_id) == "pausing"
    # A leased unit may finish its paid semantic/output-privacy chain while
    # admission is stopped. The original UUID settles its own charge.
    meter = BuildCallMeter(trial.build_id, first[0].id, first[3])
    call_id = str(uuid4())
    meter.reserve(call_id, 50, CallSpec("semantic", "deepseek-v4-flash", "initial"))
    meter.settle(call_id, TokenUsage(10, 2), False, 50, "deepseek-v4-flash")
    assert brain_task._complete(first[0].id, first[3], {"fingerprint": "fixture"})
    with database() as session:
        build = session.get(BrainBuild, trial.build_id)
        assert build.execution_state == "pausing" and build.paused_at is None
        new_unit = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id, BrainUnit.kind == "extract"))
        assert new_unit and new_unit.status == "pending"
        new_id = new_unit.id
    assert claim_unit(new_id) is None
    assert brain_task._complete(second[0].id, second[3], {"checkpoint": "kept"})
    with database() as session:
        build = session.get(BrainBuild, trial.build_id)
        assert build.execution_state == "paused" and build.paused_at is not None
        assert build.status == "compiling"
        assert build.reserved_tokens == build.total_prompt_tokens + build.total_completion_tokens == 12
        call = session.get(BrainModelCall, call_id)
        assert call.state == "settled" and call.unit_id == first[0].id
        assert session.get(BrainUnit, second[0].id).result == {"checkpoint": "kept"}
        assert all(unit.status in {"completed", "pending"} for unit in session.scalars(
            select(BrainUnit).where(BrainUnit.build_id == build.id)))


def test_finalize_early_return_acknowledges_pause_and_keeps_source_privacy_frontier(database):
    trial = seed(database, "finalize")
    claimed = claim_unit(trial.units[0])
    assert pause(database, trial.build_id) == "pausing"
    packet = {"index": 0, "hash": "c" * 64, "module_id": "fixture-module", "fact_ids": ["fixture-fact"]}
    assert brain_task._complete(claimed[0].id, claimed[3], {"bundle_mac": "fixture-mac", "packets": [packet]})
    with database() as session:
        build = session.get(BrainBuild, trial.build_id)
        frontier = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id, BrainUnit.kind == "source_privacy"))
        assert build.execution_state == "paused" and build.status == "extracting"
        assert frontier.status == "pending" and frontier.lease_token is None and frontier.dispatched_at is None
        assert session.get(BrainUnit, claimed[0].id).result["cache_hits"] == 0
        frontier_id = frontier.id
    assert claim_unit(frontier_id) is None


@pytest.mark.parametrize("outcome", [PrivacyScanDeferred, BudgetBusy, RuntimeError])
def test_task_yield_or_retryable_error_during_pause_commits_without_scheduling_retry(database, monkeypatch, outcome):
    trial = seed(database, "compile")
    dispatch, delivered, retry = MagicMock(), MagicMock(), MagicMock(side_effect=AssertionError("unexpected_retry"))
    monkeypatch.setattr(brain_task, "dispatch_pending", dispatch)
    monkeypatch.setattr(brain_task.execute_brain_unit, "apply_async", delivered)
    monkeypatch.setattr(brain_task.execute_brain_unit, "retry", retry)

    async def execute(unit, build, project, token):
        assert pause(database, build.id) == "pausing"
        raise outcome()

    monkeypatch.setattr(brain_task, "_execute", execute)
    result = brain_task.execute_brain_unit.run(trial.units[0])
    assert result == {"status": "paused"}
    dispatch.assert_not_called()
    delivered.assert_not_called()
    retry.assert_not_called()
    with database() as session:
        build = session.get(BrainBuild, trial.build_id)
        unit = session.get(BrainUnit, trial.units[0])
        assert build.execution_state == "paused" and build.status == "compiling"
        assert unit.status == "pending" and unit.lease_token is None and unit.lease_until is None
        assert unit.dispatched_at is None
        assert unit.attempts == (1 if outcome is RuntimeError else 0)


def test_ready_completion_wins_over_pause_request(database):
    trial = seed(database, "assemble")
    claimed = claim_unit(trial.units[0])
    assert pause(database, trial.build_id) == "pausing"
    assert brain_task._complete(claimed[0].id, claimed[3], {
        "artifact_path": "fixture-artifact", "coverage": {}, "fingerprint": "fixture-fingerprint"})
    with database() as session:
        build = session.get(BrainBuild, trial.build_id)
        assert build.status == "ready" and build.completed_at is not None
        assert build.execution_state == "running" and build.paused_at is None
        assert session.get(BrainUnit, claimed[0].id).status == "completed"


@pytest.mark.parametrize("unknown", [False, True])
def test_beat_fences_expired_lease_then_pauses_or_retains_unknown_and_fails(database, monkeypatch, unknown):
    trial = seed(database, "compile")
    claimed = claim_unit(trial.units[0])
    assert pause(database, trial.build_id) == "pausing"
    call_id = str(uuid4())
    if unknown:
        BuildCallMeter(trial.build_id, claimed[0].id, claimed[3]).reserve(
            call_id, 7, CallSpec("semantic", "deepseek-v4-flash", "initial"))
    with database() as session:
        session.get(BrainUnit, trial.units[0]).lease_until = utcnow() - timedelta(seconds=1)
        session.commit()
    delivered = MagicMock()
    monkeypatch.setattr(brain_task.execute_brain_unit, "apply_async", delivered)
    assert brain_task.recover_brain.run()["status"] == "recovered"
    delivered.assert_not_called()
    with database() as session:
        build = session.get(BrainBuild, trial.build_id)
        unit = session.get(BrainUnit, trial.units[0])
        assert unit.lease_token is None and unit.lease_until is None and unit.dispatched_at is None
        if unknown:
            assert build.status == "failed" and build.execution_state == "running"
            assert build.reserved_tokens == 7 and unit.status == "failed"
            assert session.get(BrainModelCall, call_id).state == "unknown"
            assert unit.payload["_unknown_tokens"] == 7
            assert "failure_code=model_calls_incomplete" in build.error_message
        else:
            assert build.execution_state == "paused" and unit.status == "pending"
            assert build.status == "compiling" and build.reserved_tokens == 0


def test_dispatch_capacity_counts_draining_running_units_but_ignores_paused_pending(database, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_CONCURRENCY", 1)
    paused = seed(database, "scan")
    assert pause(database, paused.build_id) == "paused"
    with database() as session:
        session.get(BrainUnit, paused.units[0]).dispatched_at = utcnow()
        session.commit()
    draining = seed(database, "compile", "compile")
    claims = [claim_unit(unit_id) for unit_id in draining.units]
    assert pause(database, draining.build_id) == "pausing"
    running = seed(database, "scan")
    delivered = MagicMock()
    monkeypatch.setattr(brain_task.execute_brain_unit, "apply_async", delivered)
    brain_task.dispatch_pending()
    delivered.assert_not_called()
    assert brain_task._complete(claims[0][0].id, claims[0][3], {"checkpoint": "kept"})
    brain_task.dispatch_pending()
    delivered.assert_called_once_with(args=[running.units[0]], queue="brain", retry=False)


def test_direct_stale_delivery_cannot_reclaim_a_lost_paid_call_before_beat(database):
    """Broker redelivery must apply the same unknown fence as lease recovery."""
    trial = seed(database, "compile")
    claimed = claim_unit(trial.units[0])
    call_id = str(uuid4())
    BuildCallMeter(trial.build_id, claimed[0].id, claimed[3]).reserve(
        call_id, 7, CallSpec("semantic", "deepseek-v4-flash", "initial"))
    with database() as session:
        session.get(BrainUnit, trial.units[0]).lease_until = utcnow() - timedelta(seconds=1)
        session.commit()

    assert claim_unit(trial.units[0]) is None
    with database() as session:
        build = session.get(BrainBuild, trial.build_id)
        unit = session.get(BrainUnit, trial.units[0])
        assert build.status == "failed" and build.completed_at is not None
        assert build.reserved_tokens == 7 and build.total_prompt_tokens == build.total_completion_tokens == 0
        assert unit.status == "failed" and unit.attempts == 1
        assert unit.lease_token is None and unit.lease_until is None
        assert unit.payload["_unknown_tokens"] == 7 and unit.payload["_reserved_tokens"] == 0
        assert session.get(BrainModelCall, call_id).state == "unknown"
        assert "failure_code=model_calls_incomplete" in build.error_message


def test_uncertain_handoff_during_pause_reports_the_persisted_failed_outcome(database, monkeypatch):
    """A fault during drain cannot report a normal deferred/paused result."""
    trial = seed(database, "compile")
    delivered, retry = MagicMock(), MagicMock(side_effect=AssertionError("unexpected_retry"))
    monkeypatch.setattr(brain_task.execute_brain_unit, "apply_async", delivered)
    monkeypatch.setattr(brain_task.execute_brain_unit, "retry", retry)
    call_id = str(uuid4())

    async def execute(unit, build, project, token):
        assert pause(database, build.id) == "pausing"
        BuildCallMeter(build.id, unit.id, token).reserve(
            call_id, 7, CallSpec("semantic", "deepseek-v4-flash", "initial"))
        raise PrivacyScanDeferred()

    monkeypatch.setattr(brain_task, "_execute", execute)
    assert brain_task.execute_brain_unit.run(trial.units[0]) == {"status": "failed"}
    delivered.assert_not_called()
    retry.assert_not_called()
    with database() as session:
        build = session.get(BrainBuild, trial.build_id)
        assert build.status == "failed" and build.execution_state == "running"
        assert build.reserved_tokens == 7
        assert session.get(BrainModelCall, call_id).state == "unknown"
        assert "failure_code=model_calls_incomplete" in build.error_message
