"""Complete topology round trips: compact transport never removes graph facts."""

import copy
import json

import pytest

from src.core.project_brain.synthesis_wire import (
    ENTITY_IDENTITY, compact_synthesis_wire, expand_synthesis_wire,
)


def expanded_graph(count=90):
    children = [{"id": f"C{index}", "checkpoint_digest": str(index) * 64,
                 "module": f"模块{index}", "title": f"标题{index}",
                 "claims": [{"text": f"说明{index}包含引号\"、换行\n和反斜线\\。", "knowledge_type": "AI_INFERRED"}],
                 "unknowns": [f"动态行为{index}未知", ""],
                 "selection": {"claim_start": 2, "claim_end": 3, "unknown_start": 4, "unknown_end": 6}}
                for index in range(1, 4)]
    entities = {f"F{index}": {"child_candidates": [] if index % 4 == 0 else ["C1", "C3"],
                              "identity": ENTITY_IDENTITY} for index in range(1, count + 2)}
    relations = []
    for index in range(1, count + 1):
        resolution = ("resolved", "candidate", "unresolved")[index % 3]
        relations.append({"from": f"F{index}", "to": f"F{index + 1}" if resolution == "resolved" else None,
                          "type": "calls", "resolution": resolution, "target_name": f"对象{index}.方法()",
                          "knowledge_type": "DERIVED" if index % 5 == 0 else "FACT",
                          "candidates": [f"F{index}", f"F{index + 1}"] if resolution == "candidate" else []})
    # The last endpoint may only be referenced by the final candidate/resolved.
    if count and not any(entities_alias in [r["from"], r["to"], *r["candidates"]]
                         for r in relations for entities_alias in [f"F{count + 1}"]):
        entities.pop(f"F{count + 1}")
    if not count:
        entities = {}
    return {"protocol": "project-brain-synthesis/1", "purpose": "module", "level": 2,
            "module": "完整模块", "aggregate_slot": "S1", "children": children,
            "relationships": relations, "entities": entities, "coverage": ["静态事实不能证明运行时行为"],
            "inherited_limitations": {"count": 12, "detail_retained": True},
            "semantic_support": "Only provenance; AI_INFERRED and unreviewed."}


@pytest.mark.parametrize("count", [0, 1, 90, 650])
def test_complete_graph_text_and_candidate_state_roundtrip(count):
    expanded = expanded_graph(count)
    before = copy.deepcopy(expanded)
    compact = compact_synthesis_wire(expanded)
    assert compact["protocol"] == "project-brain-synthesis/2"
    assert set(compact["relationships"]) == {"columns", "defaults", "rows"}
    assert set(compact["entities"]) == {"columns", "defaults", "rows"}
    assert len(compact["relationships"]["rows"]) == count
    assert expand_synthesis_wire(compact) == expanded
    assert expanded == before
    assert compact["children"] == expanded["children"]
    assert compact["coverage"] == expanded["coverage"]


def test_duplicate_relationship_rows_are_preserved_as_distinct_input_records():
    expanded = expanded_graph(1)
    expanded["relationships"].append(copy.deepcopy(expanded["relationships"][0]))
    compact = compact_synthesis_wire(expanded)
    assert len(compact["relationships"]["rows"]) == 2
    assert expand_synthesis_wire(compact) == expanded


def test_no_shared_mutable_defaults_or_source_aliases():
    expanded = expanded_graph(1)
    compact = compact_synthesis_wire(expanded)
    restored = expand_synthesis_wire(compact)
    restored["children"][0]["claims"][0]["text"] = "changed"
    restored["entities"]["F1"]["child_candidates"].append("C2")
    assert restored["entities"]["F2"]["child_candidates"] == ["C1", "C3"]
    assert restored != expanded
    assert expand_synthesis_wire(compact) == expanded
    compact["children"][0]["unknowns"].append("new")
    assert expanded["children"][0]["unknowns"] == ["动态行为1未知", ""]


def test_dense_graph_removes_repeated_keys_without_changing_any_relationship():
    expanded = expanded_graph(650)
    compact = compact_synthesis_wire(expanded)
    size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    assert size(compact) < size(expanded) * 0.45
    assert expand_synthesis_wire(compact)["relationships"] == expanded["relationships"]
    assert json.dumps(compact).count(ENTITY_IDENTITY) == 1


@pytest.mark.parametrize("change", ["extra", "missing_column", "duplicate_column", "overlap", "unknown_column",
    "short_row", "bad_row", "unknown_endpoint", "invalid_resolution", "candidate_without_target",
    "resolved_without_target", "unresolved_with_target", "bad_candidates", "bad_knowledge", "extra_entity",
    "conflicting_entity", "unknown_child", "duplicate_child", "bool_selection", "wrong_selection",
    "bad_hash", "unknown_child_field", "alter_identity", "bad_default_type", "bad_protocol"])
def test_compact_rejects_unclassified_fields_types_and_topology(change):
    compact = compact_synthesis_wire(expanded_graph())
    relation = compact["relationships"]
    entity = compact["entities"]
    rcolumns = relation["columns"]
    row = relation["rows"][0]
    if change == "extra": compact["private_untrusted_key"] = "private_untrusted_value"
    elif change == "missing_column": rcolumns.pop()
    elif change == "duplicate_column": rcolumns.append(rcolumns[0])
    elif change == "overlap": relation["defaults"][rcolumns[0]] = "private_untrusted_value"
    elif change == "unknown_column": rcolumns[0] = "private_untrusted_key"
    elif change == "short_row": row.pop()
    elif change == "bad_row": relation["rows"][0] = {}
    elif change == "unknown_endpoint": row[rcolumns.index("from")] = "F99999"
    elif change == "invalid_resolution": row[rcolumns.index("resolution")] = ["private_untrusted_value"]
    elif change == "candidate_without_target":
        row[rcolumns.index("resolution")] = "candidate"
        row[rcolumns.index("candidates")] = []
    elif change == "resolved_without_target":
        row[rcolumns.index("resolution")] = "resolved"
        row[rcolumns.index("to")] = None
    elif change == "unresolved_with_target":
        row[rcolumns.index("resolution")] = "unresolved"
        row[rcolumns.index("to")] = "F1"
    elif change == "bad_candidates": row[rcolumns.index("candidates")] = [False]
    elif change == "bad_knowledge": row[rcolumns.index("knowledge_type")] = "AI_INFERRED"
    elif change == "extra_entity": entity["rows"].append(["F99999", []])
    elif change == "conflicting_entity": entity["rows"].append(["F1", ["C2"]])
    elif change == "unknown_child": entity["rows"][0][entity["columns"].index("child_candidates")] = ["C99999"]
    elif change == "duplicate_child": compact["children"].append(copy.deepcopy(compact["children"][0]))
    elif change == "bool_selection": compact["children"][0]["selection"]["claim_start"] = True
    elif change == "wrong_selection": compact["children"][0]["selection"]["claim_end"] += 1
    elif change == "bad_hash": compact["children"][0]["checkpoint_digest"] = "private_untrusted_value"
    elif change == "unknown_child_field": compact["children"][0]["private_untrusted_key"] = "private_untrusted_value"
    elif change == "alter_identity": compact["entity_identity"] = "private_untrusted_value"
    elif change == "bad_default_type": relation["defaults"]["type"] = False
    else: compact["protocol"] = "project-brain-synthesis/unknown"
    with pytest.raises(ValueError) as caught:
        expand_synthesis_wire(compact)
    assert str(caught.value) == "综合知识传输表契约无效"
    assert "private_untrusted" not in str(caught.value)


@pytest.mark.parametrize("change", ["extra_relationship_field", "wrong_purpose", "bool_level", "invalid_entity",
                                  "unknown_claim_field", "claim_as_fact", "unknown_entity_field"])
def test_encoder_rejects_invalid_expanded_graph_without_deleting_data(change):
    wire = expanded_graph()
    if change == "extra_relationship_field": wire["relationships"][0]["private_untrusted_key"] = "private_untrusted_value"
    elif change == "wrong_purpose": wire["purpose"] = ["private_untrusted_value"]
    elif change == "bool_level": wire["level"] = True
    elif change == "invalid_entity": wire["entities"]["F1"]["child_candidates"] = ["C99999"]
    elif change == "unknown_claim_field": wire["children"][0]["claims"][0]["private_untrusted_key"] = "private_untrusted_value"
    elif change == "claim_as_fact": wire["children"][0]["claims"][0]["knowledge_type"] = "FACT"
    else: wire["entities"]["F1"]["private_untrusted_key"] = "private_untrusted_value"
    before = copy.deepcopy(wire)
    with pytest.raises(ValueError) as caught:
        compact_synthesis_wire(wire)
    assert str(caught.value) == "综合知识传输表契约无效"
    assert wire == before
