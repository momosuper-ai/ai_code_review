"""Real isolated PostgreSQL recovery registration, gated and model-free.

No test creates a source snapshot, freezes a private parent or starts a Worker.
The existing pg_project fixture enforces the isolated database identity. These
tests run only when the operator explicitly supplies BRAIN_LOCAL_TEST=1.
"""

import asyncio
import copy
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import func, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError

from src.api.v1.brain import retry_build
from src.core.project_brain.lifecycle import create_build
from src.core.project_brain.recovery import ACTIVE, PROTOCOL, create_recovery
from src.models import SyncSession, sync_engine
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainRecovery, BrainUnit, utcnow
from tests.integration.test_brain_postgres import pg_project  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get("BRAIN_LOCAL_TEST") != "1",
                              reason="仅在显式开启的独立 PostgreSQL schema 执行")


@pytest.fixture(autouse=True)
def no_models_gitlab_or_dispatch(monkeypatch):
    forbidden = AsyncMock(side_effect=AssertionError("recovery registration must not call a model or GitLab"))
    monkeypatch.setattr("src.core.llm_client.LLMClient.chat", forbidden)
    monkeypatch.setattr("src.core.gitlab_client.GitLabClient.get_project", forbidden)
    def no_dispatch(*args, **kwargs):
        raise AssertionError("registration test must not dispatch tasks")
    monkeypatch.setattr("src.api.v1.brain.dispatch_pending", no_dispatch)


def _row(model):
    return {column.name: copy.deepcopy(getattr(model, column.name)) for column in model.__table__.columns}


def _account_snapshot(session, build_id):
    build = session.get(BrainBuild, build_id)
    units = session.scalars(select(BrainUnit).where(BrainUnit.build_id == build_id).order_by(BrainUnit.id)).all()
    calls = session.scalars(select(BrainModelCall).where(BrainModelCall.build_id == build_id).order_by(BrainModelCall.id)).all()
    return {"build": _row(build), "units": [_row(unit) for unit in units], "calls": [_row(call) for call in calls]}


def _parent(pg_project, *, unknown=True):
    """All synthetic numeric accounting; no API or private-source material."""
    with SyncSession() as session:
        build = create_build(session, pg_project, "main", "synthetic/recovery", "a" * 40,
                             mode="full", historical=True)
        session.flush()
        project = session.get(BrainProject, build.project_id)
        project.maintenance_state, project.gitlab_sync, project.auto_update = "paused", False, False
        build.status, build.coverage = "failed", {"public_schema_version": "2.0"}
        build.error_message = "failure_code=model_calls_incomplete" if unknown else "failure_code=synthetic_failure"
        first = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        first.key, first.kind, first.status, first.result = "privacy:0", "privacy", "completed", {"records": 1}
        failed = BrainUnit(build_id=build.id, key="compile:0", kind="compile", status="failed",
                           payload={"_reserved_tokens": 0, "_unknown_tokens": 123 if unknown else 0},
                           result=None, error_message="synthetic_failure")
        session.add(failed)
        session.flush()
        if unknown:
            build.total_prompt_tokens, build.total_completion_tokens = 1000, 200
            build.total_cached_tokens, build.reserved_tokens = 50, 1323
            session.add(BrainModelCall(id=str(uuid4()), build_id=build.id, unit_id=first.id,
                lease_token=str(uuid4()), stage="privacy", role="privacy", purpose="generic",
                requested_model="local_model", attempt=1, state="settled", reserved_tokens=6000,
                prompt_tokens=1000, completion_tokens=200, cached_tokens=50, settled_at=utcnow(),
                transport_group=str(uuid4()), transport_attempt=1))
            session.add(BrainModelCall(id=str(uuid4()), build_id=build.id, unit_id=failed.id,
                lease_token=str(uuid4()), stage="compile", role="privacy", purpose="semantic_output",
                requested_model="local_model", attempt=1, state="unknown", reserved_tokens=123,
                transport_group=str(uuid4()), transport_attempt=1))
        session.commit()
        return SimpleNamespace(build_id=build.id, project_id=project.id, next_version=project.next_version)


def test_postgres_concurrent_recovery_registration_has_one_active_child(pg_project):
    parent = _parent(pg_project)
    barrier = Barrier(4)
    def register(_):
        with SyncSession() as session:
            # Force all transactions to start before contending on the same
            # project row. Registration itself owns and releases the row lock.
            session.scalar(select(BrainBuild.id).where(BrainBuild.id == parent.build_id))
            barrier.wait(timeout=10)
            child = create_recovery(session, parent.build_id)
            session.commit()
            return child.id
    with ThreadPoolExecutor(max_workers=4) as workers:
        identities = list(workers.map(register, range(4)))
    assert len(set(identities)) == 1
    with SyncSession() as session:
        children = session.scalars(select(BrainBuild).join(BrainRecovery,
            BrainRecovery.child_build_id == BrainBuild.id).where(
                BrainRecovery.parent_build_id == parent.build_id, BrainBuild.status.in_(ACTIVE))).all()
        assert [child.id for child in children] == [identities[0]]
        assert session.scalar(select(func.count()).select_from(BrainRecovery).where(
            BrainRecovery.parent_build_id == parent.build_id)) == 1
        assert session.scalar(select(func.count()).select_from(BrainUnit).where(
            BrainUnit.build_id == identities[0], BrainUnit.kind == "scan")) == 1
        assert session.get(BrainProject, parent.project_id).next_version == parent.next_version + 1


def test_postgres_recovery_keeps_every_parent_charge_and_unknown_with_original_owner(pg_project):
    parent = _parent(pg_project)
    with SyncSession() as session:
        before = _account_snapshot(session, parent.build_id)
    with SyncSession() as session:
        child = create_recovery(session, parent.build_id)
        child_id = child.id
        session.commit()
    with SyncSession() as session:
        assert _account_snapshot(session, parent.build_id) == before
        child = session.get(BrainBuild, child_id)
        project = session.get(BrainProject, parent.project_id)
        assert child.historical is True and child.source_commit == before["build"]["source_commit"]
        assert child.mode == "full" and child.status == "pending" and child.generation == 1
        assert (child.total_prompt_tokens, child.total_completion_tokens, child.total_cached_tokens, child.reserved_tokens) == (0, 0, 0, 0)
        assert session.scalar(select(func.count()).select_from(BrainModelCall).where(BrainModelCall.build_id == child.id)) == 0
        assert project.branch == "main" and project.maintenance_state == "paused" and project.gitlab_sync is False
        assert project.active_build_id is None and project.auto_update is False
        assert before["calls"][1]["build_id"] == parent.build_id  # full-row equality above is authoritative
        assert sum(call["reserved_tokens"] for call in before["calls"] if call["state"] == "unknown") == 123
        assert session.get(BrainRecovery, child.id).plan_version == PROTOCOL


def _sqlstate(error):
    return getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)


def test_postgres_009_recovery_foreign_keys_primary_key_and_distinct_build_constraint(pg_project):
    parent = _parent(pg_project, unknown=False)
    with SyncSession() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "010_brain_control"
        child = create_recovery(session, parent.build_id)
        child_id = child.id
        session.commit()
    inspector = inspect(sync_engine)
    assert inspector.get_pk_constraint("brain_recoveries")["constrained_columns"] == ["child_build_id"]
    assert "ck_brain_recovery_distinct_builds" in {item["name"] for item in inspector.get_check_constraints("brain_recoveries")}
    foreign = {tuple(item["constrained_columns"]): item for item in inspector.get_foreign_keys("brain_recoveries")}
    for name in ("child_build_id", "parent_build_id"):
        assert foreign[(name,)]["referred_table"] == "brain_builds"
        assert foreign[(name,)]["options"].get("ondelete") == "RESTRICT"
    base = dict(status="pending", plan_version=PROTOCOL, compatibility_hash="a" * 64, plan={})
    cases = [(dict(child_build_id=parent.build_id, parent_build_id=parent.build_id), "23514"),
             (dict(child_build_id=child_id, parent_build_id=parent.build_id), "23505"),
             (dict(child_build_id=str(uuid4()), parent_build_id=parent.build_id), "23503")]
    for values, expected_state in cases:
        with SyncSession() as session:
            with pytest.raises(IntegrityError) as caught:
                session.execute(insert(BrainRecovery).values(**base, **values))
                session.commit()
            assert _sqlstate(caught.value) == expected_state
            session.rollback()


class _AsyncSessionFacade:
    """Invoke the route's business function over this exact isolated sync tx.

    No HTTP client, async engine or second schema/search_path is introduced.
    """
    def __init__(self, session):
        self.session = session
    async def scalar(self, query):
        return self.session.scalar(query)
    async def scalars(self, query):
        return self.session.scalars(query)
    async def run_sync(self, operation):
        return operation(self.session)
    async def commit(self):
        self.session.commit()


def test_postgres_frozen_parent_dependency_rejects_in_place_retry_without_mutating_checkpoints(pg_project):
    parent = _parent(pg_project, unknown=False)
    with SyncSession() as session:
        child = create_recovery(session, parent.build_id)
        dependency = session.get(BrainRecovery, child.id)
        dependency.status, dependency.manifest_hash = "frozen", "d" * 64
        session.commit()
    with SyncSession() as session:
        before = _account_snapshot(session, parent.build_id)
        with pytest.raises(HTTPException) as caught:
            asyncio.run(retry_build(pg_project, parent.build_id, session=_AsyncSessionFacade(session)))
        assert caught.value.status_code == 409 and "恢复依赖" in caught.value.detail
        session.rollback()
    with SyncSession() as session:
        assert _account_snapshot(session, parent.build_id) == before
