"""Real PostgreSQL frontier/commit recovery with synthetic, offline knowledge."""

import asyncio
import copy
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from celery.exceptions import Retry
from sqlalchemy import select, text

from src.config import settings
from src.core.project_brain.compiler import CompilationLimitError
from src.core.project_brain.lifecycle import claim_unit, create_build
from src.core.project_brain.trusted_packet import checkpoint_digest
from src.models import SyncSession
from src.models.brain import BrainBuild, BrainProject, BrainUnit, utcnow
from src.tasks import brain_task
from tests.integration.test_brain_postgres import pg_project  # noqa: F401
from tests.unit.test_brain_hierarchy_planner import fixture, ready

pytestmark = pytest.mark.skipif(os.environ.get("BRAIN_LOCAL_TEST") != "1",
                              reason="仅在独立本机 PostgreSQL 测试栈执行")


@pytest.fixture
def offline_planning(monkeypatch):
    monkeypatch.setattr(settings, "MAIN_BRANCHES", "main")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    monkeypatch.setattr(brain_task, "dispatch_pending", lambda *args: None)
    monkeypatch.setattr("src.core.llm_client.LLMClient.chat",
                        AsyncMock(side_effect=AssertionError("planning must not call a model")))
    monkeypatch.setattr("src.core.gitlab_client.GitLabClient.get_project",
                        AsyncMock(side_effect=AssertionError("planning must not call GitLab")))
    monkeypatch.setattr(brain_task, "read_snapshot", lambda *args: SimpleNamespace(commit_sha="a" * 40))
    monkeypatch.setattr(brain_task, "validate", lambda *args: None)
    return monkeypatch


def _seed(pg_project, monkeypatch, *, count=1, completed=False):
    bundle, source_units = fixture((count,))
    for index, unit in enumerate(source_units):
        unit.payload.update(index=index, fact_ids=[f"fact:{index}"])
    monkeypatch.setattr(brain_task.ArtifactStore, "read_work", lambda *args: bundle.model_dump())
    with SyncSession() as session:
        build = create_build(session, pg_project, "main", "synthetic/planning", "a" * 40, mode="full")
        session.flush()
        scan = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        scan.status, scan.result = "completed", {"fingerprint": "f" * 64}
        build.status = "compiling"
        build.total_prompt_tokens, build.total_completion_tokens, build.reserved_tokens = 100, 20, 120
        session.add(BrainUnit(build_id=build.id, kind="extract", key="extract", status="completed", result={}))
        session.add(BrainUnit(build_id=build.id, kind="privacy", key="privacy:0", status="completed", result={}))
        session.add(BrainUnit(build_id=build.id, kind="finalize", key="finalize", status="completed", result={
            "source_commit": build.source_commit, "recipe_hash": build.recipe_hash,
            "bundle_hash": checkpoint_digest(bundle.model_dump()), "packets": [
                {"index": unit.payload["index"], "module_id": unit.payload["module_id"],
                 "fact_ids": unit.payload["fact_ids"], "hash": unit.input_hash}
                for unit in source_units]}))
        for unit in source_units:
            session.add(BrainUnit(build_id=build.id, key=unit.key, kind=unit.kind,
                                  payload=unit.payload, input_hash=unit.input_hash,
                                  status="completed" if completed else "pending",
                                  result=unit.result if completed else None))
        session.commit()
        identities = session.scalars(select(BrainUnit.id).where(BrainUnit.build_id == build.id,
                                    BrainUnit.kind == "compile").order_by(BrainUnit.id)).all()
        return build.id, bundle, source_units, identities


def _planner_id(build_id):
    with SyncSession() as session:
        return session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build_id,
                                                        BrainUnit.kind == "planning"))


def test_postgres_paid_summary_commit_survives_planner_capacity_failure(pg_project, offline_planning):
    build_id, bundle, leaves, _ = _seed(pg_project, offline_planning, completed=True)
    task = brain_task.next_summary_tasks(bundle, leaves)[0]
    output = ready(task).result
    with SyncSession() as session:
        summary = BrainUnit(build_id=build_id, key=task.key, kind=task.kind,
                            payload=task.payload, input_hash=task.input_hash)
        session.add(summary)
        session.commit()
        summary_id = summary.id
    claim = claim_unit(summary_id)
    assert claim
    planner = pytest.MonkeyPatch()
    def capacity_error(*args):
        raise CompilationLimitError("synthetic topology cannot converge")
    planner.setattr(brain_task, "next_summary_tasks", capacity_error)
    try:
        # Old code ran capacity_error here and rolled back the paid result.
        assert brain_task._complete(summary_id, claim[3], output)
        planning_id = _planner_id(build_id)
        assert planning_id is not None
        assert brain_task.execute_brain_unit.run(planning_id) == {"status": "failed"}
    finally:
        planner.undo()
    with SyncSession() as session:
        paid = session.get(BrainUnit, summary_id)
        planning = session.get(BrainUnit, planning_id)
        build = session.get(BrainBuild, build_id)
        assert paid.status == "completed" and paid.result == output
        assert planning.status == "failed" and "CompilationLimitError" in planning.error_message
        assert build.status == "failed"
        assert (build.total_prompt_tokens, build.total_completion_tokens, build.reserved_tokens) == (100, 20, 120)
        assert session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build_id,
                                                        BrainUnit.kind == "assemble")) is None
    assert claim_unit(summary_id) is None


def test_postgres_concurrent_last_leaf_completions_create_single_planner(pg_project, offline_planning):
    build_id, _, leaves, identities = _seed(pg_project, offline_planning, count=2)
    claims = [claim_unit(identity) for identity in identities]
    assert all(claims)
    with ThreadPoolExecutor(max_workers=2) as workers:
        completions = list(workers.map(lambda pair: brain_task._complete(pair[0][0].id, pair[0][3], pair[1].result),
                                      zip(claims, leaves, strict=True)))
    assert completions == [True, True]
    with SyncSession() as session:
        planners = session.scalars(select(BrainUnit).where(BrainUnit.build_id == build_id,
                                                          BrainUnit.kind == "planning")).all()
        assert len(planners) == 1 and planners[0].status == "pending"
        assert planners[0].payload["trigger_unit_id"] in identities
        assert not session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build_id,
                                                            BrainUnit.kind.in_(brain_task.SUMMARY_KINDS)))


def test_postgres_planning_executes_graph_outside_project_and_build_locks(pg_project, offline_planning):
    build_id, bundle, leaves, identities = _seed(pg_project, offline_planning)
    claim = claim_unit(identities[0])
    assert brain_task._complete(identities[0], claim[3], leaves[0].result)
    planning_id = _planner_id(build_id)
    original = brain_task.next_summary_tasks
    def unlocked_plan(knowledge, inputs):
        with SyncSession() as session:
            session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            build = session.get(BrainBuild, build_id)
            session.scalar(select(BrainProject).where(BrainProject.id == build.project_id).with_for_update(nowait=True))
            session.scalar(select(BrainBuild).where(BrainBuild.id == build_id).with_for_update(nowait=True))
        return original(knowledge, inputs)
    offline_planning.setattr(brain_task, "next_summary_tasks", unlocked_plan)
    assert brain_task.execute_brain_unit.run(planning_id) == {"status": "completed"}
    with SyncSession() as session:
        assert session.get(BrainUnit, planning_id).status == "completed"
        summaries = session.scalars(select(BrainUnit).where(BrainUnit.build_id == build_id,
                                   BrainUnit.kind.in_(brain_task.SUMMARY_KINDS))).all()
        assert len(summaries) == 1 and summaries[0].status == "pending"


def test_postgres_stale_dependency_rejects_plan_without_overwriting_paid_leaf(pg_project, offline_planning):
    build_id, _, leaves, identities = _seed(pg_project, offline_planning)
    claim = claim_unit(identities[0])
    assert brain_task._complete(identities[0], claim[3], leaves[0].result)
    planning_id = _planner_id(build_id)
    planning_claim = claim_unit(planning_id)
    result = asyncio.run(brain_task._execute(*planning_claim[:3], planning_claim[3]))
    with SyncSession() as session:
        leaf = session.get(BrainUnit, identities[0])
        changed = copy.deepcopy(leaf.result)
        changed["title"] = "Changed dependency"
        leaf.result = changed
        session.commit()
    with pytest.raises(ValueError, match="规划依赖"):
        brain_task._complete(planning_id, planning_claim[3], result)
    with SyncSession() as session:
        assert session.get(BrainUnit, identities[0]).result["title"] == "Changed dependency"
        assert session.get(BrainUnit, planning_id).status == "running"
        assert not session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build_id,
                                                            BrainUnit.kind.in_(brain_task.SUMMARY_KINDS)))


def test_postgres_late_planner_lease_and_new_request_cannot_insert_frontier(pg_project, offline_planning):
    build_id, _, leaves, identities = _seed(pg_project, offline_planning)
    claim = claim_unit(identities[0])
    assert brain_task._complete(identities[0], claim[3], leaves[0].result)
    planning_id = _planner_id(build_id)
    old = claim_unit(planning_id)
    result = asyncio.run(brain_task._execute(*old[:3], old[3]))
    with SyncSession() as session:
        session.get(BrainUnit, planning_id).lease_until = utcnow() - timedelta(seconds=1)
        session.commit()
    new = claim_unit(planning_id)
    assert new and new[3] != old[3]
    assert not brain_task._complete(planning_id, old[3], result)
    with SyncSession() as session:
        build = session.get(BrainBuild, build_id)
        session.get(BrainProject, build.project_id).request_seq += 1
        session.commit()
    assert not brain_task._complete(planning_id, new[3], result)
    with SyncSession() as session:
        assert session.get(BrainBuild, build_id).status == "superseded"
        assert not session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build_id,
                                                            BrainUnit.kind.in_(brain_task.SUMMARY_KINDS)))


def test_postgres_planning_retry_does_not_regenerate_completed_model_output(pg_project, offline_planning):
    build_id, _, leaves, identities = _seed(pg_project, offline_planning)
    claim = claim_unit(identities[0])
    assert brain_task._complete(identities[0], claim[3], leaves[0].result)
    planning_id = _planner_id(build_id)
    original = brain_task.next_summary_tasks
    def transient_planning_error(*args):
        raise ValueError("synthetic temporary planning failure")
    offline_planning.setattr(brain_task, "next_summary_tasks", transient_planning_error)
    with pytest.raises((Retry, RuntimeError)):
        brain_task.execute_brain_unit.run(planning_id)
    with SyncSession() as session:
        assert session.get(BrainUnit, planning_id).status == "pending"
        assert session.get(BrainUnit, identities[0]).status == "completed"
        assert session.get(BrainUnit, identities[0]).result == leaves[0].result
    offline_planning.setattr(brain_task, "next_summary_tasks", original)
    assert brain_task.execute_brain_unit.run(planning_id) == {"status": "completed"}
    with SyncSession() as session:
        planning = session.get(BrainUnit, planning_id)
        assert planning.attempts == 2 and planning.status == "completed"
        assert session.get(BrainUnit, identities[0]).attempts == 1
        build = session.get(BrainBuild, build_id)
        assert (build.total_prompt_tokens, build.total_completion_tokens, build.reserved_tokens) == (100, 20, 120)
