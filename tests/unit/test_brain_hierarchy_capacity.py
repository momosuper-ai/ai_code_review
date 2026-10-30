"""Synthetic hierarchy capacity regressions; no model, DB or project input.

A short overview can inherit many source references. Canonical relationships
that require multiple references become visible only after this union. The
planner must account for that growth independently of the prose length.
"""

from itertools import combinations
from types import SimpleNamespace

import pytest

from src.config import settings
from src.core.project_brain.compiler import encoded, packet_wire, token_bound
from src.core.project_brain.context import compact_packet
from src.core.project_brain.schema import Compilation, Fact, FactBundle, Relation, SourceRef
from src.core.project_brain.source_bound import SourceBoundPlan
from src.core.project_brain.summary import next_summary_tasks, wrap_summary_result
from src.core.project_brain.summary import hierarchy_for_assembly
from src.core.project_brain.synthesis_wire import canonical_relationship_digest
from tests.unit.test_brain_hierarchy_planner import fixture, ready


def _dense_fixture(resolution):
    refs, facts, units = [], [], []
    for number in range(25):
        ref = SourceRef(id=f"evidence:{number}", commit_sha="a" * 40,
                        path=f"synthetic/f{number}.py", blob_sha="b" * 40,
                        line_start=1, line_end=2, symbol_id=f"fact:{number}")
        facts.append(Fact(id=f"fact:{number}", kind="function", name=f"f{number}",
                          module_id="synthetic", source_ref_ids=[ref.id], extractor="fixture"))
        refs.append(ref)
        result = Compilation(title="Leaf", claims=[{"text": "Short responsibility.",
                             "evidence_ids": [ref.id]}], unknowns=[])
        units.append(SimpleNamespace(key=f"compile:{number}", kind="compile", status="completed",
                                     payload={"module_id": "synthetic"}, input_hash=f"input:{number}",
                                     result=result.model_dump()))
    relations = []
    if resolution:
        for left, right in combinations(range(24), 2):
            relations.append(Relation(id=f"relation:{left}:{right}", from_id=f"fact:{left}",
                to_id=f"fact:{right}" if resolution == "resolved" else None,
                candidates=[f"fact:{right}"] if resolution == "candidate" else [],
                type="calls", target_name=f"synthetic_target_{right}", resolution=resolution,
                source_ref_ids=[f"evidence:{left}", f"evidence:{right}"]))
    bundle = FactBundle(source_commit="a" * 40, files={}, modules={"synthetic": "Synthetic module"},
                        facts=facts, source_refs=refs, relations=relations,
                        source_fingerprint="f" * 64, issues=["Runtime not executed."])
    return bundle, units


def _complete_short_overview(task):
    plan = SourceBoundPlan.from_packet(task.payload, compact_packet(task.payload))
    compilation = plan.bind(encoded({"title": "Short overview", "slots": {
        "S1": {"texts": ["A short module overview."], "unknowns": []}},
        "unknowns": [], "document_path": None}))
    return SimpleNamespace(key=task.key, kind=task.kind, status="completed", payload=task.payload,
                           input_hash=task.input_hash, result=wrap_summary_result(task.payload, compilation))


def _complete_first_level(bundle, units):
    tasks = next_summary_tasks(bundle, units)
    assert [len(task.payload["items"]) for task in tasks] == [24, 1]
    assert all(token_bound(encoded(packet_wire(task.payload))) <= 32000 for task in tasks)
    # Pair-backed relations are absent while every child has one reference.
    assert all(not item["relations"] for task in tasks for item in task.payload["items"])
    units.extend(_complete_short_overview(task) for task in tasks)
    assert len(units[-2].result["compilation"]["claims"][0]["text"]) == 24
    assert len(units[-2].result["compilation"]["claims"][0]["evidence_ids"]) == 24


def test_short_overview_with_wide_source_pool_is_not_itself_oversized(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = _dense_fixture(None)
    _complete_first_level(bundle, units)
    next_tasks = next_summary_tasks(bundle, units)
    assert len(next_tasks) == 1
    assert next_tasks[0].payload["synthesis"]["level"] == 1


@pytest.mark.parametrize("resolution", ["resolved", "candidate"])
def test_short_overview_does_not_become_unsplittable_when_source_union_exposes_relations(monkeypatch, resolution):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = _dense_fixture(resolution)
    assert len(bundle.relations) == 276
    _complete_first_level(bundle, units)

    # Regression expectation: relation growth needs its own capacity treatment,
    # without deleting canonical relationships, candidate state or overview.
    # Current code raises "single claim exceeds cap" although prose is 24 bytes.
    next_tasks = next_summary_tasks(bundle, units)
    assert next_tasks
    assert all(token_bound(encoded(packet_wire(task.payload))) <= 32000 for task in next_tasks)
    retained = {relation["id"]: relation["resolution"] for task in next_tasks
                for item in task.payload["items"] for relation in item["relations"]}
    assert retained == {relation.id: resolution for relation in bundle.relations}


def test_complete_tree_converges_with_8646_relationships_and_short_upper_overviews(monkeypatch):
    """Counts are a declared upper view; the complete canonical graph survives."""
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture((132,))
    bundle.relations = []
    for number, (left, right) in enumerate(combinations(range(132), 2)):
        resolution = ("resolved", "candidate", "unresolved")[number % 3]
        bundle.relations.append(Relation(
            id=f"relation:{left}:{right}", from_id=f"fact:{left}",
            to_id=f"fact:{right}" if resolution == "resolved" else None,
            candidates=[f"fact:{right}"] if resolution == "candidate" else [],
            type="calls", target_name=f"f{right}", resolution=resolution,
            source_ref_ids=[f"evidence:{left}", f"evidence:{right}"]))
    assert len(bundle.relations) == 8646

    completed_tasks = []
    for _ in range(5):
        tasks = next_summary_tasks(bundle, units)
        if not tasks:
            break
        for task in tasks:
            assert token_bound(encoded(packet_wire(task.payload))) <= 32000
        completed_tasks.extend(tasks)
        units.extend(ready(task, text="Short complete child-set overview.") for task in tasks)
    else:
        pytest.fail("wide graph did not reach its complete project root")

    hierarchy = hierarchy_for_assembly(bundle, units)
    assert hierarchy["coverage"]["module_ids"] == ["m0"]
    assert hierarchy["coverage"]["leaf_ids"] == [f"compile:{i}" for i in range(132)]
    assert len(hierarchy["project"].claims) == 1
    assert hierarchy["project"].claims[0].source_scope.relationship_view == "resolution_counts"
    assert hierarchy["project"].claims[0].evidence_ids == sorted(ref.id for ref in bundle.source_refs)
    assert {f"未知{i}" for i in range(132)} <= set(hierarchy["all_unknowns"]["project"])

    project_tasks = [task for task in completed_tasks if task.kind == "project_summary"]
    assert len(project_tasks) == 1
    project = project_tasks[0]
    canonical_relations = {relation["id"]: relation for item in project.payload["items"]
                           for relation in item["relations"]}
    assert canonical_relations == {relation.id: relation.model_dump() for relation in bundle.relations}
    wire = packet_wire(project.payload)
    assert wire["protocol"] == "project-brain-synthesis/3"
    assert wire["relationships"]["view"] == "resolution_counts"
    assert wire["relationships"]["total"] == 8646
    assert wire["relationships"]["detail_retained"] is True
    assert wire["relationships"]["canonical_digest"] == canonical_relationship_digest(list(canonical_relations.values()))
    assert "entities" not in wire
