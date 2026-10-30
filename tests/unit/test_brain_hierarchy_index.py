"""Per-evaluation hierarchy indexes preserve canonical provenance and limits."""

from types import SimpleNamespace

import pytest

from src.config import settings
from src.core.project_brain import summary
from src.core.project_brain.schema import Compilation, Fact, FactBundle, Relation, SourceRef


class _CountedRefs(list):
    iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def _sparse_graph(leaf_count=32, ref_count=600, relation_count=3000):
    refs = [SourceRef(id=f"evidence:{i}", commit_sha="a" * 40, path=f"m/f{i}.py",
                      blob_sha="b" * 40, line_start=1, line_end=2, symbol_id=f"fact:{i}")
            for i in range(ref_count)]
    facts = [Fact(id=f"fact:{i}", kind="function", name=f"f{i}", module_id="m",
                  source_ref_ids=[ref.id], extractor="synthetic") for i, ref in enumerate(refs)]
    units = [SimpleNamespace(key=f"compile:{i}", kind="compile", status="completed",
                             payload={"module_id": "m"}, input_hash=f"input:{i}",
                             result=Compilation(title=f"Leaf{i}", claims=[{
                                 "text": f"Responsibility{i}", "evidence_ids": [refs[i].id]}],
                                 unknowns=[]).model_dump()) for i in range(leaf_count)]
    relations = [Relation(id=f"relation:{i}", from_id=f"fact:{i % ref_count}",
                          to_id=None, type="calls", target_name="unresolved_target",
                          resolution="unresolved", source_ref_ids=[
                              refs[leaf_count + i % (ref_count - leaf_count)].id])
                 for i in range(relation_count)]
    relations.extend(Relation(id=f"selected:{i}", from_id=f"fact:{i}", to_id=None,
                              type="calls", target_name="selected_target", resolution="candidate",
                              candidates=[f"fact:{i}"], source_ref_ids=[refs[i].id]) for i in range(leaf_count))
    for relation in relations:
        relation.source_ref_ids = _CountedRefs(relation.source_ref_ids)
    return FactBundle(source_commit="a" * 40, files={}, modules={"m": "Module"},
                      facts=facts, source_refs=refs, relations=relations,
                      source_fingerprint="f" * 64, issues=[]), units


def _slow_item(bundle, child, selection, *args, **kwargs):
    """The original full-list predicate, kept independent of the new index."""
    claims = child.compilation.claims[selection["claim_start"]:selection["claim_end"]]
    unknowns = child.compilation.unknowns[selection["unknown_start"]:selection["unknown_end"]]
    if not claims and not unknowns:
        summary._fail()
    refs = {ref.id: ref for ref in bundle.source_refs}
    evidence = {rid for claim in claims for rid in claim.evidence_ids}
    evidence.update(rid for claim in claims if claim.source_scope for rid in claim.source_scope.source_ref_ids)
    if not claims:
        evidence.update(rid for claim in child.compilation.claims for rid in claim.evidence_ids)
        evidence.update(rid for claim in child.compilation.claims if claim.source_scope
                        for rid in claim.source_scope.source_ref_ids)
    if not evidence or not evidence <= set(refs):
        summary._fail()
    scoped = {rid for claim in claims if claim.source_scope for rid in claim.source_scope.relation_ids}
    relations = [relation for relation in bundle.relations
                 if relation.id in scoped or set(relation.source_ref_ids) <= evidence]
    if any(not set(relation.source_ref_ids) <= set(refs) for relation in relations):
        summary._fail()
    evidence.update(rid for relation in relations for rid in relation.source_ref_ids)
    child_id = summary.stable_id("summary-input", child.key, *(str(selection[key]) for key in (
        "claim_start", "claim_end", "unknown_start", "unknown_end")))
    return {"fact": {"module": child.module_name, "title": child.compilation.title,
                     "claims": [claim.model_dump() for claim in claims], "unknowns": list(unknowns),
                     "child_id": child_id},
            "relations": [relation.model_dump() for relation in sorted(relations, key=lambda value: value.id)],
            "evidence": [refs[rid].model_dump() for rid in sorted(evidence)]}


def test_sparse_graph_relationship_sets_are_built_once_per_evaluation(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = _sparse_graph()
    tasks = summary.next_summary_tasks(bundle, units)
    assert tasks and sum(len(task.payload["items"]) for task in tasks) == len(units)
    conversions = sum(relation.source_ref_ids.iterations for relation in bundle.relations)
    # Counting work rather than wall time makes this portable. The previous
    # repeated full-list predicate iterated every unrelated relation per trial.
    assert conversions <= len(bundle.relations) * 2 + len(units) * 20


def test_sparse_graph_task_hashes_and_packets_match_full_list_predicate(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = _sparse_graph(12, 120, 250)
    optimized = summary.next_summary_tasks(bundle, units)
    monkeypatch.setattr(summary, "_item", _slow_item)
    assert summary.next_summary_tasks(bundle, units) == optimized


def test_identical_complete_packets_do_not_reencode_wire_within_evaluation(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = _sparse_graph(3, 12, 9)
    seen = []
    original = summary.packet_wire

    def counted(packet):
        seen.append(summary.encoded(packet))
        return original(packet)

    monkeypatch.setattr(summary, "packet_wire", counted)
    summary.next_summary_tasks(bundle, units)
    assert len(seen) == len(set(seen))


def test_index_is_not_reused_after_source_or_child_mutation(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = _sparse_graph(1, 4, 2)
    first = summary.next_summary_tasks(bundle, units)
    units[0].result["claims"][0]["text"] = "Changed complete child content"
    assert summary.next_summary_tasks(bundle, units) != first
    previous = summary.next_summary_tasks(bundle, units)
    bundle.source_refs[0].blob_sha = "c" * 40
    assert summary.next_summary_tasks(bundle, units) != previous


def test_fit_cache_authenticates_entire_packet_before_reusing_wire_result(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = _sparse_graph(1, 4, 0)
    evaluation = summary._EvaluationIndex(bundle)
    child = summary._compile_children(bundle, summary._unit_map(units), evaluation)["m"][0]
    packet = summary._packet(bundle, "module", "m", 0, 0, [
        (child, summary._selection(child, 0, 1, 0, 0))], evaluation)
    assert summary._fits(packet, evaluation)
    packet["synthesis"]["unclassified"] = "Cannot reuse a smaller known envelope"
    with pytest.raises(ValueError):
        summary._fits(packet, evaluation)


def test_empty_relation_sources_and_duplicate_ids_preserve_list_semantics():
    bundle, units = _sparse_graph(1, 4, 0)
    bundle.relations = [Relation(id="same", from_id="fact:0", to_id=None, type="calls",
                                target_name=name, resolution="candidate", source_ref_ids=sources)
                        for name, sources in [("empty", []), ("selected", ["evidence:0"]),
                                              ("outside", ["evidence:1"])]]
    child = summary._compile_children(bundle, summary._unit_map(units))["m"][0]
    selection = summary._selection(child, 0, 1, 0, 0)
    indexed = summary._item(bundle, child, selection, summary._EvaluationIndex(bundle))
    assert indexed == summary._item(bundle, child, selection) == _slow_item(bundle, child, selection)
    assert [relation["target_name"] for relation in summary._item(bundle, child, selection)["relations"]] == [
        "empty", "selected"]


def test_conflicting_duplicate_relationships_remain_visible_to_contract_gate(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = _sparse_graph(1, 4, 0)
    relation = bundle.relations[0].model_copy(deep=True)
    relation.target_name = "Conflicting duplicate content"
    bundle.relations.append(relation)
    with pytest.raises(ValueError, match="冲突"):
        summary.next_summary_tasks(bundle, units)
    monkeypatch.setattr(summary, "_item", _slow_item)
    with pytest.raises(ValueError, match="冲突"):
        summary.next_summary_tasks(bundle, units)


@pytest.mark.parametrize("indexed", [False, True])
def test_scoped_relation_keeps_or_predicate_and_rejects_missing_source(indexed):
    bundle, units = _sparse_graph(1, 4, 0)
    result = units[0].result
    result["claims"][0]["source_scope"] = {
        "id": "scope:0", "kind": "declaration_fragment", "packet_digest": "f" * 64,
        "fact_id": "fact:0", "relation_ids": ["selected:0"], "source_ref_ids": ["evidence:0"],
        "fragment_index": 0, "fragment_total": 1}
    # Scope selects the canonical relation by ID even when its source pool is
    # outside this claim. The relation then contributes its complete evidence.
    bundle.relations[0].source_ref_ids = ["evidence:1"]
    child = summary._compile_children(bundle, summary._unit_map(units))["m"][0]
    selection = summary._selection(child, 0, 1, 0, 0)
    index = summary._EvaluationIndex(bundle) if indexed else None
    item = summary._item(bundle, child, selection, index)
    assert item == _slow_item(bundle, child, selection)
    assert {ref["id"] for ref in item["evidence"]} == {"evidence:0", "evidence:1"}
    bundle.relations[0].source_ref_ids = ["missing:evidence"]
    index = summary._EvaluationIndex(bundle) if indexed else None
    with pytest.raises(ValueError, match="契约"):
        summary._item(bundle, child, selection, index)
