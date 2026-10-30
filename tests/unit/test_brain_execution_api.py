"""Explicit build control preserves fixed checkpoints, stage, and accounting."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import select

from src.api.v1 import brain as api
from src.config import settings
from src.core.project_brain.lifecycle import recipe_hash
from src.models import get_session
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainUnit, utcnow
from tests.unit.test_project_brain import AsyncSessionAdapter, database  # noqa: F401


PRIVATE = "fixture-private-control-value"
CONTROL_FIELDS = {"execution_state", "pause_requested_at", "paused_at", "resumed_at"}


class Adapter(AsyncSessionAdapter):
    async def rollback(self):
        self.session.rollback()


@pytest.fixture
def trial(database, monkeypatch):
    monkeypatch.setattr(settings, "API_KEYS", "fixture-api-key")
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    dispatch = MagicMock()
    monkeypatch.setattr(api, "dispatch_pending", dispatch)
    monkeypatch.setattr(api, "GitLabClient", MagicMock(side_effect=AssertionError("unexpected_gitlab_io")))
    monkeypatch.setattr("src.core.llm_client.LLMClient.chat", AsyncMock(side_effect=AssertionError("unexpected_model_io")))
    with database() as session:
        project = BrainProject(gitlab_project_id=1, project_name=PRIVATE, branch="main",
            auto_update=False, gitlab_sync=False, maintenance_state="paused", request_seq=4,
            source_status="available", last_observed_head="f" * 40, next_version=2)
        session.add(project)
        session.flush()
        build = BrainBuild(project_id=project.id, source_commit="a" * 40, recipe_hash=recipe_hash(),
            mode="full", historical=True, request_seq=3, brain_version=1, status="compiling",
            coverage={"public_schema_version": "2.0", "issues": [PRIVATE]}, reserved_tokens=11,
            total_prompt_tokens=10, total_completion_tokens=1)
        session.add(build)
        session.flush()
        completed = BrainUnit(build_id=build.id, key="compile:completed", kind="compile", status="completed",
            input_hash="c" * 64, payload={"private_payload": PRIVATE}, result={"private_result": PRIVATE}, attempts=1)
        pending = BrainUnit(build_id=build.id, key="compile:pending", kind="compile", status="pending",
            input_hash="d" * 64, payload={"private_payload": PRIVATE}, dispatched_at=utcnow(), attempts=0)
        session.add_all([completed, pending])
        session.flush()
        session.add(BrainModelCall(id=str(uuid4()), build_id=build.id, unit_id=completed.id,
            lease_token=str(uuid4()), stage="compile", role="semantic", purpose="initial",
            requested_model="deepseek-v4-flash", attempt=1, state="settled", reserved_tokens=20,
            prompt_tokens=10, completion_tokens=1, settled_at=utcnow()))
        session.commit()
        identity = build.id, project.id, completed.id, pending.id
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")

    async def dependency():
        async with Adapter(database) as adapter:
            yield adapter

    app.dependency_overrides[get_session] = dependency
    return SimpleNamespace(app=app, factory=database, build_id=identity[0], project_id=identity[1],
        completed_id=identity[2], pending_id=identity[3], dispatch=dispatch,
        root=f"/api/v1/projects/1/brain/builds/{identity[0]}")


def client(trial, *, authorized=True):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=trial.app), base_url="http://test",
        headers={"X-API-Key": "fixture-api-key"} if authorized else {})


def preserved_snapshot(trial):
    with trial.factory() as session:
        build = session.get(BrainBuild, trial.build_id)
        project = session.get(BrainProject, trial.project_id)
        units = session.scalars(select(BrainUnit).where(BrainUnit.build_id == build.id).order_by(BrainUnit.id)).all()
        calls = session.scalars(select(BrainModelCall).where(BrainModelCall.build_id == build.id).order_by(BrainModelCall.id)).all()
        return deepcopy({
            "build": {column.name: getattr(build, column.name) for column in BrainBuild.__table__.columns
                      if column.name not in CONTROL_FIELDS},
            "project": {column.name: getattr(project, column.name) for column in BrainProject.__table__.columns},
            "units": [{column.name: getattr(unit, column.name) for column in BrainUnit.__table__.columns
                       if column.name != "dispatched_at"} for unit in units],
            "calls": [{column.name: getattr(call, column.name) for column in BrainModelCall.__table__.columns}
                      for call in calls],
        })


def set_execution(trial, state):
    with trial.factory() as session:
        build = session.get(BrainBuild, trial.build_id)
        build.execution_state = state
        if state != "running":
            build.pause_requested_at = utcnow() - timedelta(seconds=10)
        if state == "paused":
            build.paused_at = utcnow() - timedelta(seconds=5)
        session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["pause", "resume"])
async def test_control_requires_api_key_and_project_scope(trial, suffix):
    async with client(trial, authorized=False) as http:
        unauthorized = await http.post(trial.root + "/" + suffix)
    async with client(trial) as http:
        foreign = await http.post(trial.root.replace("/projects/1/", "/projects/2/") + "/" + suffix)
    assert unauthorized.status_code == 401
    assert foreign.status_code == 404
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["pause", "resume"])
@pytest.mark.parametrize("field,value", [("commit_sha", "b" * 40), ("max_tokens", 100_000_000),
                                         ("gitlab_sync", True), ("model", PRIVATE), ("privacy", False)])
async def test_control_body_cannot_change_fixed_source_budget_or_privacy(trial, suffix, field, value):
    before = preserved_snapshot(trial)
    async with client(trial) as http:
        response = await http.post(trial.root + "/" + suffix, json={field: value})
    assert response.status_code == 422
    assert preserved_snapshot(trial) == before
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, {}])
async def test_pause_is_durable_idempotent_and_preserves_checkpoints_and_ledger(trial, body):
    before = preserved_snapshot(trial)
    async with client(trial) as http:
        first = await http.post(trial.root + "/pause", **({"json": body} if body is not None else {}))
        second = await http.post(trial.root + "/pause")
        current = await http.get(trial.root)
    assert first.status_code == second.status_code == 202
    assert current.status_code == 200
    data = first.json()
    assert data["id"] == trial.build_id and data["status"] == "compiling"
    assert data["execution_state"] == second.json()["execution_state"] == current.json()["execution_state"] == "paused"
    assert data["pause_requested_at"] and data["paused_at"]
    assert data["pause_requested_at"] == second.json()["pause_requested_at"]
    assert data["paused_at"] == second.json()["paused_at"]
    assert PRIVATE not in first.text + second.text + current.text
    assert preserved_snapshot(trial) == before
    with trial.factory() as session:
        assert session.get(BrainUnit, trial.pending_id).dispatched_at is None
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_active_unit_drains_before_paused_and_resume_is_rejected_until_then(trial):
    with trial.factory() as session:
        pending = session.get(BrainUnit, trial.pending_id)
        pending.status, pending.lease_token = "running", str(uuid4())
        pending.lease_until = utcnow() + timedelta(seconds=60)
        pending.attempts = 1
        session.commit()
    before = preserved_snapshot(trial)
    async with client(trial) as http:
        pausing = await http.post(trial.root + "/pause")
        resume = await http.post(trial.root + "/resume")
    assert pausing.status_code == 202 and pausing.json()["execution_state"] == "pausing"
    assert pausing.json()["paused_at"] is None
    assert resume.status_code == 409
    assert preserved_snapshot(trial) == before
    with trial.factory() as session:
        pending = session.get(BrainUnit, trial.pending_id)
        pending.status, pending.lease_token, pending.lease_until = "completed", None, None
        pending.result = {"checkpoint": "completed-during-drain"}
        session.commit()
    async with client(trial) as http:
        paused = await http.post(trial.root + "/pause")
    assert paused.status_code == 202 and paused.json()["execution_state"] == "paused"
    assert paused.json()["pause_requested_at"] == pausing.json()["pause_requested_at"]
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_resume_keeps_one_build_and_bills_and_dispatches_only_once(trial):
    set_execution(trial, "paused")
    before = preserved_snapshot(trial)
    async with client(trial) as http:
        first = await http.post(trial.root + "/resume", json={})
        second = await http.post(trial.root + "/resume")
    assert first.status_code == second.status_code == 202
    assert first.json()["execution_state"] == second.json()["execution_state"] == "running"
    assert first.json()["resumed_at"] and first.json()["resumed_at"] == second.json()["resumed_at"]
    assert first.json()["status"] == "compiling"
    assert PRIVATE not in first.text + second.text
    assert preserved_snapshot(trial) == before
    with trial.factory() as session:
        assert session.get(BrainUnit, trial.pending_id).dispatched_at is None
    trial.dispatch.assert_called_once_with(trial.build_id)


@pytest.mark.asyncio
async def test_broker_failure_retains_running_intent_and_reports_accepted(trial):
    set_execution(trial, "paused")
    before = preserved_snapshot(trial)
    trial.dispatch.side_effect = RuntimeError(PRIVATE)
    async with client(trial) as http:
        response = await http.post(trial.root + "/resume")
    assert response.status_code == 202 and response.json()["execution_state"] == "running"
    assert preserved_snapshot(trial) == before
    assert PRIVATE not in response.text


@pytest.mark.asyncio
async def test_resume_dispatch_decision_observes_pause_committed_after_scope_read(trial, monkeypatch):
    original = api._build

    async def pause_after_scope_read(session, project_id, build_id):
        observed = await original(session, project_id, build_id)
        assert observed.execution_state == "running"
        set_execution(trial, "paused")
        return observed

    monkeypatch.setattr(api, "_build", pause_after_scope_read)
    async with client(trial) as http:
        response = await http.post(trial.root + "/resume")
    assert response.status_code == 202 and response.json()["execution_state"] == "running"
    trial.dispatch.assert_called_once_with(trial.build_id)


@pytest.mark.asyncio
async def test_pause_remains_available_when_generation_disabled_but_resume_does_not(trial, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_ENABLED", False)
    async with client(trial) as http:
        pause = await http.post(trial.root + "/pause")
        resume = await http.post(trial.root + "/resume")
    assert pause.status_code == 202 and pause.json()["execution_state"] == "paused"
    assert resume.status_code == 503
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_resume_rejects_changed_recipe_without_releasing_pause(trial):
    set_execution(trial, "paused")
    with trial.factory() as session:
        session.get(BrainBuild, trial.build_id).recipe_hash = "0" * 64
        session.commit()
    before = preserved_snapshot(trial)
    async with client(trial) as http:
        response = await http.post(trial.root + "/resume")
    assert response.status_code == 409 and PRIVATE not in response.text
    assert preserved_snapshot(trial) == before
    with trial.factory() as session:
        assert session.get(BrainBuild, trial.build_id).execution_state == "paused"
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["pause", "resume"])
@pytest.mark.parametrize("state", ["failed", "ready", "superseded"])
async def test_terminal_builds_cannot_restart_or_pause(trial, suffix, state):
    with trial.factory() as session:
        session.get(BrainBuild, trial.build_id).status = state
        session.commit()
    before = preserved_snapshot(trial)
    async with client(trial) as http:
        response = await http.post(trial.root + "/" + suffix)
    assert response.status_code == 409
    assert preserved_snapshot(trial) == before
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_calls_block_resume_and_preserve_original_charge(trial):
    set_execution(trial, "paused")
    with trial.factory() as session:
        session.add(BrainModelCall(id=str(uuid4()), build_id=trial.build_id, unit_id=trial.pending_id,
            lease_token=str(uuid4()), stage="compile", role="privacy", purpose="semantic_output",
            requested_model=PRIVATE, attempt=1, state="unknown", reserved_tokens=7))
        build = session.get(BrainBuild, trial.build_id)
        build.reserved_tokens += 7
        session.commit()
    before = preserved_snapshot(trial)
    async with client(trial) as http:
        response = await http.post(trial.root + "/resume")
    assert response.status_code == 409 and PRIVATE not in response.text
    assert preserved_snapshot(trial) == before
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_call_does_not_rollback_the_requested_stop_admission_intent(trial):
    call_id = str(uuid4())
    with trial.factory() as session:
        session.add(BrainModelCall(id=call_id, build_id=trial.build_id, unit_id=trial.pending_id,
            lease_token=str(uuid4()), stage="compile", role="privacy", purpose="semantic_output",
            requested_model=PRIVATE, attempt=1, state="unknown", reserved_tokens=7))
        build = session.get(BrainBuild, trial.build_id)
        build.reserved_tokens += 7
        session.commit()
    before = preserved_snapshot(trial)
    async with client(trial) as http:
        response = await http.post(trial.root + "/pause")
    assert response.status_code == 202
    assert response.json()["execution_state"] == "pausing" and response.json()["paused_at"] is None
    assert PRIVATE not in response.text
    assert preserved_snapshot(trial) == before
    with trial.factory() as session:
        build = session.get(BrainBuild, trial.build_id)
        assert build.execution_state == "pausing" and build.pause_requested_at is not None
        assert session.get(BrainModelCall, call_id).state == "unknown"
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_state", ["pausing", "paused"])
async def test_stage_retry_cannot_bypass_execution_control(trial, execution_state):
    set_execution(trial, execution_state)
    with trial.factory() as session:
        session.get(BrainBuild, trial.build_id).status = "failed"
        session.commit()
    before = preserved_snapshot(trial)
    async with client(trial) as http:
        response = await http.post(trial.root + "/retry")
    assert response.status_code == 409
    assert preserved_snapshot(trial) == before
    trial.dispatch.assert_not_called()
