"""Persistent unit creation order survives arbitrary database read/update order."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from src.config import settings
from src.core.project_brain.artifacts import ArtifactStore
from src.core.project_brain.extractor import FactExtractor
from src.core.project_brain.lifecycle import claim_unit, create_build
from src.core.project_brain.schema import Compilation
from src.core.project_brain.summary import next_summary_tasks
from src.core.project_brain.trusted_packet import checkpoint_digest
from src.models.brain import BrainProject, BrainUnit
from src.tasks.brain_task import _advance, _complete, _execute
from tests.unit.test_brain_hierarchy_quality import completed_summary_unit
from tests.unit.test_project_brain import database, repository, snapshot  # noqa: F401


def compiled_unit(build_id, index, fact, *, document_path=None):
    compilation = Compilation(title=f"编译单元{index}",
        claims=[{"text": f"编译结论{index}", "evidence_ids": fact.source_ref_ids}],
        unknowns=[f"未知项{index}"], document_path=document_path)
    return BrainUnit(build_id=build_id, key=f"compile:{index}", kind="compile",
                     payload={"module_id": fact.module_id, "index": index, "fact_ids": [fact.id]},
                     input_hash=checkpoint_digest([index, fact.id]), status="pending",
                     result=compilation.model_dump())


def test_summary_pack_order_follows_creation_not_unordered_database_reads(database, snapshot, monkeypatch):
    bundle = FactExtractor(snapshot).extract()
    fact = bundle.facts[0]
    monkeypatch.setattr(ArtifactStore, "read_work", lambda *args: bundle.model_dump())
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_EXPLANATION_SLOTS", 2)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    monkeypatch.setattr("src.core.project_brain.summary.MAX_SUMMARY_CHILDREN", 2)
    monkeypatch.setattr("src.tasks.brain_task.dispatch_pending", lambda *args: None)
    monkeypatch.setattr("src.tasks.brain_task.read_snapshot", lambda *args: snapshot)
    with database() as session:
        session.execute(text("PRAGMA reverse_unordered_selects=ON"))
        build = create_build(session, 1, "main", "fixture/project", snapshot.commit_sha)
        # This ordering fixture supplies legacy canonical checkpoints directly;
        # v2's authenticated catalog/projection pipeline has its own full test.
        build.coverage = {}
        scan = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id, BrainUnit.kind == "scan"))
        scan.status, scan.result = "completed", {"fingerprint": snapshot.fingerprint}
        units = [compiled_unit(build.id, index, fact) for index in range(3)]
        for module in sorted(set(bundle.modules) - {fact.module_id}):
            module_fact = next(value for value in bundle.facts if value.module_id == module)
            unit = compiled_unit(build.id, len(units), module_fact)
            unit.status = "completed"
            units.append(unit)
        session.add_all(units)
        session.add(BrainUnit(build_id=build.id, kind="privacy", key="privacy:0", status="completed", result={}))
        session.add(BrainUnit(build_id=build.id, kind="finalize", key="finalize", status="completed", result={
            "bundle_hash": checkpoint_digest(bundle.model_dump()), "source_commit": build.source_commit,
            "recipe_hash": build.recipe_hash, "packets": [{"index": unit.payload["index"],
                "module_id": unit.payload["module_id"], "fact_ids": unit.payload["fact_ids"],
                "hash": unit.input_hash} for unit in units]}))
        session.commit()
        # Finish out of creation order, as independent workers naturally do.
        for index in (2, 0, 1):
            units[index].status = "completed"
            session.commit()
        _advance(session, build, units[1].id)
        session.commit()
        planning_id = session.scalar(select(BrainUnit.id).where(
            BrainUnit.build_id == build.id, BrainUnit.kind == "planning"))
        assert planning_id is not None
        assert session.scalar(select(BrainUnit.id).where(
            BrainUnit.build_id == build.id, BrainUnit.kind == "module_summary")) is None
    planning, planning_build, project, token = claim_unit(planning_id)
    result = asyncio.run(_execute(planning, planning_build, project, token))
    assert _complete(planning.id, token, result)
    with database() as session:
        summaries = session.scalars(select(BrainUnit).where(
            BrainUnit.build_id == build.id, BrainUnit.kind == "module_summary").order_by(BrainUnit.id)).all()
        summaries = [unit for unit in summaries if unit.payload["module_id"] == fact.module_id]
        assert len(summaries) == 2
        items = [item for unit in summaries for item in unit.payload["items"]]
        assert [item["fact"]["claims"][0]["text"] for item in items] == [f"编译结论{i}" for i in range(3)]
        assert [item["fact"]["unknowns"] for item in items] == [[f"未知项{i}"] for i in range(3)]


@pytest.mark.asyncio
@pytest.mark.parametrize("overview_path", [None, "docs/project-brain/free/final-overview.md"])
async def test_assembly_preserves_creation_order_and_module_document_path(database, snapshot, monkeypatch, overview_path):
    bundle = FactExtractor(snapshot).extract()
    fact = bundle.facts[0]
    guard = SimpleNamespace(ledger=None, filter=SimpleNamespace(sanitize_text=lambda value: value),
                            clean=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr("src.tasks.brain_task._guard", lambda *args: guard)
    monkeypatch.setattr("src.tasks.brain_task.read_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(ArtifactStore, "read_work", lambda *args: bundle.model_dump())

    with database() as session:
        session.execute(text("PRAGMA reverse_unordered_selects=ON"))
        build = create_build(session, 1, "main", "fixture/project", snapshot.commit_sha)
        scan = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id, BrainUnit.kind == "scan"))
        scan.status, scan.result = "completed", {"project_url": "https://gitlab.example.test/fixture/project"}
        build.coverage = {}  # Direct legacy checkpoints; v2 uses its full Worker test.
        session.add_all([
            BrainUnit(build_id=build.id, key="extract", kind="extract", status="completed",
                      result={"reused_files": 0, "incremental": False}),
            BrainUnit(build_id=build.id, key="finalize", kind="finalize", status="completed",
                      result={"cache_hits": 0}),
        ])
        units = [compiled_unit(build.id, index, fact,
                               document_path=f"docs/project-brain/free/module-{index}.md") for index in range(3)]
        # Supply every remaining real fixture module, so normal final evidence
        # and navigation validation can run before the intercepted persistence.
        for module in sorted(set(bundle.modules) - {fact.module_id}):
            module_fact = next(value for value in bundle.facts if value.module_id == module)
            units.append(compiled_unit(build.id, len(units), module_fact))
        session.add_all(units)
        session.commit()
        for unit in reversed(units):
            unit.status = "completed"
            session.commit()
        while tasks := next_summary_tasks(bundle, session.scalars(select(BrainUnit).where(
                BrainUnit.build_id == build.id).order_by(BrainUnit.id)).all()):
            for task in reversed(tasks):
                synthesized = completed_summary_unit(task,
                    text="模块最终概览" if task.kind == "module_summary" else "项目最终综合",
                    document_path=overview_path if task.payload["module_id"] == fact.module_id else None)
                session.add(BrainUnit(build_id=build.id, key=task.key, kind=task.kind,
                    payload=task.payload, input_hash=task.input_hash, status="completed", result=synthesized.result))
            session.commit()
        assemble = BrainUnit(build_id=build.id, key="assemble", kind="assemble")
        session.add(assemble)
        session.commit()
        project = session.get(BrainProject, build.project_id)

    captured = []

    class ArtifactCaptured(Exception):
        """Stop after real assembly/validation, before any artifact write."""

    def capture_final_cards(self, manifest, safe_bundle, cards, project_url):
        captured.extend(cards)
        raise ArtifactCaptured()

    monkeypatch.setattr(ArtifactStore, "finalize", capture_final_cards)
    with pytest.raises(ArtifactCaptured):
        await _execute(assemble, build, project, "synthetic-lease")
    module_card = next(card for card in captured if card.kind == "module" and card.module_id == fact.module_id)
    assert [claim.text for claim in module_card.claims] == ["模块最终概览"]
    assert [claim.text for claim in module_card.detail_claims] == [f"编译结论{i}" for i in range(3)]
    assert module_card.document_path == (overview_path or "docs/project-brain/free/module-0.md")
    project_card = next(card for card in captured if card.kind == "project")
    assert [claim.text for claim in project_card.claims if claim.knowledge_type == "AI_INFERRED"] == ["项目最终综合"]
    guard.close.assert_awaited_once()

