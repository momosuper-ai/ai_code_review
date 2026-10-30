"""Project Brain lifecycle and checkpoint acceptance against real PostgreSQL."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from openai.types.chat import ChatCompletionMessage
from sqlalchemy import delete, inspect, select, text

from src.config import settings
from src.core.gitlab_client import GitLabClient
from src.core.llm_client import ChatResult, TokenUsage
from src.core.project_brain.artifacts import ArtifactStore
from src.core.project_brain.lifecycle import claim_unit, create_build, freshness
from src.core.project_brain.lifecycle import reserve_tokens
from src.core.project_brain.snapshot import export_snapshot
from src.models import SyncSession, sync_engine
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainPublication, BrainRecovery, BrainUnit, utcnow
from src.tasks.brain_task import _complete, execute_brain_unit
from tests.unit.test_project_brain import git, repository  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get("BRAIN_LOCAL_TEST") != "1", reason="仅在独立本机 PostgreSQL 测试栈执行")


@pytest.fixture
def pg_project(monkeypatch: pytest.MonkeyPatch):
    """创建测试专属记录并清理，不连接业务库或调用真实模型/GitLab。"""
    from sqlalchemy.engine import make_url

    url = make_url(settings.DATABASE_URL_SYNC)
    assert url.host == "postgres" and url.database == "obc_brain_local" and url.username == "brain_local"
    monkeypatch.setattr(settings, "PROJECT_BRAIN_ENABLED", True)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_GITLAB_SYNC", False)
    project_id = -int(uuid4().hex[:7], 16) - 1
    yield project_id
    with SyncSession() as session:
        projects = select(BrainProject.id).where(BrainProject.gitlab_project_id == project_id)
        builds = select(BrainBuild.id).where(BrainBuild.project_id.in_(projects))
        session.execute(delete(BrainRecovery).where(
            BrainRecovery.child_build_id.in_(builds) | BrainRecovery.parent_build_id.in_(builds)))
        session.execute(delete(BrainUnit).where(BrainUnit.build_id.in_(builds)))
        session.execute(delete(BrainPublication).where(BrainPublication.build_id.in_(builds)))
        session.execute(delete(BrainBuild).where(BrainBuild.project_id.in_(projects)))
        session.execute(delete(BrainProject).where(BrainProject.gitlab_project_id == project_id))
        session.commit()


def test_postgres_migration_contract(pg_project: int) -> None:
    """已执行的迁移必须与关键生命周期列和唯一约束一致。"""
    with SyncSession() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "010_brain_control"
    inspector = inspect(sync_engine)
    assert {"child_build_id", "parent_build_id", "manifest_hash", "plan"} <= {
        column["name"] for column in inspector.get_columns("brain_recoveries")}
    assert {"maintenance_state", "event_seq", "last_observed_head"} <= {c["name"] for c in inspector.get_columns("brain_projects")}
    constraints = inspector.get_unique_constraints("brain_builds")
    assert any(c["column_names"] == ["project_id", "source_commit", "recipe_hash", "mode", "generation"] for c in constraints)
    assert "usage_tracking_version" in {column["name"] for column in inspector.get_columns("brain_builds")}
    assert {"execution_state", "pause_requested_at", "paused_at", "resumed_at"} <= {
        column["name"] for column in inspector.get_columns("brain_builds")}
    assert "ck_brain_build_execution_state" in {
        constraint["name"] for constraint in inspector.get_check_constraints("brain_builds")}
    assert {"role", "requested_model", "response_model", "state", "reserved_tokens", "prompt_tokens", "completion_tokens",
            "transport_group", "transport_attempt", "http_status", "closed_at"} <= {
        column["name"] for column in inspector.get_columns("brain_model_calls")}


def test_postgres_concurrent_claim_and_late_completion(pg_project: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实行锁只允许一个 Worker 获得租约，过期 Worker 不能落旧结果。"""
    monkeypatch.setattr("src.tasks.brain_task.dispatch_pending", lambda *args: None)
    with SyncSession() as session:
        build = create_build(session, pg_project, "main", "local/integration", "a" * 40)
        session.commit()
        unit_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build.id))
    with ThreadPoolExecutor(max_workers=4) as workers:
        claims = list(workers.map(claim_unit, [unit_id] * 4))
    claimed = [result for result in claims if result is not None]
    assert len(claimed) == 1
    old_token = claimed[0][3]
    with SyncSession() as session:
        session.get(BrainUnit, unit_id).lease_until = utcnow() - timedelta(seconds=1)
        session.commit()
    new_claim = claim_unit(unit_id)
    assert new_claim is not None and new_claim[3] != old_token
    assert not _complete(unit_id, old_token, {"fingerprint": "old"})
    assert _complete(unit_id, new_claim[3], {"fingerprint": "new"})
    with SyncSession() as session:
        assert session.get(BrainUnit, unit_id).result["fingerprint"] == "new"


def test_postgres_duplicate_build_keeps_request_generation(pg_project: int) -> None:
    """重复登记通过真实事务复用构建，不改变版本和请求序号。"""
    with SyncSession() as session:
        first = create_build(session, pg_project, "main", "local/integration", "a" * 40)
        session.commit()
        identity = first.id, first.request_seq, first.brain_version
    with SyncSession() as session:
        second = create_build(session, pg_project, "main", "local/integration", "a" * 40)
        session.commit()
        assert (second.id, second.request_seq, second.brain_version) == identity


def test_postgres_budget_refreshes_object_loaded_before_project_lock(pg_project: int, monkeypatch) -> None:
    """Reproduce a reservation reading stale ORM state while waiting for a lock."""
    from threading import Event
    from src.models import SyncSession as actual_session

    with actual_session() as session:
        build = create_build(session, pg_project, "main", "local/budget-race", "b" * 40)
        session.commit()
        unit_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build.id))
        build_id = build.id
    claim = claim_unit(unit_id)
    assert claim is not None
    token = claim[3]
    loaded, release = Event(), Event()

    def delayed_session():
        session = actual_session()
        original_get = session.get

        def get(model, identity, *args, **kwargs):
            result = original_get(model, identity, *args, **kwargs)
            if model is BrainBuild:
                loaded.set()
                assert release.wait(10)
            return result

        session.get = get
        return session

    monkeypatch.setattr("src.core.project_brain.lifecycle.SyncSession", delayed_session)
    with ThreadPoolExecutor(max_workers=1) as worker:
        reservation = worker.submit(reserve_tokens, build_id, token, unit_id, 100)
        assert loaded.wait(10)
        with actual_session() as session:
            # This committed change is invisible to the already loaded object
            # unless the later locked SELECT refreshes it explicitly.
            current = session.get(BrainBuild, build_id)
            current.reserved_tokens = 700
            session.commit()
        release.set()
        reservation.result(timeout=10)
    with actual_session() as session:
        assert session.get(BrainBuild, build_id).reserved_tokens == 800


def test_postgres_checkpoint_pipeline(pg_project: int, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy PostgreSQL pipeline; schema-2 canonical sources have a separate E2E."""
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "auto")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_BASE_DIR", str(tmp_path / "published"))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path / "private"))
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", False)
    monkeypatch.setattr("src.tasks.brain_task.dispatch_pending", lambda *args: None)
    monkeypatch.setattr(GitLabClient, "get_project", AsyncMock(return_value={"path_with_namespace": "local/integration", "web_url": "https://gitlab.test/local/integration"}))

    async def snapshot_method(self, project_id, project_path, branch, sha, destination):
        return export_snapshot(repository, Path(destination), sha, 100)

    async def chat(self, messages, **kwargs):
        packet = json.loads(messages[1]["content"])
        if packet.get("aggregate_slot") == "S1" and isinstance(packet.get("children"), list):
            content = json.dumps({"title": "本机综合概览", "slots": {
                "S1": {"texts": ["根据完整子知识集合归纳职责和边界"], "unknowns": []}},
                "unknowns": [], "document_path": None})
            return ChatResult(ChatCompletionMessage(role="assistant", content=content), TokenUsage(20, 10))
        evidence = packet["evidence"][0][0]
        content = json.dumps({"title": "本机知识验证", "claims": [{"text": "职责来自声明证据", "evidence_ids": [evidence]}], "unknowns": [], "document_path": None})
        return ChatResult(ChatCompletionMessage(role="assistant", content=content), TokenUsage(20, 10))

    monkeypatch.setattr("src.core.repo_manager.RepoManager.ensure_snapshot", snapshot_method)
    monkeypatch.setattr("src.core.llm_client.LLMClient.chat", chat)
    with SyncSession() as session:
        build = create_build(session, pg_project, "main", "local/integration", git(repository, "rev-parse", "HEAD"))
        build.coverage = {}  # Explicit schema-1 fixture and model protocol.
        session.commit()
        build_id = build.id
    completed_units = set()
    while True:
        with SyncSession() as session:
            build = session.get(BrainBuild, build_id)
            if build.status in {"ready", "partial", "failed", "superseded"}:
                break
            unit_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build_id, BrainUnit.status == "pending").order_by(BrainUnit.id))
        assert unit_id is not None, "非终态构建必须有可推进的待执行检查点"
        assert unit_id not in completed_units, "已完成的检查点不能重新执行"
        assert execute_brain_unit.run(unit_id)["status"] == "completed"
        completed_units.add(unit_id)
    with SyncSession() as session:
        build = session.get(BrainBuild, build_id)
        project = session.get(BrainProject, build.project_id)
        assert build.status == "ready" and project.active_build_id == build_id
        assert freshness(project, build) == "fresh"
        assert build.coverage["valid_reference_ratio"] == 1.0
        assert session.scalar(select(BrainPublication.id).where(BrainPublication.build_id == build_id)) is None
        units = session.scalars(select(BrainUnit).where(BrainUnit.build_id == build_id)).all()
        assert len(units) == len(completed_units) and all(unit.status == "completed" for unit in units)
        finalized = next(unit for unit in units if unit.kind == "finalize")
        assert finalized.result["source_commit"] == build.source_commit
        assert finalized.result["recipe_hash"] == build.recipe_hash
        assert all(packet["packet_digest"] for packet in finalized.result["packets"])
    store = ArtifactStore()
    assert (store.directory(build_id) / "knowledge.zip").is_file()
    assert "PROJECT_BRAIN.md" in store.repository(build_id)


def test_postgres_privacy_resume_keeps_completed_calls(pg_project: int, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy receipts resume on PG; schema-2 sources use the new worker E2E."""
    from src.core.project_brain.extractor import FactExtractor
    from src.tasks.brain_task import privacy_targets

    monkeypatch.setattr(settings, "PROJECT_BRAIN_BASE_DIR", str(tmp_path / "published"))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path / "private"))
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    # Normal checkpoints yield before the next reservation. A missing response
    # after reservation is terminal and is covered separately below.
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_RECORDS_PER_PACKET", 5)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT", 3)
    monkeypatch.setattr("src.tasks.brain_task.dispatch_pending", lambda *args: None)
    calls = []

    async def chat(self, messages, **kwargs):
        calls.append(messages[-1]["content"])
        return ChatResult(ChatCompletionMessage(role="assistant", content='{"items":[]}'), TokenUsage(100, 10), "stop")

    monkeypatch.setattr("src.core.llm_client.LLMClient.chat", chat)
    sha = git(repository, "rev-parse", "HEAD")
    with SyncSession() as session:
        build = create_build(session, pg_project, "main", "local/integration", sha, mode="full")
        build.coverage = {}  # Negative GitLab ID and old canonical mock are legacy.
        session.commit()
        build_id = build.id
        scan_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build_id))
    snap = export_snapshot(repository, ArtifactStore().directory(build_id, "snapshots"), sha, 100)
    claimed = claim_unit(scan_id)
    assert _complete(scan_id, claimed[3], {"fingerprint": snap.fingerprint, "project_url": "https://gitlab.test/local/integration"})
    with SyncSession() as session:
        extract_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build_id, BrainUnit.kind == "extract"))
    assert execute_brain_unit.run(extract_id)["status"] == "completed"
    assert not calls
    with SyncSession() as session:
        unit = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build_id, BrainUnit.kind == "privacy").order_by(BrainUnit.id))
        # Exercise several input packets in one unit to reach a normal yield.
        unit.payload = {"start": 0, "end": len(privacy_targets(FactExtractor(snap).extract()))}
        unit_id = unit.id
        session.commit()
    assert execute_brain_unit.run(unit_id) == {"status": "deferred"}
    assert len(calls) == 3
    first_completed = calls[:3]
    before = len(calls)
    for _ in range(10):
        result = execute_brain_unit.run(unit_id)
        if result["status"] == "completed":
            break
        assert result["status"] == "deferred"
    assert result["status"] == "completed"
    assert not any(value in calls[before:] for value in first_completed)
    with SyncSession() as session:
        unit = session.get(BrainUnit, unit_id)
        build = session.get(BrainBuild, build_id)
        assert unit.attempts == 1
        assert build.total_prompt_tokens == len(calls) * 100
        assert build.total_completion_tokens == len(calls) * 10


@pytest.mark.parametrize("failure", [TimeoutError, SoftTimeLimitExceeded])
def test_postgres_lost_response_stops_without_rebilling(pg_project, monkeypatch, failure):
    """A durable unknown call cannot be recovered by charging a new attempt."""
    from src.core.project_brain.usage import BuildCallMeter, CallSpec

    with SyncSession() as session:
        build = create_build(session, pg_project, "main", "local/transport-loss", "e" * 40)
        session.flush()
        current = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        current.kind = "privacy"
        sibling = BrainUnit(build_id=build.id, key="waiting", kind="privacy")
        session.add(sibling)
        session.commit()
        build_id, unit_id, sibling_id = build.id, current.id, sibling.id
    call_id, meters = str(uuid4()), []

    async def fail(unit, build, project, token):
        meter = BuildCallMeter(build.id, unit.id, token)
        meter.reserve(call_id, 90, CallSpec("privacy", "local_model", "source_excerpt"))
        meters.append(meter)
        raise failure()

    retry = MagicMock()
    monkeypatch.setattr(execute_brain_unit, "retry", retry)
    monkeypatch.setattr("src.tasks.brain_task._execute", fail)
    assert execute_brain_unit.run(unit_id) == {"status": "failed"}
    retry.assert_not_called()
    with SyncSession() as session:
        stored = session.get(BrainBuild, build_id)
        assert stored.status == "failed" and stored.reserved_tokens == 90
        assert stored.total_prompt_tokens == stored.total_completion_tokens == 0
        assert "failure_code=model_calls_incomplete" in stored.error_message
        assert session.get(BrainModelCall, call_id).state == "unknown"
        assert session.get(BrainUnit, unit_id).attempts == 1
        assert session.get(BrainUnit, unit_id).payload["_unknown_tokens"] == 90
        assert session.get(BrainUnit, sibling_id).status == "failed"
    meters[0].settle(call_id, TokenUsage(20, 5), False, 90, None)
    with SyncSession() as session:
        stored = session.get(BrainBuild, build_id)
        assert stored.status == "failed" and stored.reserved_tokens == 25
        assert session.get(BrainModelCall, call_id).state == "settled"
        assert session.get(BrainUnit, unit_id).payload["_unknown_tokens"] == 0


def test_postgres_model_calls_settle_concurrently_and_duplicate_delivery_is_idempotent(pg_project: int) -> None:
    """Actual row locks keep the per-model ledger and build total identical."""
    from src.core.project_brain.usage import BuildCallMeter, CallSpec, usage_data
    with SyncSession() as session:
        build = create_build(session, pg_project, "main", "local/model-usage", "c" * 40)
        session.flush()
        first = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        first.kind = "compile"
        second = BrainUnit(build_id=build.id, key="privacy-fixture", kind="privacy")
        session.add(second)
        session.commit()
        ids = first.id, second.id
        build_id = build.id
    meters = []
    for unit_id in ids:
        unit, _, _, token = claim_unit(unit_id)
        meters.append(BuildCallMeter(build_id, unit.id, token))
    calls = [(meters[index % 2], str(uuid4()), CallSpec("semantic" if index % 2 == 0 else "privacy",
        "semantic-fixture" if index % 2 == 0 else "privacy-fixture", "initial" if index % 2 == 0 else "generic"))
        for index in range(8)]
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda entry: entry[0].reserve(entry[1], 400, entry[2]), calls))
        # Each committed response is delivered twice, simulating a lost DB ack.
        list(workers.map(lambda entry: entry[0].settle(entry[1], TokenUsage(30, 4, 10), False, 400, None), calls + calls))
    with SyncSession() as session:
        build = session.get(BrainBuild, build_id)
        rows = session.execute(select(BrainModelCall, BrainUnit).join(BrainUnit).where(BrainModelCall.build_id == build_id)).all()
        assert len(rows) == 8
        assert build.total_prompt_tokens == 240 and build.total_completion_tokens == 32
        assert build.reserved_tokens == 272 and build.total_cached_tokens == 80
        data = usage_data(build, rows)
        assert data["exact_model_shares_available"]
        assert {item["role"]: item["token_share_percent"] for item in data["models"]} == {"privacy": 50.0, "semantic": 50.0}


def test_postgres_model_ledger_preserves_unknown_cost_until_late_response(pg_project: int) -> None:
    """Unknown blocks new admission; a real late response settles only its UUID."""
    from src.core.project_brain.usage import BuildCallMeter, CallSpec, usage_data
    from src.tasks.brain_task import _abandon_live_reservation
    with SyncSession() as session:
        build = create_build(session, pg_project, "main", "local/model-unknown", "d" * 40)
        session.commit()
        unit_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build.id))
        build_id = build.id
    unit, _, _, token = claim_unit(unit_id)
    meter = BuildCallMeter(build_id, unit_id, token)
    old_call = str(uuid4())
    meter.reserve(old_call, 90, CallSpec("privacy", "privacy-fixture", "generic"))
    with SyncSession() as session:
        current = session.get(BrainUnit, unit_id)
        _abandon_live_reservation(current, session)
        current.status, current.lease_token, current.lease_until = "pending", None, None
        session.commit()
    assert claim_unit(unit_id) is None
    with SyncSession() as session:
        build = session.get(BrainBuild, build_id)
        current = session.get(BrainUnit, unit_id)
        assert build.status == "failed" and build.reserved_tokens == 90
        assert build.total_prompt_tokens == build.total_completion_tokens == 0
        assert current.status == "failed" and current.lease_token is None
        assert current.payload["_reserved_tokens"] == 0 and current.payload["_unknown_tokens"] == 90
        assert session.get(BrainModelCall, old_call).state == "unknown"
        rows = session.execute(select(BrainModelCall, BrainUnit).join(BrainUnit).where(
            BrainModelCall.build_id == build_id)).all()
        data = usage_data(build, rows)
        assert data["unknown_reserved_tokens"] == 90 and not data["exact_model_shares_available"]
    meter.settle(old_call, TokenUsage(20, 5), False, 90, None)
    meter.settle(old_call, TokenUsage(20, 5), False, 90, None)
    with SyncSession() as session:
        build = session.get(BrainBuild, build_id)
        current = session.get(BrainUnit, unit_id)
        assert build.status == "failed" and build.reserved_tokens == 25
        assert build.total_prompt_tokens == 20 and build.total_completion_tokens == 5
        assert current.status == "failed" and current.lease_token is None
        assert current.payload["_unknown_tokens"] == 0 and current.payload["_reserved_tokens"] == 0
        assert session.get(BrainModelCall, old_call).state == "settled"
    assert claim_unit(unit_id) is None
