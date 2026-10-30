"""An unverified fact checkpoint must never reach a synthesis model."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.config import settings
from src.core.project_brain.artifacts import ArtifactStore
from src.core.project_brain.extractor import FactExtractor
from src.core.project_brain.lifecycle import recipe_hash
from src.core.project_brain.trusted_packet import checkpoint_digest
from src.tasks.brain_task import _execute
from tests.unit.test_project_brain import repository, snapshot  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["hash", "commit", "recipe", "privacy", "missing_finalize"])
async def test_summary_authenticates_fact_checkpoint_before_paid_generation(snapshot, monkeypatch, tmp_path, fault):
    bundle = FactExtractor(snapshot).extract()
    build = SimpleNamespace(id=str(uuid4()), recipe_hash=recipe_hash(), source_commit=snapshot.commit_sha)
    finalized = SimpleNamespace(kind="finalize", status="completed", result={
        "bundle_hash": checkpoint_digest(bundle.model_dump()), "source_commit": build.source_commit,
        "recipe_hash": build.recipe_hash})
    privacy = SimpleNamespace(kind="privacy", status="completed")
    inputs = [finalized, privacy]
    if fault == "hash":
        finalized.result["bundle_hash"] = "0" * 64
    elif fault == "commit":
        finalized.result["source_commit"] = "b" * 40
    elif fault == "recipe":
        finalized.result["recipe_hash"] = "c" * 64
    elif fault == "privacy":
        privacy.status = "pending"
    else:
        inputs.remove(finalized)

    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def scalars(self, *args): return SimpleNamespace(all=lambda: inputs)

    guard = SimpleNamespace(close=AsyncMock())
    model = AsyncMock()
    monkeypatch.setattr(settings, "PROJECT_BRAIN_BASE_DIR", str(tmp_path / "brain"))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr("src.tasks.brain_task.SyncSession", Session)
    monkeypatch.setattr("src.tasks.brain_task._guard", lambda *args: guard)
    monkeypatch.setattr("src.tasks.brain_task.read_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(ArtifactStore, "read_work", lambda *args: bundle.model_dump())
    monkeypatch.setattr("src.tasks.brain_task.compile_packet", model)
    with pytest.raises(ValueError, match="事实隐私检查点"):
        await _execute(SimpleNamespace(kind="module_summary"), build, SimpleNamespace(), "lease")
    model.assert_not_awaited()
    guard.close.assert_awaited_once()
