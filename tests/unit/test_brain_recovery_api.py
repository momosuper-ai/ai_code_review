"""Recovery APIs create a new lineage without reclassifying the failed ledger."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import func, select

from src.api.v1 import brain as api
from src.config import settings
from src.models import get_session
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainRecovery, BrainUnit, utcnow
from tests.unit.test_project_brain import AsyncSessionAdapter, database  # noqa: F401


PRIVATE = "fixture-private-recovery-value"
SHA = "a" * 40


class Adapter(AsyncSessionAdapter):
    async def execute(self, statement):
        return self.session.execute(statement)

    async def rollback(self):
        self.session.rollback()


@pytest.fixture
def trial(database, monkeypatch):
    BrainRecovery.__table__.create(database.kw["bind"], checkfirst=True)
    monkeypatch.setattr(settings, "API_KEYS", "fixture-api-key")
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    dispatch = MagicMock()
    monkeypatch.setattr(api, "dispatch_pending", dispatch)
    # Recovery knows the original full SHA; planning/registration must neither
    # resolve the branch anew nor construct a model client or ask a model.
    monkeypatch.setattr(api, "GitLabClient", MagicMock(side_effect=AssertionError("unexpected_gitlab_io")))
    monkeypatch.setattr("src.core.llm_client.LLMClient.chat", AsyncMock(side_effect=AssertionError("unexpected_model_io")))
    with database() as session:
        project = BrainProject(gitlab_project_id=1, project_name=PRIVATE, branch="main",
            auto_update=False, gitlab_sync=False, maintenance_state="paused", request_seq=4,
            source_status="available", last_observed_head="f" * 40, next_version=2)
        session.add(project)
        session.flush()
        parent = BrainBuild(project_id=project.id, source_commit=SHA, recipe_hash="b" * 64,
            mode="full", historical=True, request_seq=3, brain_version=1, status="failed",
            coverage={"public_schema_version": "2.0", "issues": [PRIVATE]},
            error_message=f"{PRIVATE};failure_code=model_calls_incomplete", reserved_tokens=18,
            total_prompt_tokens=10, total_completion_tokens=1, completed_at=utcnow())
        session.add(parent)
        session.flush()
        units = []
        for key, kind, state in (
            ("scan", "scan", "completed"), ("extract", "extract", "completed"),
            ("privacy:1", "privacy", "completed"), ("privacy:2", "privacy", "completed"),
            ("source_privacy:1", "source_privacy", "completed"), ("finalize", "finalize", "completed"),
            ("compile:1", "compile", "completed"), ("compile:2", "compile", "failed"),
        ):
            unit = BrainUnit(build_id=parent.id, key=key, kind=kind, status=state,
                input_hash="c" * 64, payload={"private_context": PRIVATE},
                result={"private_result": PRIVATE} if state == "completed" else None,
                error_message=PRIVATE if state == "failed" else None)
            session.add(unit)
            units.append(unit)
        session.flush()
        for state, bound, prompt, completion in (("settled", 11, 10, 1), ("unknown", 7, 0, 0)):
            session.add(BrainModelCall(id=str(uuid4()), build_id=parent.id, unit_id=units[-1].id,
                lease_token=str(uuid4()), stage="compile", role="privacy", purpose="semantic_output",
                requested_model=PRIVATE, attempt=1, state=state, reserved_tokens=bound,
                prompt_tokens=prompt, completion_tokens=completion))
        session.commit()
        parent_id, project_id, failed_unit = parent.id, project.id, units[-1].id
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")

    async def dependency():
        async with Adapter(database) as adapter:
            yield adapter

    app.dependency_overrides[get_session] = dependency
    return SimpleNamespace(app=app, factory=database, parent_id=parent_id, project_id=project_id,
                           failed_unit=failed_unit, dispatch=dispatch,
                           root=f"/api/v1/projects/1/brain/builds/{parent_id}")


def client(trial, *, authorized=True):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=trial.app), base_url="http://test",
                             headers={"X-API-Key": "fixture-api-key"} if authorized else {})


def parent_snapshot(trial):
    with trial.factory() as session:
        parent = session.get(BrainBuild, trial.parent_id)
        units = session.scalars(select(BrainUnit).where(BrainUnit.build_id == parent.id).order_by(BrainUnit.id)).all()
        calls = session.scalars(select(BrainModelCall).where(BrainModelCall.build_id == parent.id).order_by(BrainModelCall.id)).all()
        return deepcopy({"build": {column.name: getattr(parent, column.name) for column in BrainBuild.__table__.columns},
            "units": [{column.name: getattr(unit, column.name) for column in BrainUnit.__table__.columns} for unit in units],
            "calls": [{column.name: getattr(call, column.name) for column in BrainModelCall.__table__.columns} for call in calls]})


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,method", [("recovery-plan", "get"), ("recovery", "post")])
async def test_recovery_requires_shared_api_key(trial, suffix, method):
    async with client(trial, authorized=False) as http:
        response = await getattr(http, method)(trial.root + "/" + suffix)
    assert response.status_code == 401
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,method", [("recovery-plan", "get"), ("recovery", "post")])
async def test_recovery_cannot_cross_gitlab_project_scope(trial, suffix, method):
    async with client(trial) as http:
        response = await getattr(http, method)(trial.root.replace("/projects/1/", "/projects/2/") + "/" + suffix)
    assert response.status_code == 404
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_plan_is_read_only_candidate_metadata_without_private_contents(trial):
    before = parent_snapshot(trial)
    async with client(trial) as http:
        response = await http.get(trial.root + "/recovery-plan")
    assert response.status_code == 200
    data = response.json()
    assert data["parent_build_id"] == trial.parent_id
    assert data["source_commit"] == SHA
    assert data["state"] == "candidate_only"
    assert data["completed_units_by_stage"]["privacy"] == 2
    assert data["completed_units_by_stage"]["source_privacy"] == 1
    assert data["completed_units_by_stage"]["compile"] == 1
    assert PRIVATE not in json.dumps(data)
    assert parent_snapshot(trial) == before
    with trial.factory() as session:
        assert session.scalar(select(func.count()).select_from(BrainRecovery)) == 0
        assert session.scalar(select(func.count()).select_from(BrainBuild)) == 1
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, {}])
async def test_create_new_child_preserves_parent_unknown_costs_and_paused_project(trial, body):
    before = parent_snapshot(trial)
    async with client(trial) as http:
        response = await http.post(trial.root + "/recovery", **({"json": body} if body is not None else {}))
    assert response.status_code == 202
    data = response.json()
    child_id = data["build_id"]
    assert child_id != trial.parent_id
    assert PRIVATE not in response.text
    assert data["url"] == f"/api/v1/projects/1/brain/builds/{child_id}"
    assert data["historical"] is True
    assert parent_snapshot(trial) == before
    with trial.factory() as session:
        parent = session.get(BrainBuild, trial.parent_id)
        child = session.get(BrainBuild, child_id)
        lineage = session.get(BrainRecovery, child_id)
        project = session.get(BrainProject, trial.project_id)
        assert child.project_id == parent.project_id and child.source_commit == parent.source_commit
        assert child.mode == "full" and child.historical
        assert child.status == "pending"
        assert child.recipe_hash != parent.recipe_hash
        assert child.reserved_tokens == child.total_prompt_tokens == child.total_completion_tokens == child.total_cached_tokens == 0
        assert not child.usage_estimated
        assert child.artifact_path is None
        assert lineage.parent_build_id == parent.id and lineage.child_build_id == child.id
        assert lineage.status == "pending"
        assert session.scalar(select(func.count()).select_from(BrainModelCall).where(BrainModelCall.build_id == child.id)) == 0
        assert project.maintenance_state == "paused" and not project.auto_update and not project.gitlab_sync
        assert project.active_build_id is None
        # A stale branch HEAD must not replace the failed build's fixed source.
        assert project.last_observed_head == "f" * 40 and child.source_commit == SHA
    trial.dispatch.assert_called_once_with(child_id)


@pytest.mark.asyncio
async def test_nonhistorical_parent_recovers_as_fixed_sha_history_without_branch_promotion(trial):
    with trial.factory() as session:
        session.get(BrainBuild, trial.parent_id).historical = False
        session.commit()
    before = parent_snapshot(trial)
    async with client(trial) as http:
        response = await http.post(trial.root + "/recovery", json={})
    assert response.status_code == 202
    assert response.json()["historical"] is True
    with trial.factory() as session:
        child = session.get(BrainBuild, response.json()["build_id"])
        assert child.historical and child.source_commit == SHA
        project = session.get(BrainProject, trial.project_id)
        assert project.active_build_id is None
        assert project.maintenance_state == "paused" and not project.gitlab_sync
        assert project.last_observed_head == "f" * 40
    assert parent_snapshot(trial) == before


@pytest.mark.asyncio
async def test_broker_failure_keeps_one_durable_pending_child_and_returns_accepted(trial):
    trial.dispatch.side_effect = RuntimeError(PRIVATE)
    before = parent_snapshot(trial)
    async with client(trial) as http:
        first = await http.post(trial.root + "/recovery", json={})
        second = await http.post(trial.root + "/recovery")
    assert first.status_code == second.status_code == 202
    assert first.json()["build_id"] == second.json()["build_id"]
    assert PRIVATE not in first.text and PRIVATE not in second.text
    with trial.factory() as session:
        assert session.scalar(select(func.count()).select_from(BrainRecovery)) == 1
        child = session.get(BrainBuild, first.json()["build_id"])
        assert child.status == "pending"
    assert parent_snapshot(trial) == before


@pytest.mark.asyncio
async def test_duplicate_request_reuses_one_active_recovery_child(trial):
    before = parent_snapshot(trial)
    async with client(trial) as http:
        first = await http.post(trial.root + "/recovery", json={})
        second = await http.post(trial.root + "/recovery")
    assert first.status_code == second.status_code == 202
    assert first.json()["build_id"] == second.json()["build_id"]
    with trial.factory() as session:
        assert session.scalar(select(func.count()).select_from(BrainRecovery)) == 1
        assert session.scalar(select(func.count()).select_from(BrainBuild)) == 2
    assert parent_snapshot(trial) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("project_id", 2), ("branch", "other"), ("commit_sha", "d" * 40), ("source_commit", "d" * 40),
    ("repo_url", "https://example.test/other.git"), ("artifact_path", "../../outside"),
    ("snapshot_path", "C:/outside"), ("parent_build_id", str(uuid4())),
    ("max_tokens", 100_000_000), ("gitlab_sync", True),
])
async def test_recovery_body_cannot_override_fixed_identity_paths_or_budget(trial, field, value):
    async with client(trial) as http:
        response = await http.post(trial.root + "/recovery", json={field: value})
    assert response.status_code == 422
    with trial.factory() as session:
        assert session.scalar(select(func.count()).select_from(BrainRecovery)) == 0
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_registration_requires_feature_enabled_but_preview_remains_readable(trial, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_ENABLED", False)
    async with client(trial) as http:
        preview = await http.get(trial.root + "/recovery-plan")
        creation = await http.post(trial.root + "/recovery", json={})
    assert preview.status_code == 200
    assert creation.status_code == 503
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "extracting", "compiling", "validating", "ready", "superseded"])
async def test_only_failed_parent_can_create_recovery(trial, status):
    with trial.factory() as session:
        session.get(BrainBuild, trial.parent_id).status = status
        session.commit()
    async with client(trial) as http:
        response = await http.post(trial.root + "/recovery", json={})
    assert response.status_code == 409
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("coverage", [{}, {"public_schema_version": "1.0"}, {"public_schema_version": 2}])
async def test_legacy_or_unrecognized_privacy_protocol_cannot_be_recovered(trial, coverage):
    with trial.factory() as session:
        session.get(BrainBuild, trial.parent_id).coverage = coverage
        session.commit()
    async with client(trial) as http:
        response = await http.post(trial.root + "/recovery", json={})
    assert response.status_code == 409
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["privacy_seal_conflict", "known_privacy_regression"])
async def test_safety_isolated_parent_cannot_donate_checkpoints(trial, code):
    with trial.factory() as session:
        session.get(BrainBuild, trial.parent_id).error_message = f"{PRIVATE};failure_code={code}"
        session.commit()
    async with client(trial) as http:
        response = await http.post(trial.root + "/recovery", json={})
    assert response.status_code == 409
    assert PRIVATE not in response.text
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,lease_delta", [("running", 60), ("running", -60), ("failed", 60), ("completed", 60)])
async def test_parent_with_running_unit_or_active_lease_cannot_be_recovered(trial, status, lease_delta):
    with trial.factory() as session:
        unit = session.get(BrainUnit, trial.failed_unit)
        unit.status, unit.lease_token = status, str(uuid4())
        unit.lease_until = utcnow() + timedelta(seconds=lease_delta)
        session.commit()
    async with client(trial) as http:
        response = await http.post(trial.root + "/recovery", json={})
    assert response.status_code == 409
    trial.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_parent_with_unresolved_inflight_reservation_must_settle_or_abandon_first(trial):
    with trial.factory() as session:
        unknown = session.scalar(select(BrainModelCall).where(BrainModelCall.build_id == trial.parent_id,
                                                             BrainModelCall.state == "unknown"))
        unknown.state = "reserved"
        session.commit()
    async with client(trial) as http:
        response = await http.post(trial.root + "/recovery", json={})
    assert response.status_code == 409
    trial.dispatch.assert_not_called()
