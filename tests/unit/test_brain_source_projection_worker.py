"""Run the actual fixed-SHA worker graph with mock-only network boundaries."""

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock
import zipfile

import httpx
import pytest
from openai.types.chat import ChatCompletionMessage
from sqlalchemy import select

from src.config import settings
from src.core.gitlab_client import GitLabClient
from src.core.llm_client import ChatResult, LLMClient, TokenUsage
from src.core.project_brain.artifacts import ArtifactStore
from src.core.project_brain.lifecycle import create_build
from src.core.project_brain.lifecycle import recipe_hash
from src.core.project_brain.privacy import PrivacyLedger
from src.core.project_brain.public_schema import PublicBrainManifest
from src.core.project_brain.snapshot import export_snapshot
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainPublication, BrainUnit
from src.tasks import brain_task
from tests.unit.test_brain_strict_transport import formatter_result
from tests.unit.test_project_brain import database, git, repository  # noqa: F401


@pytest.mark.parametrize("inherit_only", [False, True])
def test_original_commit_with_hardcoded_credential_builds_safe_formal_knowledge(database, repository, tmp_path, monkeypatch, inherit_only):
    """No bypasses: real leases, scanning, compile, planner, seal and ZIP."""
    secret = "synthetic_collision_credential"
    source_file = repository / secret / "settings.py"
    source_file.parent.mkdir()
    source_file.write_text('import os\nSERVICE_PASSWORD = os.getenv("SERVICE_PASSWORD", "' + secret + '")\n'
                           'def configured():\n    return SERVICE_PASSWORD\n', encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "synthetic fixed credential collision")
    sha = git(repository, "rev-parse", "HEAD")
    source_bytes = source_file.read_bytes()
    monkeypatch.setattr(settings, "PROJECT_BRAIN_BASE_DIR", str(tmp_path / "public"))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path / "private"))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MODEL_NAME", "synthetic-semantic")
    monkeypatch.setattr(settings, "VLLM_MODEL_NAME", "synthetic-privacy")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_GITLAB_SYNC", False)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_BUILD_TOKENS", 1000000)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    monkeypatch.setattr(settings, "PRIVACY_CUSTOM_PATTERNS", "")
    monkeypatch.setattr(settings, "LLM_EXTRA_BODY", "")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_EXTRA_BODY", "")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_EXTRA_BODY", "")
    # Capture the real configured recipe once. Tests share a workspace with
    # implementation agents; an unrelated on-disk edit between unit leases
    # must not turn this mock orchestration test into a deployment-change test.
    fixed_recipe = recipe_hash()
    monkeypatch.setattr(brain_task, "recipe_hash", lambda: fixed_recipe)
    monkeypatch.setattr("src.core.project_brain.lifecycle.recipe_hash", lambda: fixed_recipe)
    monkeypatch.setattr(brain_task, "_SOURCE_CACHE", None)
    monkeypatch.setattr(brain_task, "dispatch_pending", lambda *args: None)
    monkeypatch.setattr(brain_task.execute_brain_unit, "apply_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(GitLabClient, "get_project", AsyncMock(return_value={
        "path_with_namespace": "synthetic/project", "web_url": "https://gitlab.invalid/synthetic/project"}))
    async def ensure(self, project_id, project_path, branch, commit, destination):
        assert commit == sha
        return export_snapshot(repository, Path(destination), commit, 100)
    monkeypatch.setattr("src.core.repo_manager.RepoManager.ensure_snapshot", ensure)
    async def deny_async(*args, **kwargs):
        raise AssertionError("real HTTP is forbidden in this synthetic test")
    def deny_sync(*args, **kwargs):
        raise AssertionError("real HTTP is forbidden in this synthetic test")
    monkeypatch.setattr(httpx.AsyncClient, "send", deny_async)
    monkeypatch.setattr(httpx.Client, "send", deny_sync)
    observed = {"privacy": 0, "semantic": 0, "excerpt_detection": 0}
    async def chat(self, messages, **kwargs):
        if self._privacy_filter:
            messages = self._privacy_filter.sanitize_messages(messages)
        payload = json.loads(messages[1]["content"])
        if self._model == "synthetic-privacy":
            observed["privacy"] += 1
            found = any(secret in text for text in payload.get("source_excerpt", []))
            if inherit_only:
                assert secret not in json.dumps(messages, ensure_ascii=False)
                found = False  # A classifier that never rediscovers the prior value.
            observed["excerpt_detection"] += int(found)
            return ChatResult(ChatCompletionMessage(role="assistant", content=json.dumps({"items": [
                {"value": secret, "type": "SECRET"}] if found else []})), TokenUsage(20, 10), "stop")
        assert self._model == "synthetic-semantic"
        assert secret not in json.dumps(messages, ensure_ascii=False)
        with database() as session:
            barriers = session.scalars(select(BrainUnit.status).where(BrainUnit.kind == "source_privacy")).all()
            assert barriers and all(state == "completed" for state in barriers)
        observed["semantic"] += 1
        slots = kwargs["tools"][0]["function"]["parameters"]["properties"]["slots"]["properties"]
        return formatter_result(json.dumps({"title": "Complete component responsibilities", "slots": {
            slot: {"texts": ["Explains responsibilities from the complete given source."], "unknowns": []}
            for slot in slots}, "unknowns": ["Runtime configuration remains unverified."], "document_path": None}))
    monkeypatch.setattr(LLMClient, "chat", chat)
    old_ledger = PrivacyLedger(tmp_path / "unrelated-old-private")
    old_ledger.remember([{"value": "unrelated-old-value", "type": "SECRET"}])
    old_index_digest = hashlib.sha256(old_ledger.index.read_bytes()).hexdigest()
    with database() as session:
        build = create_build(session, 480, "main", "synthetic/project", sha, mode="full")
        session.flush()
        old = BrainBuild(project_id=build.project_id, source_commit="f" * 40, recipe_hash="old-recipe",
            mode="full", generation=0, request_seq=0, brain_version=0, status="failed",
            total_prompt_tokens=13, total_completion_tokens=7, reserved_tokens=777)
        session.add(old)
        session.commit()
        build_id, old_id = build.id, old.id
        if inherit_only:
            legacy_extract = BrainUnit(build_id=old_id, key="extract", kind="extract", status="completed", result={})
            session.add(legacy_extract)
            session.commit()
            parent_ledger = PrivacyLedger(ArtifactStore().directory(old_id, "work"))
            parent_ledger.remember_scan([{"value": secret, "type": "SECRET"}],
                [{"field": "source_excerpt", "text": secret}], call_id="00000000-0000-0000-0000-000000000001",
                scope=parent_ledger.digest("legacy-input"), packet=parent_ledger.digest("legacy-packet"),
                context={"build_id": old_id, "unit_id": legacy_extract.id, "stage": "extract"})
            parent_digest = hashlib.sha256(parent_ledger.index.read_bytes()).hexdigest()
    kinds = []
    for _ in range(200):
        with database() as session:
            pending = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build_id,
                BrainUnit.status == "pending").order_by(BrainUnit.id))
            if pending is None:
                break
            unit_id, kind = pending.id, pending.kind
        kinds.append(kind)
        outcome = brain_task.execute_brain_unit.run(unit_id)
        assert outcome["status"] in {"completed", "deferred"}, (kind, outcome)
    else:
        raise AssertionError("synthetic worker graph failed to converge")
    with database() as session:
        build = session.get(BrainBuild, build_id)
        units = session.scalars(select(BrainUnit).where(BrainUnit.build_id == build_id)).all()
        project = session.get(BrainProject, build.project_id)
        assert build.status == "ready", [(u.kind, u.status, u.error_message) for u in units]
        assert all(unit.status == "completed" for unit in units)
        assert project.active_build_id == build_id
        assert session.scalar(select(BrainPublication.id).where(BrainPublication.build_id == build_id)) is None
        assert build.total_prompt_tokens > 0 and build.total_completion_tokens > 0
        assert all(call.state == "settled" for call in session.scalars(select(BrainModelCall).where(BrainModelCall.build_id == build_id)))
        old = session.get(BrainBuild, old_id)
        assert (old.total_prompt_tokens, old.total_completion_tokens, old.reserved_tokens, old.status) == (13, 7, 777, "failed")
    assert {"scan", "extract", "privacy", "finalize", "source_privacy", "compile", "planning", "module_summary", "project_summary", "assemble"} <= set(kinds)
    assert (observed["excerpt_detection"] == 0 if inherit_only else observed["excerpt_detection"] >= 1)
    assert observed["privacy"] > 0 and observed["semantic"] > 0
    if inherit_only:
        assert hashlib.sha256(parent_ledger.index.read_bytes()).hexdigest() == parent_digest
    store = ArtifactStore()
    manifest = store.read_manifest(build_id)
    assert isinstance(manifest, PublicBrainManifest) and manifest.schema_version == "2.0"
    from scripts.brain_quality_audit import _history_integrity
    identity_path = tmp_path / "private/source-identities/480/1/identity.key"
    audited = _history_integrity(manifest.model_dump(), identity_path.read_bytes(), identity_path,
        tmp_path / "private/source-audits" / build_id, None)
    assert audited["baseline_authenticated"] and audited["parent_count"] == int(inherit_only)
    if inherit_only:
        assert manifest.coverage["privacy_history_parents"] == 1
        assert manifest.coverage["privacy_policy_findings"] >= 1
    assert store.read_cards(build_id)
    public_root = store.directory(build_id)
    with zipfile.ZipFile(public_root / "knowledge.zip") as archive:
        for name in archive.namelist():
            assert secret not in name
            assert secret.encode() not in archive.read(name)
            assert "identity.key" not in name and "fingerprints.json" not in name
    for public_file in public_root.rglob("*"):
        if public_file.is_file() and public_file.suffix != ".zip":
            assert secret.encode() not in public_file.read_bytes()
    assert source_file.read_bytes() == source_bytes and git(repository, "rev-parse", "HEAD") == sha
    assert hashlib.sha256(old_ledger.index.read_bytes()).hexdigest() == old_index_digest
