"""Actual Worker checkpoints recover with only synthetic network adapters."""
import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest
from openai.types.chat import ChatCompletionMessage
from sqlalchemy import select

from src.config import settings
from src.core.gitlab_client import GitLabClient
from src.core.llm_client import ChatResult, LLMClient, TokenUsage
from src.core.project_brain.artifacts import ArtifactStore
from src.core.project_brain.lifecycle import create_build
from src.core.project_brain.recovery import create_recovery, verify_dependencies
from src.core.project_brain.snapshot import export_snapshot
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainRecovery, BrainUnit
from src.tasks import brain_task
from tests.unit.test_brain_strict_transport import formatter_result
from tests.unit.test_project_brain import database, git, repository  # noqa: F401


def snapshot_rows(session, build_id):
    build = session.get(BrainBuild, build_id)
    rows = {"build": {c.name: copy.deepcopy(getattr(build, c.name)) for c in BrainBuild.__table__.columns}}
    for name, model in (("units", BrainUnit), ("calls", BrainModelCall)):
        rows[name] = [{c.name: copy.deepcopy(getattr(row, c.name)) for c in model.__table__.columns}
            for row in session.scalars(select(model).where(model.build_id == build_id).order_by(model.id))]
    return rows


@pytest.mark.parametrize("credential", [False, True])
def test_failed_build_recovers_completed_packets_and_seals_without_rebilling_parent(database, repository, tmp_path, monkeypatch, credential):
    secret = "synthetic_recovery_embedded_credential"
    if credential:
        source = repository / secret / "settings.py"
        source.parent.mkdir()
        source.write_text(f'SERVICE_PASSWORD = "{secret}"\n', encoding="utf-8")
        git(repository, "add", ".")
        git(repository, "commit", "-m", "synthetic hardcoded credential")
    sha = git(repository, "rev-parse", "HEAD")
    for key, value in {"PROJECT_BRAIN_BASE_DIR": str(tmp_path / "public"),
        "PROJECT_BRAIN_WORK_DIR": str(tmp_path / "private"), "PROJECT_BRAIN_MODEL_NAME": "synthetic-semantic",
        "VLLM_MODEL_NAME": "synthetic-privacy", "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE": "source_bound",
        "PROJECT_BRAIN_GITLAB_SYNC": False, "PROJECT_BRAIN_MAX_BUILD_TOKENS": 2000000,
        "PROJECT_BRAIN_PACKET_MAX_TOKENS": 32000, "PRIVACY_FILTER_ENABLED": True,
        "PRIVACY_TOOL_SCAN_MODE": "hybrid", "LLM_EXTRA_BODY": "", "PROJECT_BRAIN_PRIVACY_EXTRA_BODY": "",
        "PROJECT_BRAIN_SEMANTIC_EXTRA_BODY": "", "PRIVACY_CUSTOM_PATTERNS": ""}.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(brain_task, "_SOURCE_CACHE", None)
    monkeypatch.setattr(brain_task, "dispatch_pending", lambda *args: None)
    monkeypatch.setattr(brain_task.execute_brain_unit, "apply_async", lambda *args, **kwargs: None)

    async def project(self, project_id):
        return {"path_with_namespace": "synthetic/project", "web_url": "https://gitlab.invalid/synthetic/project"}
    async def ensure(self, project_id, project_path, branch, commit, destination):
        assert commit == sha
        return export_snapshot(repository, Path(destination), commit, 100)
    monkeypatch.setattr(GitLabClient, "get_project", project)
    monkeypatch.setattr("src.core.repo_manager.RepoManager.ensure_snapshot", ensure)
    observed, current = [], {"build": None, "unit": None, "kind": None}

    async def chat(self, messages, **kwargs):
        if self._privacy_filter:
            messages = self._privacy_filter.sanitize_messages(messages)
        observed.append((current["build"], current["unit"], current["kind"], self._model))
        if self._model == "synthetic-privacy":
            found = secret in json.dumps(messages, ensure_ascii=False)
            response = {"items": [{"value": secret, "type": "SECRET"}] if found else []}
            return ChatResult(ChatCompletionMessage(role="assistant", content=json.dumps(response)), TokenUsage(20, 10), "stop")
        assert secret not in json.dumps(messages, ensure_ascii=False)
        slots = kwargs["tools"][0]["function"]["parameters"]["properties"]["slots"]["properties"]
        return formatter_result(json.dumps({"title": "Synthetic component overview", "slots": {
            slot: {"texts": ["Responsibilities follow the complete provided declarations."], "unknowns": []}
            for slot in slots}, "unknowns": ["Runtime behavior requires verification."], "document_path": None}))
    monkeypatch.setattr(LLMClient, "chat", chat)

    def step(build_id):
        with database() as session:
            unit = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build_id,
                BrainUnit.status == "pending").order_by(BrainUnit.id))
            if unit is None:
                return None
            current.update(build=build_id, unit=unit.id, kind=unit.kind)
        outcome = brain_task.execute_brain_unit.run(current["unit"])
        assert outcome["status"] in {"completed", "deferred"}, (current["kind"], outcome)
        return current["kind"], current["unit"]

    with database() as session:
        parent = create_build(session, 480, "main", "synthetic/project", sha, "full", historical=True)
        session.commit()
        parent_id = parent.id
    saved_keys = set()
    for _ in range(150):
        result = step(parent_id)
        if result and result[0] == "compile":
            with database() as session:
                unit = session.get(BrainUnit, result[1])
                if unit.status == "completed":
                    saved_keys.add(unit.key)
                    if len(saved_keys) == 2:
                        break
    else:
        pytest.fail("parent did not produce a completed compilation")
    with database() as session:
        parent = session.get(BrainBuild, parent_id)
        owner = session.get(BrainProject, parent.project_id)
        owner.maintenance_state, owner.auto_update, owner.gitlab_sync = "paused", False, False
        pending = session.scalar(select(BrainUnit).where(BrainUnit.build_id == parent_id,
            BrainUnit.status == "pending", BrainUnit.kind == "compile").order_by(BrainUnit.id))
        assert pending is not None
        unknown_id = str(uuid4())
        session.add(BrainModelCall(id=unknown_id, build_id=parent_id, unit_id=pending.id,
            lease_token=str(uuid4()), stage="compile", role="privacy", purpose="semantic_output",
            requested_model="synthetic-privacy", attempt=1, state="unknown", reserved_tokens=7,
            prompt_tokens=0, completion_tokens=0, cached_tokens=0, usage_estimated=False))
        parent.reserved_tokens += 7
        parent.status, parent.error_message = "failed", "failure_code=model_calls_incomplete"
        brain_task._cancel_unfinished(session, parent_id)
        session.commit()
        before = snapshot_rows(session, parent_id)
        child = create_recovery(session, parent_id)
        session.commit()
        child_id = child.id
    for _ in range(250):
        if step(child_id) is None:
            break
    else:
        pytest.fail("recovery graph did not converge")
    with database() as session:
        child = session.get(BrainBuild, child_id)
        units = session.scalars(select(BrainUnit).where(BrainUnit.build_id == child_id)).all()
        assert child.status == "ready", [(u.kind, u.status, u.error_message) for u in units]
        record = session.get(BrainRecovery, child_id)
        assert record.imports
        for identifier, proof in record.imports.items():
            saved = next(u for u in units if u.id == int(identifier))
            assert saved.status == "completed" and saved.key in saved_keys
            assert not [item for item in observed if item[0] == child_id and item[1] == saved.id]
        assert any(u.result.get("cache_hits", 0) for u in units if u.kind == "privacy")
        assert record.status == "completed" and child.historical
        assert snapshot_rows(session, parent_id) == before
        assert session.get(BrainModelCall, unknown_id).state == "unknown"
        assert not session.get(BrainProject, child.project_id).active_build_id
        assert all(c.state == "settled" for c in session.scalars(select(BrainModelCall).where(BrainModelCall.build_id == child_id)))
        verify_dependencies(session, child)
    store = ArtifactStore()
    assert store.export_path(child_id).is_file()
    manifest = store.read_manifest(child_id)
    assert manifest.coverage["recovery_reused_compile_units"] >= 1
    assert manifest.coverage["recovery_historical_unknown_reserved_tokens"] == 7
    assert manifest.source_commit == sha
    if credential:
        for path in store.directory(child_id).rglob("*"):
            if path.is_file() and path.suffix != ".zip":
                assert secret.encode() not in path.read_bytes()
    else:
        # A real new Worker guard can receive a valid late discovery after a
        # child is ready. Revoke first, without pretending another SDK call or
        # clearing the parent's unrelated unknown charge.
        with database() as session:
            old_unit = session.scalar(select(BrainUnit).where(BrainUnit.build_id == parent_id,
                BrainUnit.kind == "privacy").order_by(BrainUnit.id))
        late_guard = brain_task._guard(old_unit, "unused-closed-lease", new_mode=True, project_id=480)
        try:
            late = "synthetic_late_recovery_value"
            late_guard.ledger.remember_policy([{"value": late, "type": "SECRET"}],
                [{"field": "value", "text": late}], kind="deterministic_rule",
                scope=late_guard.ledger.digest("late-policy"),
                context={"build_id": parent_id, "unit_id": old_unit.id, "stage": old_unit.kind})
        finally:
            import asyncio
            asyncio.run(late_guard.close())
        with database() as session:
            child = session.get(BrainBuild, child_id)
            assert child.status == "failed"
            assert session.get(BrainRecovery, child_id).status == "invalidated"
            assert session.get(BrainModelCall, unknown_id).state == "unknown"
            assert not (session.get(BrainBuild, parent_id).coverage or {}).get("recovery_write_intents")
