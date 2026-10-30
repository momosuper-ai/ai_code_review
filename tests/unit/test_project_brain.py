"""Acceptance tests for pinned evidence, incremental knowledge and recovery."""

import hashlib
import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import httpx
from fastapi import FastAPI
from openai.types.chat import ChatCompletionMessage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.core.gitlab_client import GitLabClient
from src.core.llm_client import ChatResult, TokenUsage
from src.core.project_brain.artifacts import ArtifactStore, assemble_cards, owned_path, validate
from src.core.project_brain.compiler import CompilationGuard, compile_packet, encoded, packet_hash, packets
from src.core.project_brain.extractor import FactExtractor
from src.core.project_brain.lifecycle import account_tokens, claim_unit, create_build, freshness, mark_dirty, recipe_hash, reserve_tokens
from src.core.project_brain.publisher import publication_actions
from src.core.project_brain.schema import BrainManifest, Compilation
from src.core.project_brain.snapshot import allowed_source, export_snapshot, run_git
from src.models import Base
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainPublication, BrainRecovery, BrainUnit, utcnow
from src.tasks.brain_task import _complete, execute_brain_unit


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True,
                            env={**os.environ, "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
                                 "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test"})
    return result.stdout.decode().strip()


def test_large_declaration_packets_preserve_every_relation(snapshot, monkeypatch) -> None:
    """A declaration with many calls splits without losing facts or evidence."""
    from src.core.project_brain.schema import Relation
    from src.core.project_brain.compiler import token_bound

    bundle = FactExtractor(snapshot).extract()
    fact = next(f for f in bundle.facts if f.source_ref_ids)
    subset = bundle.model_copy(update={"facts": [fact], "modules": {fact.module_id: bundle.modules[fact.module_id]}})
    subset.relations = [Relation(id=f"fixture-relation-{i}", from_id=fact.id, type="calls", to_id=None,
        target_name=f"fixture_target_{i}", resolution="unresolved", source_ref_ids=fact.source_ref_ids) for i in range(50)]
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 2048)
    result = packets(subset, snapshot)
    assert len(result) > 1
    from src.core.project_brain.context import compact_packet
    assert all(token_bound(encoded(compact_packet(packet).wire)) <= 2048 for packet in result)
    items = [item for packet in result for item in packet["items"]]
    assert {relation["id"] for item in items for relation in item["relations"]} == {r.id for r in subset.relations}
    assert sum(len(item["relations"]) for item in items) == 50
    assert all(item["fragment"]["total"] == len(items) for item in items)
    assert all(set(rid for relation in item["relations"] for rid in relation["source_ref_ids"]) <= {ref["id"] for ref in item["evidence"]} for item in items)


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "git"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    files = {
        "app/main.py": 'from fastapi import FastAPI\nfrom app.routes import router as api\napp = FastAPI()\napp.include_router(api, prefix="/api")\n',
        "app/routes.py": 'from fastapi import APIRouter\nfrom app.service import lookup as load\nrouter = APIRouter(prefix="/users")\n@router.get("/")\ndef users():\n    return load()\n',
        "app/service.py": 'from sqlalchemy import select\nfrom app.models import User\ndef lookup():\n    return select(User)\n',
        "app/models.py": 'class User:\n    __tablename__ = "users"\n',
        "migration.sql": "CREATE TABLE users (id INTEGER);\n",
        "web/View.tsx": "export function View() { return <main>Hello</main>; }\n",
        "web/run.js": "function run() { unknown(); }\n",
        "service/main.go": "package main\nfunc main() {}\n",
        "java/pom.xml": '<project><dependencies><dependency><groupId>org.test</groupId><artifactId>test</artifactId></dependency></dependencies></project>',
        "java/UserController.java": '''package example;
import org.springframework.web.bind.annotation.*;
import org.apache.ibatis.annotations.Select;
@RequestMapping("/users")
class UserController {
    @GetMapping({"/", "/all"})
    public String list() { return helper(); }
    private String helper() { return "ok"; }
    @Select("SELECT id FROM users")
    public String lookup() { return ""; }
}
''',
        ".env": "PASSWORD=secret-should-not-leak\n",
        ".env.example": "DATABASE_PASSWORD=placeholder\n",
        "PROJECT_BRAIN.md": "generated\n",
        "AI_README.md": "legacy\n",
    }
    for name, content in files.items():
        full = repo / name
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8", newline="\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
def snapshot(repository, tmp_path):
    return export_snapshot(repository, tmp_path / "snapshot", git(repository, "rev-parse", "HEAD"), 100)


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://")
    tables = [BrainProject.__table__, BrainBuild.__table__, BrainUnit.__table__, BrainPublication.__table__, BrainModelCall.__table__, BrainRecovery.__table__]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(engine, expire_on_commit=False)
    for module in ("src.core.project_brain.lifecycle", "src.tasks.brain_task", "src.tasks.brain_publication_task"):
        monkeypatch.setattr(module + ".SyncSession", factory)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_ENABLED", True)
    yield factory
    engine.dispose()


def test_pinned_snapshot_excludes_secrets_and_generated(snapshot, repository):
    assert ".env" not in snapshot.files
    assert "AI_README.md" not in snapshot.files
    assert "PROJECT_BRAIN.md" not in snapshot.files
    assert not (Path(snapshot.path) / ".git").exists()
    before = (Path(snapshot.path) / "app/service.py").read_text()
    (repository / "app/service.py").write_text("changed = True\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "advance")
    assert (Path(snapshot.path) / "app/service.py").read_text() == before


@pytest.mark.parametrize("path", ["../x.md", "docs/project-brain/../x.json", "C:/x.json", "docs\\project-brain\\x.md", "/x.md", "docs//project-brain/x.md"])
def test_path_sandbox(path):
    with pytest.raises(ValueError):
        owned_path(path)


@pytest.mark.parametrize("path", ["src/main.py", "docs/project-brain-other/page.md", "docs/project-brain", "AI_README.md"])
def test_publication_whitelist(path):
    assert not owned_path(path)


def test_source_file_limit_is_failure(repository, tmp_path):
    with pytest.raises(ValueError, match="超过"):
        export_snapshot(repository, tmp_path / "limited", git(repository, "rev-parse", "HEAD"), 1)


def test_python_java_and_generic_facts(snapshot):
    bundle = FactExtractor(snapshot).extract()
    assert {"GET /api/users/", "GET /users/", "GET /users/all"} <= {f.name for f in bundle.facts if f.kind == "interface"}
    assert {"python", "java", "go", "typescript", "javascript"} <= {f.attributes["language"] for f in bundle.facts if f.kind == "symbol"}
    facts = {f.id: f for f in bundle.facts}
    assert any(r.type == "CALLS" and r.resolution == "resolved" and facts[r.from_id].name == "users" and facts[r.to_id].name == "lookup" for r in bundle.relations)
    assert any(r.type == "CALLS" and r.resolution == "resolved" and facts[r.to_id].name == "example.UserController.helper" for r in bundle.relations)
    assert any(r.type == "READS_TABLE" and r.resolution == "resolved" and facts[r.from_id].name == "lookup" for r in bundle.relations)
    validate(bundle, [], snapshot)


def test_same_name_dynamic_calls_never_resolve(repository, tmp_path):
    (repository / "app/service.py").write_text("from app.other import lookup as imported\ndef lookup():\n    return 1\ndef other(obj):\n    return obj.lookup()\ndef shadowed(imported):\n    return imported()\n")
    (repository / "app/other.py").write_text("def lookup():\n    return 2\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "ambiguous")
    snap = export_snapshot(repository, tmp_path / "ambiguous", git(repository, "rev-parse", "HEAD"), 100)
    bundle = FactExtractor(snap).extract()
    relation = next(r for r in bundle.relations if r.target_name == "obj.lookup")
    assert relation.resolution == "candidate" and relation.to_id is None
    shadowed = next(r for r in bundle.relations if r.target_name == "imported")
    assert shadowed.resolution != "resolved" and shadowed.to_id is None


def test_incremental_delete_invalidates_call_and_keeps_other_modules(snapshot, repository, tmp_path):
    old = FactExtractor(snapshot).extract()
    (repository / "app/service.py").unlink()
    git(repository, "add", ".")
    git(repository, "commit", "-m", "delete")
    new = export_snapshot(repository, tmp_path / "next", git(repository, "rev-parse", "HEAD"), 100)
    extractor = FactExtractor(new, old)
    bundle = extractor.extract()
    assert extractor.reused_files > 0
    assert not any(f.attributes["path"] == "app/service.py" for f in bundle.facts)
    assert next(r for r in bundle.relations if r.target_name == "load").resolution == "unresolved"
    assert any(f.attributes["path"] == "web/View.tsx" for f in bundle.facts)
    assert all(r.commit_sha == new.commit_sha for r in bundle.source_refs)
    validate(bundle, [], new)


def test_blob_tampering_blocks_ready(snapshot):
    bundle = FactExtractor(snapshot).extract()
    full = Path(snapshot.path) / "app/models.py"
    full.chmod(0o600)
    full.write_text("tampered\n")
    with pytest.raises(ValueError, match="blob"):
        validate(bundle, [], snapshot)


@pytest.mark.asyncio
async def test_compile_rejects_forged_evidence(snapshot, monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", False)
    client = MagicMock()
    client.chat = AsyncMock(return_value=ChatResult(ChatCompletionMessage(role="assistant", content=json.dumps({"title": "模块", "claims": [{"text": "虚构", "evidence_ids": ["forged"]}], "unknowns": []})), TokenUsage(20, 10)))
    client.aclose = AsyncMock()
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", lambda **kwargs: client)
    packet = packets(FactExtractor(snapshot).extract(), snapshot)[0]
    guard = CompilationGuard(lambda *args: None, lambda amount: None)
    with pytest.raises(ValueError, match="校验"):
        await compile_packet(packet, guard)
    assert client.chat.await_count == settings.AGENT_FORMAT_RETRY_MAX + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_path", ["自由布局/说明.txt", "docs/project-brain/../app.py", "/docs/project-brain/说明.txt"])
async def test_compile_corrects_document_path_without_restricting_layout(snapshot, monkeypatch, bad_path):
    """目录授权不限制命名；非法建议重试时明确要求完整仓库相对路径。"""
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", False)
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    packet = packets(FactExtractor(snapshot).extract(), snapshot)[0]
    evidence = packet["items"][0]["evidence"][0]["id"]
    body = {"title": "模块", "claims": [{"text": "职责解释", "evidence_ids": [evidence]}], "unknowns": []}
    good_path = "docs/project-brain/自由布局/无扩展名"
    client = MagicMock()
    client.chat = AsyncMock(side_effect=[
        ChatResult(ChatCompletionMessage(role="assistant", content=json.dumps({**body, "document_path": bad_path})), TokenUsage(20, 10)),
        ChatResult(ChatCompletionMessage(role="assistant", content=json.dumps({**body, "document_path": good_path})), TokenUsage(20, 10)),
    ])
    client.aclose = AsyncMock()
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", lambda **kwargs: client)
    guard = CompilationGuard(lambda *args: None, lambda amount: None)
    result = await compile_packet(packet, guard)
    assert result.document_path == good_path
    feedback = client.chat.call_args_list[1].args[0][-1]["content"]
    assert "invalid_document_path" in feedback and "docs/project-brain/" in feedback
    assert bad_path not in feedback


@pytest.mark.asyncio
async def test_compile_output_privacy_failure_does_not_retry_model(snapshot, monkeypatch):
    """输出过滤失败停止该单元，不以格式修复为由继续调用云端。"""
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", False)
    packet = packets(FactExtractor(snapshot).extract(), snapshot)[0]
    evidence = packet["items"][0]["evidence"][0]["id"]
    client = MagicMock()
    client.chat = AsyncMock(return_value=ChatResult(ChatCompletionMessage(role="assistant", content=json.dumps({"title": "模块", "claims": [{"text": "职责解释", "evidence_ids": [evidence]}], "unknowns": [], "document_path": None})), TokenUsage(20, 10)))
    client.aclose = AsyncMock()
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", lambda **kwargs: client)
    guard = CompilationGuard(lambda *args: None, lambda amount: None)
    monkeypatch.setattr(guard, "clean", AsyncMock(side_effect=[encoded(packet), ValueError("隐私扫描失败")]))
    with pytest.raises(ValueError, match="隐私扫描"):
        await compile_packet(packet, guard)
    assert client.chat.await_count == 1


@pytest.mark.asyncio
async def test_local_scan_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    client = MagicMock()
    client.chat = AsyncMock(return_value=ChatResult(ChatCompletionMessage(role="assistant", content="not JSON"), TokenUsage(5, 5)))
    client.aclose = AsyncMock()
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", lambda **kwargs: client)
    guard = CompilationGuard(lambda *args: None, lambda amount: None)
    with pytest.raises(ValueError):
        await guard.clean("代码")


def test_immutable_artifacts_and_all_six_cards(snapshot, tmp_path):
    bundle = FactExtractor(snapshot).extract()
    compiled = [(module, Compilation(title=name, claims=[{"text": "模块职责", "evidence_ids": next(f.source_ref_ids for f in bundle.facts if f.module_id == module)}], unknowns=[])) for module, name in bundle.modules.items()]
    cards = assemble_cards(bundle, compiled, [])
    assert {c.kind for c in cards} == {"project", "architecture", "module", "interface", "data", "flow"}
    coverage = validate(bundle, cards, snapshot)
    manifest = BrainManifest(generator="test", build_id=str(uuid4()), project_id=1, branch="main", source_commit=bundle.source_commit,
                             brain_version=1, recipe_hash="recipe", source_fingerprint=bundle.source_fingerprint, files={}, coverage=coverage, issues=bundle.issues)
    store = ArtifactStore(str(tmp_path / "artifacts"))
    dest = store.finalize(manifest, bundle, cards, "https://gitlab.test/group/project")
    assert (dest / "knowledge.zip").exists()
    assert store.finalize(manifest, bundle, cards, "https://gitlab.test/group/project") == dest
    assert store.repository(manifest.build_id)["PROJECT_BRAIN.md"].startswith("<!-- project-brain:owned -->")


def test_durable_request_and_historical_build_fence(database):
    with database() as session:
        first = create_build(session, 1, "main", "group/project", "a" * 40)
        session.commit()
        project = session.get(BrainProject, first.project_id)
        assert mark_dirty(session, 1, "main")
        current_seq = project.request_seq
        history = create_build(session, 1, "main", "group/project", "b" * 40, historical=True)
        session.commit()
        assert history.historical and project.request_seq == current_seq
        unit_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == first.id))
    assert claim_unit(unit_id) is not None
    with database() as session:
        newer = create_build(session, 1, "main", "group/project", "c" * 40)
        assert newer.request_seq > first.request_seq
        session.commit()
        session.get(BrainUnit, unit_id).lease_until = utcnow() - timedelta(seconds=1)
        session.commit()
    assert claim_unit(unit_id) is None


def test_lease_fences_duplicate_and_late_result(database):
    with database() as session:
        build = create_build(session, 1, "main", "group/project", "a" * 40)
        session.commit()
        unit_id = session.scalar(select(BrainUnit.id))
    claimed = claim_unit(unit_id)
    assert claimed and claim_unit(unit_id) is None
    unit, build, project, token = claimed
    with database() as session:
        session.get(BrainUnit, unit_id).lease_until = utcnow() - timedelta(seconds=1)
        session.commit()
    newer = claim_unit(unit_id)
    assert newer[3] != token
    assert not _complete(unit_id, token, {"fingerprint": "old"})
    assert _complete(unit_id, newer[3], {"fingerprint": "new"})


def test_budget_survives_lost_worker(database, monkeypatch):
    with database() as session:
        create_build(session, 1, "main", "group/project", "a" * 40)
        session.commit()
        unit_id = session.scalar(select(BrainUnit.id))
    unit, build, project, token = claim_unit(unit_id)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_BUILD_TOKENS", 50)
    reserve_tokens(build.id, token, unit.id, 40)
    with pytest.raises(ValueError, match="预算"):
        reserve_tokens(build.id, token, unit.id, 20)


def test_expired_same_input_creates_new_immutable_generation(database):
    with database() as session:
        old = create_build(session, 1, "main", "group/project", "a" * 40)
        old.status, old.artifact_path = "ready", None
        session.commit()
        newer = create_build(session, 1, "main", "group/project", "a" * 40)
        assert newer.id != old.id and newer.generation == old.generation + 1
        assert newer.brain_version > old.brain_version and old.status == "ready"
        session.commit()
        duplicate = create_build(session, 1, "main", "group/project", "a" * 40)
        assert duplicate.id == newer.id and duplicate.request_seq == newer.request_seq


def test_temporary_checkpoints_are_outside_publisher_artifact_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_BASE_DIR", str(tmp_path / "published"))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path / "private-work"))
    store = ArtifactStore()
    bid = str(uuid4())
    store.write_work(bid, "facts.json", {"checkpoint": "private"})
    assert store.read_work(bid, "facts.json") == {"checkpoint": "private"}
    assert not store.directory(bid, "work").is_relative_to(store.base)
    assert not store.directory(bid, "snapshots").is_relative_to(store.base)
    store.remove(store.directory(bid, "work"))
    assert not store.directory(bid, "work").exists()
    with pytest.raises(ValueError):
        store.remove(tmp_path)


def test_pause_blocks_next_llm_reservation(database):
    with database() as session:
        build = create_build(session, 1, "main", "group/project", "a" * 40)
        session.commit()
        unit_id = session.scalar(select(BrainUnit.id))
    unit, build, project, token = claim_unit(unit_id)
    with database() as session:
        current = session.get(BrainProject, project.id)
        current.maintenance_state, current.auto_update = "paused", False
        session.commit()
    with pytest.raises(ValueError, match="维护请求"):
        reserve_tokens(build.id, token, unit.id, 10)


def test_publication_conflict_keeps_ready_build_and_checkpoint(database, monkeypatch):
    from src.core.project_brain.write_policy import WriteConflict
    from src.tasks.brain_publication_task import publish_brain

    with database() as session:
        build = create_build(session, 1, "main", "group/project", "a" * 40)
        build.status, build.artifact_path = "ready", "stored"
        project = session.get(BrainProject, build.project_id)
        project.active_build_id = build.id
        publication = BrainPublication(build_id=build.id, target_branch="main")
        session.add(publication)
        session.commit()
    monkeypatch.setattr("src.tasks.brain_publication_task.publish", AsyncMock(side_effect=WriteConflict("人工编辑", "human_edit")))
    assert publish_brain.run(publication.id) == {"status": "failed"}
    with database() as session:
        assert session.get(BrainBuild, build.id).status == "ready"
        failed = session.get(BrainPublication, publication.id)
        assert failed.status == "failed" and failed.error_code == "human_edit"


@pytest.mark.asyncio
async def test_publication_refuses_human_content():
    client = MagicMock()
    async def get_file(project, path, ref):
        return None if path.endswith("manifest.json") else {"decoded_content": "human docs", "last_commit_id": "old"}
    client.get_repository_file = AsyncMock(side_effect=get_file)
    with pytest.raises(ValueError, match="人工"):
        await publication_actions(client, 1, "head", {"PROJECT_BRAIN.md": "generated"}, lambda *_: True)


@pytest.mark.asyncio
async def test_new_commit_entry_preserves_legacy_limit():
    client = GitLabClient()
    with pytest.raises(ValueError):
        await client.create_commit(1, "main", "message", [{"action": "create", "file_path": "PROJECT_BRAIN.md", "content": "x"}])
    with pytest.raises(ValueError):
        await client.create_brain_commit(1, "main", "id", [{"action": "update", "file_path": "PROJECT_BRAIN.md", "content": "x"}], {"PROJECT_BRAIN.md": "x"}, set())


def test_complete_checkpoint_pipeline_without_remote_writes(database, repository, tmp_path, monkeypatch):
    """执行所有实际阶段，仅替换模型和 GitLab 网络，不省略验证与文件产物。"""
    monkeypatch.setattr(settings, "PROJECT_BRAIN_BASE_DIR", str(tmp_path / "brain"))
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", False)
    monkeypatch.setattr("src.tasks.brain_task.dispatch_pending", lambda *args: None)
    monkeypatch.setattr(GitLabClient, "get_project", AsyncMock(return_value={"path_with_namespace": "group/project", "web_url": "https://gitlab.test/group/project"}))
    async def snapshot_method(self, project_id, project_path, branch, sha, destination):
        return export_snapshot(repository, Path(destination), sha, 100)
    monkeypatch.setattr("src.core.repo_manager.RepoManager.ensure_snapshot", snapshot_method)
    async def chat(self, messages, **kwargs):
        packet = json.loads(messages[1]["content"])
        if packet.get("aggregate_slot") == "S1" and isinstance(packet.get("children"), list):
            return ChatResult(ChatCompletionMessage(role="assistant", content=json.dumps({"title": "综合概览",
                "slots": {"S1": {"texts": ["依据全部子知识归纳职责与边界"], "unknowns": []}},
                "unknowns": [], "document_path": None})), TokenUsage(20, 10))
        rid = packet["items"][0]["evidence"][0]
        return ChatResult(ChatCompletionMessage(role="assistant", content=json.dumps({"title": "已编译", "claims": [{"text": "根据声明归纳职责", "evidence_ids": [rid]}], "unknowns": []})), TokenUsage(20, 10))
    monkeypatch.setattr("src.core.llm_client.LLMClient.chat", chat)
    with database() as session:
        build = create_build(session, 1, "main", "group/project", git(repository, "rev-parse", "HEAD"))
        session.commit()
        build_id = build.id
    for _ in range(100):
        with database() as session:
            unit_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build_id, BrainUnit.status == "pending").order_by(BrainUnit.id))
        if unit_id is None:
            break
        result = execute_brain_unit.run(unit_id)
        assert result["status"] == "completed"
    with database() as session:
        build = session.get(BrainBuild, build_id)
        project = session.get(BrainProject, build.project_id)
        publication = session.scalar(select(BrainPublication).where(BrainPublication.build_id == build_id))
        assert build.status == "ready"
        assert project.active_build_id == build.id
        assert freshness(project, build) == "fresh"
        assert build.coverage["valid_reference_ratio"] == 1.0
        assert publication.status == "pending"
        assert build.total_prompt_tokens > 0
    store = ArtifactStore()
    assert {c.kind for c in store.read_cards(build_id)} == {"project", "architecture", "module", "interface", "data", "flow"}
    assert "secret-should-not-leak" not in (store.directory(build_id) / "facts.json").read_text(encoding="utf-8")


class AsyncSessionAdapter:
    """测试用真实 SQLite Session 异步外观，不连接 PostgreSQL。"""

    def __init__(self, factory):
        self.session = factory()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.session.close()

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def scalars(self, statement):
        return self.session.scalars(statement)

    async def get(self, *args):
        return self.session.get(*args)

    async def run_sync(self, callback):
        return callback(self.session)

    async def commit(self):
        self.session.commit()

    def expunge_all(self):
        self.session.expunge_all()


@pytest.mark.asyncio
async def test_api_build_auth_scope_and_historical_archive(database, monkeypatch):
    from src.api.v1.brain import router
    from src.models import get_session

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(settings, "API_KEYS", "internal-test-key")
    monkeypatch.setattr("src.api.v1.brain.dispatch_pending", lambda *args: None)
    monkeypatch.setattr(GitLabClient, "get_project", AsyncMock(return_value={"path_with_namespace": "group/project"}))
    monkeypatch.setattr(GitLabClient, "get_branch", AsyncMock(return_value={"commit": {"id": "a" * 40}}))
    monkeypatch.setattr(GitLabClient, "brain_request", AsyncMock(return_value={"id": "b" * 40}))
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    async def dependency():
        async with AsyncSessionAdapter(database) as adapter:
            yield adapter
    app.dependency_overrides[get_session] = dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        root = "/api/v1/projects/1/brain"
        assert (await client.post(root + "/builds", json={"branch": "main"})).status_code == 401
        client.headers["X-API-Key"] = "internal-test-key"
        assert (await client.post(root + "/builds", json={"branch": "main", "repo_url": "http://attacker"})).status_code == 422
        response = await client.post(root + "/builds", json={"branch": "main"})
        assert response.status_code == 202
        bid = response.json()["build_id"]
        assert (await client.get(root + f"/builds/{bid}")).json()["status"] == "pending"
        assert (await client.get(root + f"/builds/{bid}/cards")).status_code == 409
        assert (await client.get(f"/api/v1/projects/2/brain/builds/{bid}")).status_code == 404
        historic = await client.post(root + "/builds", json={"branch": "main", "commit_sha": "b" * 40})
        assert historic.status_code == 202
        detail = await client.get(historic.json()["url"])
        assert detail.json()["historical"]
        assert (await client.get(root, params={"branch": "main"})).json()["current"] is None


@pytest.mark.asyncio
async def test_owned_deletion_and_manual_edit_conflict():
    manifest = BrainManifest(generator="test", build_id=str(uuid4()), project_id=1, branch="main", source_commit="a" * 40,
                             brain_version=1, recipe_hash="r", source_fingerprint="f", files={"PROJECT_BRAIN.md": hashlib.sha256(b"old").hexdigest()}, coverage={}, issues=[])
    remotes = {"docs/project-brain/manifest.json": {"decoded_content": manifest.model_dump_json(), "last_commit_id": "m"},
               "PROJECT_BRAIN.md": {"decoded_content": "old", "last_commit_id": "c"}}
    client = MagicMock()
    client.get_repository_file = AsyncMock(side_effect=lambda pid, path, ref: remotes.get(path))
    actions, deletions = await publication_actions(client, 1, "head", {"docs/project-brain/manifest.json": "new"}, lambda *_: True)
    assert deletions == {"PROJECT_BRAIN.md"}
    assert next(a for a in actions if a["action"] == "delete")["last_commit_id"] == "c"
    remotes["PROJECT_BRAIN.md"]["decoded_content"] = "human edit"
    with pytest.raises(ValueError, match="人工"):
        await publication_actions(client, 1, "head", {"PROJECT_BRAIN.md": "new"}, lambda *_: True)
    original_hash = hashlib.sha256(remotes["docs/project-brain/manifest.json"]["decoded_content"].encode()).hexdigest()
    altered = manifest.model_copy(update={"files": {"docs/project-brain/human.md": hashlib.sha256(b"human").hexdigest()}})
    remotes["docs/project-brain/manifest.json"]["decoded_content"] = altered.model_dump_json()
    with pytest.raises(ValueError, match="所有权"):
        await publication_actions(client, 1, "head", {}, lambda bid, digest: digest == original_hash)


@pytest.mark.asyncio
async def test_lost_commit_response_reconciles_without_duplicate(monkeypatch):
    from src.core.project_brain.publisher import publish

    bid = str(uuid4())
    manifest = BrainManifest(generator="test", build_id=bid, project_id=1, branch="main", source_commit="a" * 40,
                             brain_version=1, recipe_hash="r", source_fingerprint="fp", files={}, coverage={}, issues=[])
    files = {"PROJECT_BRAIN.md": "owned docs", "docs/project-brain/manifest.json": manifest.model_dump_json()}
    store = MagicMock()
    store.read_manifest.return_value, store.repository.return_value = manifest, files
    client = MagicMock()
    client.get_branch = AsyncMock(return_value={"commit": {"id": "head"}, "protected": False})
    client.get_repository_file = AsyncMock(side_effect=lambda pid, path, ref: {"decoded_content": files[path], "last_commit_id": "published-sha"})
    client.create_brain_commit = AsyncMock()
    client.brain_request = AsyncMock(side_effect=lambda method, path, **kwargs: {"parent_ids": []} if not path.endswith(("/tree", "/diff")) else [])
    monkeypatch.setattr("src.core.project_brain.publisher.ArtifactStore", lambda: store)
    monkeypatch.setattr("src.core.project_brain.publisher.GitLabClient", lambda **kwargs: client)
    monkeypatch.setattr("src.core.project_brain.publisher.branch_fingerprint", AsyncMock(return_value="fp"))
    save = MagicMock()
    result = await publish(bid, 1, "main", {}, save, lambda *_: True)
    assert result["status"] == "published" and result["commit_sha"] == "published-sha"
    client.create_brain_commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("version,parameter", [("17.10.5", "merge_when_pipeline_succeeds"), ("17.11.0", "auto_merge"), ("18.4.0-ee", "auto_merge")])
async def test_gitlab_merge_version_and_sha_contract(monkeypatch, version, parameter):
    bodies = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"version": version})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"state": "opened"})
    client = GitLabClient()
    monkeypatch.setattr(client, "_client", lambda: httpx.AsyncClient(base_url="http://gitlab", transport=httpx.MockTransport(handler)))
    await client.merge_brain_mr(1, 2, "source-sha")
    assert bodies == [{"sha": "source-sha", "should_remove_source_branch": True, parameter: True}]


@pytest.mark.asyncio
async def test_webhook_registered_before_review_suppression_and_spoof_title(database, monkeypatch):
    from src.core.project_brain.webhook import handle_brain_event

    monkeypatch.setattr("src.core.project_brain.webhook.async_session", lambda: AsyncSessionAdapter(database))
    with database() as session:
        build = create_build(session, 1, "main", "group/project", "a" * 40)
        session.commit()
    payload = {"object_kind": "merge_request", "project": {"id": 1},
               "object_attributes": {"action": "merge", "target_branch": "main", "source_branch": "feature", "iid": 1, "title": "[AI-BRAIN] fake"}}
    assert not await handle_brain_event(payload)
    with database() as session:
        project = session.get(BrainProject, build.project_id)
        assert project.dirty_at and project.request_seq == build.request_seq and project.event_seq == 1


@pytest.mark.asyncio
async def test_mixed_business_robot_push_is_not_ignored(database, monkeypatch):
    from src.core.project_brain.webhook import handle_brain_event

    monkeypatch.setattr("src.core.project_brain.webhook.async_session", lambda: AsyncSessionAdapter(database))
    with database() as session:
        build = create_build(session, 1, "main", "group/project", "a" * 40)
        session.add(BrainPublication(build_id=build.id, target_branch="main", commit_sha="b" * 40))
        session.commit()
    monkeypatch.setattr(GitLabClient, "get_compare", AsyncMock(return_value={"diffs": [{"old_path": "PROJECT_BRAIN.md", "new_path": "PROJECT_BRAIN.md"}, {"old_path": "app.py", "new_path": "app.py"}]}))
    payload = {"object_kind": "push", "project": {"id": 1}, "ref": "refs/heads/main", "before": "a" * 40, "after": "b" * 40}
    assert not await handle_brain_event(payload)
    with database() as session:
        assert session.get(BrainProject, build.project_id).dirty_at


@pytest.mark.asyncio
async def test_webhook_commit_arrives_before_publish_response(database, monkeypatch):
    from src.core.project_brain.webhook import handle_brain_event

    monkeypatch.setattr("src.core.project_brain.webhook.async_session", lambda: AsyncSessionAdapter(database))
    with database() as session:
        build = create_build(session, 1, "main", "group/project", "a" * 40)
        record = BrainPublication(build_id=build.id, target_branch="main", status="publishing")
        session.add(record)
        session.commit()
    manifest = BrainManifest(generator="test", build_id=build.id, project_id=1, branch="main", source_commit=build.source_commit,
                             brain_version=1, recipe_hash="r", source_fingerprint="fp", files={"PROJECT_BRAIN.md": hashlib.sha256(b"owned").hexdigest()}, coverage={}, issues=[])
    manifest_text = manifest.model_dump_json()
    with database() as session:
        session.get(BrainBuild, build.id).coverage = {"manifest_sha256": hashlib.sha256(manifest_text.encode()).hexdigest()}
        session.commit()
    monkeypatch.setattr(GitLabClient, "brain_request", AsyncMock(side_effect=lambda method, path, **kwargs: [] if path.endswith("/tree") else {"message": f"[AI-BRAIN] 更新项目知识 ({build.id})\n"}))
    monkeypatch.setattr(GitLabClient, "get_repository_file", AsyncMock(side_effect=lambda pid, path, ref: {"decoded_content": manifest_text if path.endswith("manifest.json") else "owned"}))
    monkeypatch.setattr(GitLabClient, "get_compare", AsyncMock(return_value={"diffs": [{"old_path": "PROJECT_BRAIN.md", "new_path": "PROJECT_BRAIN.md"}]}))
    assert await handle_brain_event({"object_kind": "push", "project": {"id": 1}, "ref": "refs/heads/main", "before": "a" * 40, "after": "b" * 40})
    with database() as session:
        assert session.get(BrainPublication, record.id).commit_sha == "b" * 40
        assert session.get(BrainProject, build.project_id).dirty_at is None


@pytest.mark.asyncio
async def test_protected_branch_publication_keeps_ci_blocked_mr(monkeypatch):
    from src.core.project_brain.publisher import publish

    monkeypatch.setattr(settings, "PROJECT_BRAIN_MR_AUTO_MERGE", True)

    bid = str(uuid4())
    manifest = BrainManifest(generator="test", build_id=bid, project_id=1, branch="main", source_commit="a" * 40,
                             brain_version=1, recipe_hash="r", source_fingerprint="fp", files={}, coverage={}, issues=[])
    store = MagicMock()
    store.read_manifest.return_value = manifest
    store.repository.return_value = {"PROJECT_BRAIN.md": "owned", "docs/project-brain/manifest.json": manifest.model_dump_json()}
    client = MagicMock()
    client.get_branch = AsyncMock(side_effect=lambda pid, branch: {"commit": {"id": "head" if branch == "main" else "source-sha"}, "protected": True})
    client.get_repository_file = AsyncMock(side_effect=lambda pid, path, ref: {"decoded_content": store.repository.return_value[path]} if ref == "source-sha" and client.create_brain_commit.await_count else None)
    client.create_branch = AsyncMock(return_value={})
    client.create_brain_commit = AsyncMock(return_value={"id": "source-sha"})
    client.brain_request = AsyncMock(return_value=[])
    client.get_mr_changes = AsyncMock(return_value={"changes": [{"old_path": p, "new_path": p} for p in store.repository.return_value]})
    client.create_merge_request = AsyncMock(return_value={"iid": 4, "web_url": "https://gitlab/mr/4"})
    client.merge_brain_mr = AsyncMock(return_value={})
    monkeypatch.setattr("src.core.project_brain.publisher.ArtifactStore", lambda: store)
    monkeypatch.setattr("src.core.project_brain.publisher.GitLabClient", lambda **kwargs: client)
    monkeypatch.setattr("src.core.project_brain.publisher.branch_fingerprint", AsyncMock(return_value="fp"))
    result = await publish(bid, 1, "main", {}, lambda _: None, lambda *_: True)
    assert result["status"] == "mr_open"
    assert result["mr_url"] == "https://gitlab/mr/4"
    assert client.create_branch.call_args.args[1] == settings.PROJECT_BRAIN_TEMP_BRANCH_PREFIX + bid
    client.merge_brain_mr.assert_awaited_once_with(1, 4, "source-sha")


@pytest.mark.asyncio
async def test_stale_build_never_publishes(monkeypatch):
    from src.core.project_brain.publisher import publish

    store, client = MagicMock(), MagicMock()
    bid = str(uuid4())
    store.read_manifest.return_value = MagicMock(source_fingerprint="old", build_id=bid, project_id=1, branch="main")
    store.repository.return_value = {"docs/project-brain/manifest.json": "manifest"}
    client.get_branch = AsyncMock(return_value={"commit": {"id": "advanced"}})
    client.create_brain_commit = AsyncMock()
    monkeypatch.setattr("src.core.project_brain.publisher.ArtifactStore", lambda: store)
    monkeypatch.setattr("src.core.project_brain.publisher.GitLabClient", lambda **kwargs: client)
    monkeypatch.setattr("src.core.project_brain.publisher.branch_fingerprint", AsyncMock(return_value="new"))
    result = await publish(bid, 1, "main", {}, lambda _: None, lambda *_: True)
    assert result == {"status": "superseded", "source_changed": True}
    client.create_brain_commit.assert_not_awaited()


def test_migration_upgrade_and_downgrade_match_models():
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import MetaData, inspect

    spec = importlib.util.spec_from_file_location("brain_migration", Path(__file__).parents[2] / "alembic/versions/005_add_project_brain.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    next_spec = importlib.util.spec_from_file_location("brain_auto_migration", Path(__file__).parents[2] / "alembic/versions/006_brain_automatic_maintenance.py")
    next_module = importlib.util.module_from_spec(next_spec)
    next_spec.loader.exec_module(next_module)
    usage_spec = importlib.util.spec_from_file_location("brain_usage_migration", Path(__file__).parents[2] / "alembic/versions/007_brain_model_usage.py")
    usage_module = importlib.util.module_from_spec(usage_spec)
    usage_spec.loader.exec_module(usage_module)
    http_spec = importlib.util.spec_from_file_location("brain_http_migration", Path(__file__).parents[2] / "alembic/versions/008_brain_http_transport.py")
    http_module = importlib.util.module_from_spec(http_spec)
    http_spec.loader.exec_module(http_module)
    recovery_spec = importlib.util.spec_from_file_location("brain_recovery_migration", Path(__file__).parents[2] / "alembic/versions/009_brain_checkpoint_recovery.py")
    recovery_module = importlib.util.module_from_spec(recovery_spec)
    recovery_spec.loader.exec_module(recovery_module)
    control_spec = importlib.util.spec_from_file_location("brain_control_migration", Path(__file__).parents[2] / "alembic/versions/010_brain_execution_control.py")
    control_module = importlib.util.module_from_spec(control_spec)
    control_spec.loader.exec_module(control_module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        with operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            legacy = MetaData()
            legacy.reflect(bind=connection)
            for i, auto, historical in ((1, True, False), (2, False, False), (3, False, True)):
                connection.execute(legacy.tables["brain_projects"].insert().values(
                    id=i, gitlab_project_id=i, project_name=f"group/{i}", branch="main", auto_update=auto,
                    gitlab_sync=True, request_seq=1, next_version=2, created_at=utcnow()))
                connection.execute(legacy.tables["brain_builds"].insert().values(
                    id=str(uuid4()), project_id=i, source_commit="a" * 40, recipe_hash="r", mode="auto", request_seq=1,
                    brain_version=1, historical=historical, status="ready", coverage={}, total_prompt_tokens=0,
                    total_completion_tokens=0, total_cached_tokens=0, reserved_tokens=0, usage_estimated=False, created_at=utcnow()))
            next_module.upgrade()
            usage_module.upgrade()
            http_module.upgrade()
            recovery_module.upgrade()
            control_module.upgrade()
            from sqlalchemy import text

            assert connection.execute(text("SELECT maintenance_state FROM brain_projects ORDER BY id")).scalars().all() == ["active", "paused", "unmanaged"]
            assert connection.execute(text("SELECT usage_tracking_version FROM brain_builds")).scalars().all() == [0, 0, 0]
            assert connection.execute(text("SELECT execution_state FROM brain_builds")).scalars().all() == ["running"] * 3
            schema = inspect(connection)
            for model in (BrainProject, BrainBuild, BrainUnit, BrainPublication, BrainModelCall, BrainRecovery):
                assert {c["name"] for c in schema.get_columns(model.__tablename__)} == set(model.__table__.columns.keys())
            control_module.downgrade()
            recovery_module.downgrade()
            http_module.downgrade()
            usage_module.downgrade()
            next_module.downgrade()
            module.downgrade()
            assert inspect(connection).get_table_names() == []
    engine.dispose()


@pytest.mark.asyncio
async def test_repo_manager_fixed_sha_does_not_reset_review_workspace(repository, tmp_path, monkeypatch):
    from src.core.repo_manager import RepoManager

    initial = git(repository, "rev-parse", "HEAD")
    manager = RepoManager(str(tmp_path / "cache"))
    monkeypatch.setattr(manager, "_credential_url", lambda _: str(repository))
    first = await manager.ensure_snapshot(1, "group/project", "main", initial, str(tmp_path / "pin1"))
    (repository / "app/service.py").write_text("new_version = True\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "new head")
    second = await manager.ensure_snapshot(1, "group/project", "main", initial, str(tmp_path / "pin2"))
    assert first.fingerprint == second.fingerprint
    assert (repository / "app/service.py").read_text(encoding="utf-8") == "new_version = True\n"
    assert "lookup" in (Path(second.path) / "app/service.py").read_text(encoding="utf-8")


def test_symlink_git_object_is_never_exported(repository, tmp_path):
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repository, input=b"../../outside.py", capture_output=True, check=True).stdout.decode().strip()
    git(repository, "update-index", "--add", "--cacheinfo", f"120000,{blob},link.py")
    git(repository, "commit", "-m", "link fixture")
    snapshot = export_snapshot(repository, tmp_path / "safe", git(repository, "rev-parse", "HEAD"), 100)
    assert not snapshot.files["link.py"]["available"] and snapshot.files["link.py"]["mode"] == "120000"
    assert not (Path(snapshot.path) / "link.py").exists()
    assert any("符号链接" in issue for issue in snapshot.issues)


def test_dynamic_router_factory_not_reported_as_static_endpoint(repository, tmp_path):
    (repository / "app/main.py").write_text('from fastapi import FastAPI\nfrom app.routes import router\ndef factory():\n    app=FastAPI()\n    app.include_router(router, prefix="/conditional")\n    return app\n', encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "dynamic factory")
    snapshot = export_snapshot(repository, tmp_path / "factory", git(repository, "rev-parse", "HEAD"), 100)
    bundle = FactExtractor(snapshot).extract()
    assert not any(f.name.startswith("GET /conditional/") for f in bundle.facts)
    assert any("局部" in issue for issue in bundle.issues)


def test_budget_settles_actual_usage_and_releases_unused_reservation(database, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_BUILD_TOKENS", 100)
    with database() as session:
        build = create_build(session, 1, "main", "group/project", "a" * 40)
        session.commit()
        unit_id = session.scalar(select(BrainUnit.id))
    unit, build, project, token = claim_unit(unit_id)
    reserve_tokens(build.id, token, unit.id, 80)
    account_tokens(build.id, TokenUsage(20, 10), False, 80)
    reserve_tokens(build.id, token, unit.id, 70)
    with database() as session:
        actual = session.get(BrainBuild, build.id)
        assert actual.reserved_tokens == 100
        assert actual.total_prompt_tokens == 20 and actual.total_completion_tokens == 10


def test_dependency_body_change_invalidates_unchanged_caller_packet(repository, tmp_path):
    (repository / "src/api").mkdir(parents=True)
    (repository / "src/core").mkdir(parents=True)
    caller = 'from src.core.impl import job as perform\ndef handler():\n    return perform()\n'
    (repository / "src/api/entry.py").write_text(caller, encoding="utf-8")
    (repository / "src/core/impl.py").write_text('def job():\n    return "old"\n', encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "dependency")
    old_snapshot = export_snapshot(repository, tmp_path / "deps1", git(repository, "rev-parse", "HEAD"), 100)
    old = FactExtractor(old_snapshot).extract()
    old_packet = next(p for p in packets(old, old_snapshot) if p["module"] == "src/api")
    (repository / "src/core/impl.py").write_text('def job():\n    return "new"\n', encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "new behavior")
    new_snapshot = export_snapshot(repository, tmp_path / "deps2", git(repository, "rev-parse", "HEAD"), 100)
    new = FactExtractor(new_snapshot, old).extract()
    new_packet = next(p for p in packets(new, new_snapshot) if p["module"] == "src/api")
    # Complete provenance includes the new commit. Source content, scope and
    # evidence ranges of the caller are identical; its callee body changed.
    for packet in (old_packet, new_packet):
        for item in packet["items"]:
            for ref in item["evidence"]:
                ref.pop("commit_sha", None)
    assert old_packet == new_packet
    assert packet_hash(old, "recipe", old_packet) != packet_hash(new, "recipe", new_packet)


def test_java_entrypoint_and_parameter_values(repository, tmp_path):
    (repository / "java/Launch.java").write_text('''package example;
class Launch {
    public static void main(String[] args) {}
    public static int main(int count) { return count; }
    public void configured(@Value("x,y") String token) {}
}
''', encoding="utf-8")
    (repository / "web/default.js").write_text('function entry(password="x,y") {}', encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "launch")
    bundle = FactExtractor(export_snapshot(repository, tmp_path / "launch", git(repository, "rev-parse", "HEAD"), 100)).extract()
    entries = [f for f in bundle.facts if f.kind == "interface" and f.attributes["path"] == "java/Launch.java"]
    assert len(entries) == 1
    relation = next(r for r in bundle.relations if r.from_id == entries[0].id)
    target = next(f for f in bundle.facts if f.id == relation.to_id)
    assert "String[]" in target.attributes["signature"]
    signatures = [f.attributes["signature"] for f in bundle.facts if f.kind == "symbol"]
    assert all("x,y" not in signature and 'y"' not in signature for signature in signatures)


def test_recipe_changes_with_parser_runtime_and_gateway(monkeypatch):
    old = recipe_hash()
    monkeypatch.setattr("src.core.project_brain.recipes.version", lambda package: "new-parser-release")
    changed = recipe_hash()
    assert old != changed
    monkeypatch.setattr(settings, "VLLM_API_URL", "https://different-gateway.example/v1")
    assert changed != recipe_hash()


@pytest.mark.asyncio
async def test_shared_webhook_enrolls_branch_creation_before_review_skip(database, monkeypatch):
    from src.api.v1.webhooks import gitlab_webhook

    monkeypatch.setattr(settings, "GITLAB_WEBHOOK_SECRET", "fixture-secret")
    monkeypatch.setattr("src.core.project_brain.webhook.async_session", lambda: AsyncSessionAdapter(database))
    request = MagicMock()
    request.json = AsyncMock(return_value={"object_kind": "push", "project": {"id": 17, "path_with_namespace": "group/new"},
        "ref": "refs/heads/main", "before": "0" * 40, "after": "a" * 40, "commits": [], "total_commits_count": 0})
    result = await gitlab_webhook(request, "fixture-secret")
    assert result["status"] == "ignored"  # 审查沿用创建分支跳过规则，Brain 已独立登记。
    with database() as session:
        project = session.scalar(select(BrainProject))
        assert project.gitlab_project_id == 17 and project.maintenance_state == "active"
        assert project.event_seq == 1 and project.request_seq == 0 and project.dirty_at
        assert session.scalar(select(BrainBuild)) is None


@pytest.mark.asyncio
async def test_automatic_enrollment_requires_authenticated_webhook(database, monkeypatch):
    from fastapi import HTTPException
    from src.api.v1.webhooks import gitlab_webhook

    monkeypatch.setattr(settings, "GITLAB_WEBHOOK_SECRET", "")
    request = MagicMock(json=AsyncMock(return_value={"object_kind": "push"}))
    with pytest.raises(HTTPException) as error:
        await gitlab_webhook(request, None)
    assert error.value.status_code == 503
    with database() as session:
        assert session.scalar(select(BrainProject)) is None


def test_historical_archive_enrolls_on_event_but_paused_stays_paused(database):
    from src.core.project_brain.lifecycle import enrolled

    with database() as session:
        history = create_build(session, 18, "main", "group/archive", "a" * 40, historical=True)
        session.commit()
        project = session.get(BrainProject, history.project_id)
        assert project.maintenance_state == "unmanaged" and not enrolled(session, 18, "main")
        assert mark_dirty(session, 18, "main", name="group/archive")
        assert enrolled(session, 18, "main") and project.maintenance_state == "active"
        project.maintenance_state, project.auto_update = "paused", False
        session.commit()
        seq = project.event_seq
        assert not mark_dirty(session, 18, "main", name="group/archive")
        assert project.event_seq == seq and project.maintenance_state == "paused"


def test_duplicate_events_keep_running_lease_and_new_head_fences_late_completion(database):
    with database() as session:
        build = create_build(session, 1, "main", "group/project", "a" * 40)
        session.commit()
        unit_id = session.scalar(select(BrainUnit.id))
    unit, build, project, token = claim_unit(unit_id)
    with database() as session:
        mark_dirty(session, 1, "main")
        mark_dirty(session, 1, "main")
        current = session.get(BrainProject, project.id)
        original_seq = current.request_seq
        duplicate = create_build(session, 1, "main", "group/project", "a" * 40, observed_seq=current.event_seq)
        assert duplicate.id == build.id and current.request_seq == original_seq
        assert session.get(BrainUnit, unit.id).lease_token == token
        session.commit()
    assert _complete(unit.id, token, {"fingerprint": "fp"})
    with database() as session:
        next_unit = session.scalar(select(BrainUnit.id).where(BrainUnit.kind == "extract"))
    _, _, _, older_token = claim_unit(next_unit)
    with database() as session:
        mark_dirty(session, 1, "main")
        current = session.get(BrainProject, project.id)
        changed = create_build(session, 1, "main", "group/project", "b" * 40, observed_seq=current.event_seq)
        assert changed.request_seq == original_seq + 1
        session.commit()
    assert not _complete(next_unit, older_token, {})
    with database() as session:
        assert session.get(BrainBuild, build.id).status == "superseded"


@pytest.mark.asyncio
async def test_scheduler_resolves_target_head_and_does_not_clear_newer_event(database, monkeypatch):
    from src.tasks.brain_task import _schedule_project

    with database() as session:
        mark_dirty(session, 1, "main", name="group/project")
        session.commit()
        project = session.scalar(select(BrainProject))
        session.expunge_all()
    monkeypatch.setattr(GitLabClient, "get_branch", AsyncMock(return_value={"commit": {"id": "b" * 40}}))
    async def newer_event(*args):
        with database() as session:
            mark_dirty(session, 1, "main")
            session.commit()
        return "fp"
    monkeypatch.setattr("src.tasks.brain_task.branch_fingerprint", newer_event)
    await _schedule_project(project)
    with database() as session:
        current = session.get(BrainProject, project.id)
        assert current.dirty_at and current.event_seq == project.event_seq + 1
        assert session.scalar(select(BrainBuild)) is None


@pytest.mark.asyncio
async def test_missing_branch_fences_worker_and_preserves_version(database, monkeypatch):
    from src.tasks.brain_task import _schedule_project

    with database() as session:
        build = create_build(session, 1, "main", "group/project", "a" * 40)
        session.commit()
        project = session.get(BrainProject, build.project_id)
        session.expunge_all()
    response = httpx.Response(404, request=httpx.Request("GET", "http://gitlab/branch"))
    monkeypatch.setattr(GitLabClient, "get_branch", AsyncMock(side_effect=httpx.HTTPStatusError("missing", request=response.request, response=response)))
    await _schedule_project(project)
    with database() as session:
        current = session.get(BrainProject, project.id)
        assert current.source_status == "missing" and current.request_seq > build.request_seq
        unit_id = session.scalar(select(BrainUnit.id))
    assert claim_unit(unit_id) is None


@pytest.mark.asyncio
async def test_unchanged_content_reuses_ready_version_after_document_commit(database, monkeypatch):
    from src.tasks.brain_task import _schedule_project

    with database() as session:
        build = create_build(session, 1, "main", "group/project", "a" * 40)
        build.status, build.source_fingerprint, build.artifact_path = "ready", "fp", "stored"
        project = session.get(BrainProject, build.project_id)
        project.active_build_id = build.id
        mark_dirty(session, 1, "main")
        session.commit()
        session.expunge_all()
    monkeypatch.setattr(GitLabClient, "get_branch", AsyncMock(return_value={"commit": {"id": "b" * 40}}))
    monkeypatch.setattr(GitLabClient, "brain_request", AsyncMock(return_value={"id": "a" * 40}))
    monkeypatch.setattr("src.tasks.brain_task.branch_fingerprint", AsyncMock(return_value="fp"))
    monkeypatch.setattr(ArtifactStore, "repository", lambda *args: {})
    await _schedule_project(project)
    with database() as session:
        current = session.get(BrainProject, project.id)
        assert current.active_build_id == build.id and current.last_observed_head == "b" * 40
        assert current.request_seq == build.request_seq and not current.dirty_at
        assert len(session.scalars(select(BrainBuild)).all()) == 1
        assert freshness(current, session.get(BrainBuild, build.id)) == "fresh"


def test_flexible_document_layout_round_trips_manifest_and_links(snapshot, tmp_path):
    bundle = FactExtractor(snapshot).extract()
    compiled = [(m, Compilation(title="职责", claims=[{"text": "模块职责", "evidence_ids": next(f.source_ref_ids for f in bundle.facts if f.module_id == m)}],
                               unknowns=[], document_path=f"docs/project-brain/自由布局/{i}/职责说明.txt")) for i, m in enumerate(bundle.modules)]
    cards = assemble_cards(bundle, compiled, [compiled[0][1]])
    coverage = validate(bundle, cards, snapshot)
    manifest = BrainManifest(generator="test", build_id=str(uuid4()), project_id=1, branch="main", source_commit=bundle.source_commit,
                             brain_version=1, recipe_hash="r", source_fingerprint=bundle.source_fingerprint, files={}, coverage=coverage, issues=[])
    store = ArtifactStore(str(tmp_path / "brain"))
    store.finalize(manifest, bundle, cards, "http://gitlab/group/project")
    manifest = store.read_manifest(manifest.build_id)
    assert all(manifest.card_paths[m].startswith("docs/project-brain/自由布局/") for m in bundle.modules)
    assert "职责说明.txt" in " ".join(store.repository(manifest.build_id))
    assert all(owned_path(path) for path in manifest.files)
    assert "%E8%87%AA%E7%94%B1" in store.repository(manifest.build_id)["PROJECT_BRAIN.md"]


@pytest.mark.asyncio
async def test_writer_credentials_and_whole_batch_guard(tmp_path, monkeypatch):
    secret = tmp_path / "publisher-token"
    secret.write_text("fixture-publisher-token")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_ENABLED", True)
    monkeypatch.setattr(settings, "GITLAB_TOKEN", "fixture-comment-token")
    monkeypatch.setattr(settings, "GITLAB_READ_TOKEN", "fixture-read-token")
    monkeypatch.setattr(settings, "GITLAB_BRAIN_PUBLISH_TOKEN_FILE", str(secret))
    reader, reviewer, writer = GitLabClient(purpose="read"), GitLabClient(), GitLabClient(purpose="brain_publish")
    assert reader._headers["PRIVATE-TOKEN"] == "fixture-read-token"
    assert reviewer._headers["PRIVATE-TOKEN"] == "fixture-comment-token"
    assert writer._headers["PRIVATE-TOKEN"] == "fixture-publisher-token"
    with pytest.raises(ValueError):
        await reader.create_branch(1, "ai-brain/fixture", "sha")
    with pytest.raises(ValueError):
        await reviewer.create_commit(1, "main", "old queued task", [{"action": "update", "file_path": "AI_README.md", "content": "docs"}])
    with pytest.raises(ValueError):
        await writer.brain_request("POST", "/projects/1/repository/commits")
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "sha"})
    monkeypatch.setattr(writer, "_client", lambda: httpx.AsyncClient(base_url="http://gitlab", transport=httpx.MockTransport(transport)))
    verified = {"docs/project-brain/custom/narrative.txt": "docs"}
    with pytest.raises(ValueError):
        await writer.create_brain_commit(1, "main", str(uuid4()), [
            {"action": "create", "file_path": next(iter(verified)), "content": "docs"},
            {"action": "create", "file_path": "src/main.py", "content": "bad"}], verified, set())
    assert calls == []
    await writer.create_brain_commit(1, "main", str(uuid4()), [{"action": "create", "file_path": next(iter(verified)), "content": "docs"}], verified, set())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_tree_guard_rejects_symlink_ancestor_before_writing():
    from src.core.project_brain.write_policy import WriteConflict, assert_tree_safe

    client = MagicMock(brain_request=AsyncMock(return_value=[{"path": "docs", "type": "blob", "mode": "120000"}]))
    with pytest.raises(WriteConflict, match="祖先"):
        await assert_tree_safe(client, 1, "sha", {"docs/project-brain/arbitrary/free.txt"})


def test_layout_change_requires_both_manifest_owned_endpoints():
    from src.core.project_brain.write_policy import WriteConflict, validate_changes

    change = {"old_path": "docs/project-brain/old-name.md", "new_path": "docs/project-brain/自由名称.txt",
              "renamed_file": True, "a_mode": "100644", "b_mode": "100644"}
    validate_changes([change], {change["new_path"]}, {change["old_path"]})
    with pytest.raises(WriteConflict):
        validate_changes([change], {change["new_path"]}, set())
    with pytest.raises(WriteConflict):
        validate_changes([{**change, "old_path": "src/business.py"}], {change["new_path"]}, {"src/business.py"})


def test_duplicate_pending_event_defers_publication_without_superseding(database, monkeypatch):
    from src.tasks.brain_publication_task import publish_brain

    with database() as session:
        build = create_build(session, 1, "main", "group/project", "a" * 40)
        build.status, build.artifact_path = "ready", "stored"
        session.get(BrainProject, build.project_id).active_build_id = build.id
        record = BrainPublication(build_id=build.id, target_branch="main")
        session.add(record)
        mark_dirty(session, 1, "main")
        session.commit()
    publish = AsyncMock()
    monkeypatch.setattr("src.tasks.brain_publication_task.publish", publish)
    assert publish_brain.run(record.id) == {"status": "deferred"}
    publish.assert_not_awaited()
    with database() as session:
        assert session.get(BrainPublication, record.id).status == "pending"
        assert session.get(BrainBuild, build.id).status == "ready"


def test_server_hook_checks_intermediate_commits_even_when_reverted(repository, tmp_path):
    hook = Path(__file__).parents[2] / "scripts/gitlab/project_brain_pre_receive.py"
    config = tmp_path / "guard.json"
    config.write_text(json.dumps({"publisher_gl_ids": ["user-77"], "target_branches": ["main"]}))
    old = git(repository, "rev-parse", "HEAD")
    docs = repository / "docs/project-brain/free-form.txt"
    docs.parent.mkdir(parents=True)
    docs.write_text("knowledge")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "knowledge")
    new = git(repository, "rev-parse", "HEAD")
    def check(before, after, ref="refs/heads/main", identity="user-77"):
        import sys
        return subprocess.run([sys.executable, str(hook), str(config)], cwd=repository, input=f"{before} {after} {ref}\n",
                              env={**os.environ, "GL_ID": identity}, capture_output=True, text=True)
    assert check(old, new).returncode == 0
    assert check("0" * 40, new, "refs/heads/ai-brain/" + str(uuid4())).returncode == 0
    assert check(new, old).returncode == 1
    assert check(new, "0" * 40).returncode == 1
    before = new
    source = repository / "app/service.py"
    original = source.read_text()
    source.write_text("malicious = True\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "unsafe intermediate")
    source.write_text(original)
    git(repository, "add", ".")
    git(repository, "commit", "-m", "restore business file")
    assert check(before, git(repository, "rev-parse", "HEAD")).returncode == 1
    assert check(before, git(repository, "rev-parse", "HEAD"), identity="user-88").returncode == 0


@pytest.mark.asyncio
async def test_sensitive_unavailable_file_path_stops_artifact(snapshot, monkeypatch):
    from src.tasks.brain_task import _safe_bundle

    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", False)
    bundle = FactExtractor(snapshot).extract()
    bundle.files["private-host.bin"] = {"available": False, "blob_sha": "a" * 40, "lines": 0}
    guard = CompilationGuard(lambda *args: None, lambda *args: None)
    guard.filter._dynamic_values["private-host"] = "DOMAIN"
    try:
        with pytest.raises(ValueError, match="源码路径包含敏感值"):
            await _safe_bundle(bundle, guard)
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_late_privacy_discovery_cleans_earlier_fact_and_checkpoint(snapshot, monkeypatch):
    from src.tasks.brain_task import _safe_bundle

    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", False)
    bundle = FactExtractor(snapshot).extract()
    guard = CompilationGuard(lambda *args: None, lambda *args: None)
    original = guard.clean

    async def discover(text):
        result = await original(text)
        # A late discovery after all classifications still has to be reapplied
        # to earlier declarations and their observation mirrors.
        guard.filter._dynamic_values["lookup"] = "CUSTOM"
        return result

    guard.clean = discover
    try:
        safe = await _safe_bundle(bundle, guard)
        assert all("lookup" not in f.name for f in safe.facts)
        assert all("lookup" not in f["name"] for observation in safe.observations.values() for f in observation["facts"])
    finally:
        await guard.close()
