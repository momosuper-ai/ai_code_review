"""Explicit upper-layer statistics retain the raw graph in canonical input."""

from collections import Counter
import copy
import json

import pytest

from src.core.project_brain.synthesis_wire import (
    canonical_relationship_digest, resolution_counts_wire, synthesis_relationship_view,
    validate_resolution_counts_wire,
)
from tests.unit.test_brain_synthesis_wire import expanded_graph


def count_case(count=100):
    expanded = expanded_graph(count)
    common = {key: value for key, value in expanded.items() if key not in {"protocol", "relationships", "entities"}}
    canonical = [{"id": f"relation:{index}", "from_id": row["from"], "to_id": row["to"],
                  "type": row["type"], "target_name": row["target_name"], "resolution": row["resolution"],
                  "knowledge_type": row["knowledge_type"], "source_ref_ids": [f"evidence:{index}"],
                  "candidates": row["candidates"]} for index, row in enumerate(expanded["relationships"])]
    return common, canonical


def rows(table):
    return [{**table["defaults"], **dict(zip(table["columns"], row, strict=True))} for row in table["rows"]]


@pytest.mark.parametrize("count", [0, 1, 100, 8405])
def test_counts_are_exact_but_explicitly_have_no_edges_or_ownership(count):
    common, canonical = count_case(count)
    before = copy.deepcopy((common, canonical))
    wire = resolution_counts_wire(common, canonical)
    assert wire["protocol"] == "project-brain-synthesis/3"
    assert wire["relationships"]["view"] == "resolution_counts"
    assert wire["relationships"]["detail_retained"] is True
    assert wire["relationships"]["total"] == len(canonical)
    expected = Counter((r["type"], r["resolution"], r["knowledge_type"]) for r in canonical)
    actual = {(r["type"], r["resolution"], r["knowledge_type"]): r["count"] for r in rows(wire["relationships"]["counts"])}
    assert actual == expected
    assert wire["relationships"]["canonical_digest"] == canonical_relationship_digest(canonical)
    assert wire["children"] == common["children"]
    assert (common, canonical) == before
    assert "entities" not in wire and "entity_identity" not in wire
    assert not any(key in json.dumps(wire) for key in ("target_name", "from_id", "to_id", "child_candidates"))
    assert validate_resolution_counts_wire(wire) == wire
    if count == 8405:
        assert len(json.dumps(wire, ensure_ascii=False).encode()) < 8000


def test_canonical_duplicates_do_not_double_count_and_conflicts_are_rejected():
    common, canonical = count_case()
    once = resolution_counts_wire(common, canonical)
    canonical.append(copy.deepcopy(canonical[0]))
    assert resolution_counts_wire(common, canonical) == once
    canonical[-1]["target_name"] = "private_untrusted_value"
    with pytest.raises(ValueError, match="综合知识传输表契约无效"):
        resolution_counts_wire(common, canonical)


@pytest.mark.parametrize("field,value", [("target_name", "other-target"), ("source_ref_ids", ["evidence:new"]),
    ("candidates", ["F1", "F2", "F3"]), ("id", "relation:another")])
def test_relationship_digest_binds_complete_rows_not_just_counts(field, value):
    common, canonical = count_case()
    before = canonical_relationship_digest(canonical)
    canonical[0][field] = value
    assert canonical_relationship_digest(canonical) != before
    assert resolution_counts_wire(common, canonical)["relationships"]["counts"] == resolution_counts_wire(common, count_case()[1])["relationships"]["counts"]


@pytest.mark.parametrize("purpose,level,expected", [("module", 0, "symbol_graph"), ("module", 1, "resolution_counts"),
    ("project", 0, "resolution_counts"), ("project", 7, "resolution_counts")])
def test_view_selector_follows_semantic_layer_not_project_depth(purpose, level, expected):
    assert synthesis_relationship_view(purpose, level) == expected


@pytest.mark.parametrize("change", ["extra_field", "view", "bool_total", "wrong_total", "missing_count_column",
    "unknown_count_column", "duplicate_column", "duplicate_count", "bool_count", "zero_count", "state",
    "knowledge", "digest", "detail", "endpoint", "unknown_child_field", "initial_module_layer"])
def test_malformed_counts_do_not_masquerade_as_full_graph(change):
    common, canonical = count_case()
    wire = resolution_counts_wire(common, canonical)
    view = wire["relationships"]
    table = view["counts"]
    columns = table["columns"]
    first = table["rows"][0]
    if change == "extra_field": view["private_untrusted_key"] = "private_untrusted_value"
    elif change == "view": view["view"] = "symbol_graph"
    elif change == "bool_total": view["total"] = True
    elif change == "wrong_total": view["total"] += 1
    elif change == "missing_count_column": columns.pop()
    elif change == "unknown_count_column": columns[0] = "private_untrusted_key"
    elif change == "duplicate_column": columns.append(columns[0])
    elif change == "duplicate_count": table["rows"].append(copy.deepcopy(first))
    elif change == "bool_count": first[columns.index("count")] = True
    elif change == "zero_count": first[columns.index("count")] = 0
    elif change == "state": first[columns.index("resolution")] = ["private_untrusted_value"]
    elif change == "knowledge": first[columns.index("knowledge_type")] = "AI_INFERRED"
    elif change == "digest": view["canonical_digest"] = "private_untrusted_value"
    elif change == "detail": view["detail_retained"] = False
    elif change == "endpoint": wire["entities"] = {"private_untrusted_key": {}}
    elif change == "unknown_child_field": wire["children"][0]["private_untrusted_key"] = "private_untrusted_value"
    else: wire.update(purpose="module", level=0)
    with pytest.raises(ValueError) as caught:
        validate_resolution_counts_wire(wire)
    assert str(caught.value) == "综合知识传输表契约无效"
    assert "private_untrusted" not in str(caught.value)
