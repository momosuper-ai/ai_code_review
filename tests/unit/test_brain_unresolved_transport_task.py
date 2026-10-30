"""Lost billed responses stop the task; a fresh retry cannot recover them."""

from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from celery.exceptions import Retry
from openai import APITimeoutError
from sqlalchemy import select

from src.core.llm_client import TokenUsage
from src.core.project_brain.usage import BuildCallMeter, CallSpec
from src.models.brain import BrainBuild, BrainModelCall, BrainUnit
from src.tasks.brain_task import _abandon_live_reservation, execute_brain_unit
from tests.unit.test_project_brain import database  # noqa: F401


def work(database):
    from src.core.project_brain.lifecycle import create_build

    with database() as session:
        build = create_build(session, 1, "main", "fixture/project", "a" * 40)
        current = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        current.kind = "privacy"
        sibling = BrainUnit(build_id=build.id, key="waiting", kind="privacy", status="pending")
        completed = BrainUnit(build_id=build.id, key="finished", kind="privacy", status="completed",
                              result={"checkpoint": "preserved"})
        session.add_all([sibling, completed])
        session.commit()
        return build.id, current.id, sibling.id, completed.id


@pytest.mark.parametrize("failure", [
    APITimeoutError(request=httpx.Request("POST", "https://gateway.invalid/secret-path")),
    TimeoutError("synthetic-private-timeout"),
    RuntimeError("synthetic-private-provider-body"),
])
def test_lost_response_stops_first_attempt_and_preserves_accounting(database, monkeypatch, failure):
    build_id, unit_id, sibling_id, completed_id = work(database)
    call_id = str(uuid4())
    meters = []

    async def fail(unit, build, project, token):
        meter = BuildCallMeter(build.id, unit.id, token)
        meters.append(meter)
        meter.reserve(call_id, 100, CallSpec("privacy", "local_model", "source_excerpt"))
        raise failure

    monkeypatch.setattr("src.tasks.brain_task._execute", fail)
    retry, dispatch = MagicMock(), MagicMock()
    monkeypatch.setattr(execute_brain_unit, "retry", retry)
    monkeypatch.setattr("src.tasks.brain_task.dispatch_pending", dispatch)
    assert execute_brain_unit.run(unit_id) == {"status": "failed"}
    retry.assert_not_called()
    dispatch.assert_not_called()
    with database() as session:
        build, unit = session.get(BrainBuild, build_id), session.get(BrainUnit, unit_id)
        assert build.status == "failed" and build.completed_at is not None
        assert build.reserved_tokens == 100 and build.total_prompt_tokens == build.total_completion_tokens == 0
        assert unit.status == "failed" and unit.attempts == 1 and unit.lease_token is None
        assert unit.payload["_reserved_tokens"] == 0 and unit.payload["_unknown_tokens"] == 100
        assert session.get(BrainModelCall, call_id).state == "unknown"
        assert session.get(BrainUnit, sibling_id).status == "failed"
        assert session.get(BrainUnit, completed_id).result == {"checkpoint": "preserved"}
        assert "failure_code=model_calls_incomplete" in build.error_message
        assert all(private not in build.error_message for private in
                   ("secret-path", "synthetic-private", "gateway.invalid"))
    # Genuine settlement of this exact UUID is still accepted after failure;
    # it corrects the bill, and never makes a failed build ready.
    meters[0].settle(call_id, TokenUsage(20, 2), False, 100, None)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        assert build.status == "failed" and build.reserved_tokens == 22
        assert build.total_prompt_tokens + build.total_completion_tokens == 22
        assert session.get(BrainUnit, unit_id).payload["_unknown_tokens"] == 0
        assert session.get(BrainModelCall, call_id).state == "settled"


@pytest.mark.parametrize("settled", [False, True])
def test_failure_without_lost_response_retains_normal_retry(database, monkeypatch, settled):
    build_id, unit_id, sibling_id, _ = work(database)

    async def fail(unit, build, project, token):
        if settled:
            meter, call_id = BuildCallMeter(build.id, unit.id, token), str(uuid4())
            meter.reserve(call_id, 100, CallSpec("privacy", "local_model", "generic"))
            meter.settle(call_id, TokenUsage(20, 2), False, 100, None)
        raise RuntimeError("synthetic-private-error")

    monkeypatch.setattr("src.tasks.brain_task._execute", fail)
    retry = MagicMock(side_effect=Retry())
    monkeypatch.setattr(execute_brain_unit, "retry", retry)
    with pytest.raises(Retry):
        execute_brain_unit.run(unit_id)
    retry.assert_called_once()
    with database() as session:
        build, unit = session.get(BrainBuild, build_id), session.get(BrainUnit, unit_id)
        assert build.status == "extracting" and build.completed_at is None
        assert build.reserved_tokens == (22 if settled else 0)
        assert unit.status == "pending" and "model_calls_incomplete" not in unit.error_message
        assert session.get(BrainUnit, sibling_id).status == "pending"


@pytest.mark.parametrize("same_unit", [False, True])
def test_prior_unknown_stops_admission_without_rebilling_other_work(database, monkeypatch, same_unit):
    from src.core.project_brain.lifecycle import claim_unit

    build_id, unit_id, sibling_id, completed_id = work(database)
    old_id = str(uuid4())
    claimed = claim_unit(unit_id if same_unit else sibling_id)
    assert claimed is not None
    old_unit_id, old_token = claimed[0].id, claimed[3]
    meter = BuildCallMeter(build_id, old_unit_id, old_token)
    meter.reserve(old_id, 80, CallSpec("privacy", "local_model", "generic"))
    with database() as session:
        old_unit = session.get(BrainUnit, old_unit_id)
        _abandon_live_reservation(old_unit, session)
        old_unit.status, old_unit.lease_token, old_unit.lease_until = "pending", None, None
        session.commit()

    execute, retry = MagicMock(), MagicMock()
    monkeypatch.setattr("src.tasks.brain_task._execute", execute)
    monkeypatch.setattr(execute_brain_unit, "retry", retry)
    assert execute_brain_unit.run(unit_id) == {"status": "skipped"}
    execute.assert_not_called()
    retry.assert_not_called()
    with database() as session:
        build = session.get(BrainBuild, build_id)
        call = session.get(BrainModelCall, old_id)
        assert build.status == "failed" and build.reserved_tokens == 80
        assert call.state == "unknown" and call.lease_token == old_token
        assert session.get(BrainUnit, completed_id).result == {"checkpoint": "preserved"}
        assert session.get(BrainUnit, unit_id).attempts == (1 if same_unit else 0)
    # A real late response corrects only its original bill, never admission.
    meter.settle(old_id, TokenUsage(20, 2), False, 80, None)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        assert build.status == "failed" and build.reserved_tokens == 22
        assert session.get(BrainModelCall, old_id).state == "settled"
