"""Execution control races against real PostgreSQL row locks, without models."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select

from src.core.llm_client import TokenUsage
from src.core.project_brain.execution import pause_build, resume_build
from src.core.project_brain.lifecycle import claim_unit, create_build, reserve_tokens
from src.core.project_brain.usage import BuildCallMeter, CallSpec
from src.models import SyncSession, sync_engine
from src.models.brain import BrainBuild, BrainProject, BrainUnit
from src.tasks.brain_task import _complete
from tests.integration.test_brain_postgres import pg_project  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get("BRAIN_LOCAL_TEST") != "1",
                              reason="仅在显式开启的独立 PostgreSQL schema 执行")


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    forbidden = AsyncMock(side_effect=AssertionError("execution control must not invoke models or GitLab"))
    monkeypatch.setattr("src.core.llm_client.LLMClient.chat", forbidden)
    monkeypatch.setattr("src.core.gitlab_client.GitLabClient.get_project", forbidden)
    monkeypatch.setattr("src.tasks.brain_task.dispatch_pending", lambda *args: None)


def _trial(pg_project):
    with SyncSession() as session:
        build = create_build(session, pg_project, "main", "synthetic/execution", "a" * 40,
                             mode="full", historical=True)
        session.commit()
        unit = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        return build.id, unit.id


def _pause(build_id):
    with SyncSession() as session:
        build = pause_build(session, build_id)
        session.commit()
        return build.execution_state, build.pause_requested_at, build.paused_at


def _resume(build_id):
    with SyncSession() as session:
        build = resume_build(session, build_id)
        session.commit()
        return build.execution_state, build.resumed_at


def test_postgres_execution_columns_have_an_old_row_default_and_state_constraint(pg_project):
    inspector = inspect(sync_engine)
    columns = {column["name"]: column for column in inspector.get_columns("brain_builds")}
    assert {"execution_state", "pause_requested_at", "paused_at", "resumed_at"} <= columns.keys()
    assert columns["execution_state"]["nullable"] is False
    assert "running" in columns["execution_state"]["default"]
    constraints = inspector.get_check_constraints("brain_builds")
    assert any(all(state in check["sqltext"] for state in ("running", "pausing", "paused")) for check in constraints)


def test_postgres_claim_and_pause_have_one_serialized_admission_boundary(pg_project):
    build_id, unit_id = _trial(pg_project)
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=10)
        return claim_unit(unit_id)

    def pause():
        barrier.wait(timeout=10)
        return _pause(build_id)

    with ThreadPoolExecutor(max_workers=2) as workers:
        claim_future, pause_future = workers.submit(claim), workers.submit(pause)
        claimed, paused = claim_future.result(timeout=20), pause_future.result(timeout=20)
    assert paused[0] == ("pausing" if claimed else "paused")
    assert claim_unit(unit_id) is None
    if claimed:
        assert _complete(unit_id, claimed[3], {"fingerprint": "synthetic"})
    with SyncSession() as session:
        build = session.get(BrainBuild, build_id)
        assert build.execution_state == "paused"
        assert not session.scalars(select(BrainUnit).where(
            BrainUnit.build_id == build_id, BrainUnit.status == "running")).all()


def test_postgres_final_checkpoint_and_pause_cannot_drop_the_next_stage(pg_project):
    build_id, unit_id = _trial(pg_project)
    claimed = claim_unit(unit_id)
    assert claimed is not None
    barrier = Barrier(2)

    def complete():
        barrier.wait(timeout=10)
        return _complete(unit_id, claimed[3], {"fingerprint": "synthetic"})

    def pause():
        barrier.wait(timeout=10)
        return _pause(build_id)

    with ThreadPoolExecutor(max_workers=2) as workers:
        completed, pause_result = workers.submit(complete), workers.submit(pause)
        assert completed.result(timeout=20)
        pause_result.result(timeout=20)
    with SyncSession() as session:
        build = session.get(BrainBuild, build_id)
        units = session.scalars(select(BrainUnit).where(BrainUnit.build_id == build_id)).all()
        assert build.execution_state == "paused" and build.paused_at is not None
        assert [(unit.kind, unit.status) for unit in units] == [("scan", "completed"), ("extract", "pending")]
    _resume(build_id)
    with SyncSession() as session:
        extraction = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build_id, BrainUnit.kind == "extract"))
        next_id = extraction.id
    assert claim_unit(next_id) is not None


def test_postgres_parallel_pause_and_resume_are_idempotent(pg_project):
    build_id, _ = _trial(pg_project)
    barrier = Barrier(4)

    def pause(_):
        barrier.wait(timeout=10)
        return _pause(build_id)

    with ThreadPoolExecutor(max_workers=4) as workers:
        pauses = list(workers.map(pause, range(4)))
    assert len(set(pauses)) == 1 and pauses[0][0] == "paused"
    barrier = Barrier(4)

    def resume(_):
        barrier.wait(timeout=10)
        return _resume(build_id)

    with ThreadPoolExecutor(max_workers=4) as workers:
        resumes = list(workers.map(resume, range(4)))
    assert len(set(resumes)) == 1 and resumes[0][0] == "running"
    with SyncSession() as session:
        build = session.get(BrainBuild, build_id)
        project = session.get(BrainProject, build.project_id)
        assert build.total_prompt_tokens == build.total_completion_tokens == build.reserved_tokens == 0
        assert build.historical and project.maintenance_state == "unmanaged"
        assert session.scalar(select(BrainUnit).where(BrainUnit.build_id == build_id)).attempts == 0


def test_postgres_pause_admits_existing_leases_until_their_safe_checkpoint(pg_project):
    build_id, unit_id = _trial(pg_project)
    claimed = claim_unit(unit_id)
    assert claimed is not None
    token = claimed[3]
    call_id = str(uuid4())
    spec = CallSpec("privacy", "local_model", "generic", transport_group=str(uuid4()), transport_attempt=1)
    barrier = Barrier(2)

    def reserve():
        barrier.wait(timeout=10)
        reserve_tokens(build_id, token, unit_id, 17, call_id=call_id, call_spec=spec)

    def pause():
        barrier.wait(timeout=10)
        return _pause(build_id)

    with ThreadPoolExecutor(max_workers=2) as workers:
        reservation, pause_result = workers.submit(reserve), workers.submit(pause)
        reservation.result(timeout=20)
        assert pause_result.result(timeout=20)[0] == "pausing"
    BuildCallMeter(build_id, unit_id, token).settle(call_id, TokenUsage(3, 2), False, 17, "local_model")
    assert _complete(unit_id, token, {"fingerprint": "synthetic"})
    with SyncSession() as session:
        build = session.get(BrainBuild, build_id)
        assert build.execution_state == "paused" and build.reserved_tokens == 5
        assert (build.total_prompt_tokens, build.total_completion_tokens) == (3, 2)
    assert _resume(build_id)[0] == "running"
