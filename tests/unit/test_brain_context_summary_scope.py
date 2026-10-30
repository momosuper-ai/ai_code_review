"""Summary provenance is shared on the wire, never removed from canonical knowledge."""

import copy
import json

import pytest

from src.core.project_brain.context import _hoist_constants, compact_packet, read_rows
from src.core.project_brain.schema import InferenceScope


def summary_packet(*, claim_count: int = 4) -> dict:
    refs = [{"id": f"evidence:{index:024d}", "commit_sha": "a" * 40,
             "blob_sha": "b" * 40, "path": "src/service.py", "line_start": index + 1,
             "line_end": index + 1, "symbol_id": None} for index in range(2)]
    scope = InferenceScope(id="scope:" + "c" * 24, kind="declaration_fragment",
        packet_digest="d" * 64, fact_id="symbol:" + "e" * 24,
        relation_ids=[f"relation:{index:024d}" for index in range(30)],
        source_ref_ids=[ref["id"] for ref in refs], fragment_index=1, fragment_total=3).model_dump()
    claims = [{"text": f"第{index}条完整解释：保留换行\n以及引号\"和证据范围。",
               "evidence_ids": scope["source_ref_ids"], "source_scope": copy.deepcopy(scope)}
              for index in range(claim_count)]
    return {"module_id": "project", "module": "项目概览", "coverage": ["动态访问保持未知"],
            "items": [{"fact": {"module": "service", "title": "服务职责", "claims": claims,
                                "unknowns": ["运行时分派无法静态确定", ""]},
                       "relations": [], "evidence": refs}]}


def wire_claims(context) -> list[dict]:
    return read_rows(context.wire, "facts")[0]["extra"]["claims"]


def test_summary_scope_shared_table_roundtrips_every_canonical_value() -> None:
    packet = summary_packet(claim_count=20)
    before = copy.deepcopy(packet)
    context = compact_packet(packet)
    claims = wire_claims(context)
    assert packet == before
    assert len(context.wire["source_scopes"]) == 1
    assert {claim["source_scope"] for claim in claims} == {"I1"}
    assert [context.restore_summary_claim(claim) for claim in claims] == packet["items"][0]["fact"]["claims"]
    assert context.restore_source_scope("I1") == packet["items"][0]["fact"]["claims"][0]["source_scope"]
    assert read_rows(context.wire, "facts")[0]["extra"]["unknowns"] == ["运行时分派无法静态确定", ""]
    assert len(json.dumps(context.wire, ensure_ascii=False).encode()) < len(json.dumps(packet, ensure_ascii=False).encode()) / 2


def test_origins_and_packet_digests_have_separate_reversible_aliases() -> None:
    packet = summary_packet()
    context = compact_packet(packet)
    row = read_rows(context.wire, "source_scopes")[0]
    original = packet["items"][0]["fact"]["claims"][0]["source_scope"]
    assert row["fact_id"].startswith("O")
    assert all(value.startswith("O") for value in row["relation_ids"])
    assert row["packet_digest"] == "D1"
    assert context.origin_ids[row["fact_id"]] == original["fact_id"]
    assert [context.origin_ids[value] for value in row["relation_ids"]] == original["relation_ids"]
    assert context.packet_digests[row["packet_digest"]] == original["packet_digest"]
    assert context.restore_evidence_ids(row["source_ref_ids"]) == original["source_ref_ids"]
    # Historical origins must not invent current facts/relations on this wire.
    assert len(read_rows(context.wire, "facts")) == 1
    assert read_rows(context.wire, "relations") == []
    assert set(context.origin_ids).isdisjoint(row["id"] for row in read_rows(context.wire, "facts"))


def test_mixed_current_relations_and_historical_origins_retain_complete_states() -> None:
    packet = summary_packet()
    summary = packet["items"][0]
    scope = summary["fact"]["claims"][0]["source_scope"]
    current_fact = {"id": scope["fact_id"], "kind": "symbol", "name": "handler",
                    "module_id": "service", "source_ref_ids": scope["source_ref_ids"],
                    "extractor": "brain.python/1", "attributes": {"path": "src/service.py"}}
    current_relations = [{"id": scope["relation_ids"][index], "from_id": current_fact["id"],
        "to_id": "symbol:target" if state == "resolved" else None, "type": "CALLS",
        "target_name": "target", "resolution": state, "knowledge_type": "FACT",
        "source_ref_ids": scope["source_ref_ids"],
        "candidates": ["symbol:target", "symbol:alternative"] if state == "candidate" else []}
        for index, state in enumerate(("resolved", "candidate", "unresolved"))]
    packet["items"].append({"fact": current_fact, "relations": current_relations,
                            "evidence": copy.deepcopy(summary["evidence"])})
    before = copy.deepcopy(packet)
    context = compact_packet(packet)
    rows = read_rows(context.wire, "relations")
    assert packet == before
    assert len(rows) == 3
    assert len(read_rows(context.wire, "facts")) == 2
    by_state = {row["resolution"]: row for row in rows}
    assert by_state["resolved"]["to_id"]
    assert by_state["candidate"]["to_id"] is None
    assert by_state["unresolved"]["to_id"] is None
    assert len(by_state["candidate"]["candidates"]) == 2
    assert by_state["resolved"]["to_id"] in by_state["candidate"]["candidates"]
    assert by_state["unresolved"]["candidates"] == []
    assert all(context.restore_evidence_ids(row["source_ref_ids"]) == scope["source_ref_ids"] for row in rows)
    assert len(read_rows(context.wire, "source_scopes")[0]["relation_ids"]) == 30
    summary_row = next(row for row in read_rows(context.wire, "facts") if row["extra"].get("claims"))
    assert [context.restore_summary_claim(claim) for claim in summary_row["extra"]["claims"]] == summary["fact"]["claims"]


def test_multiple_scopes_preserve_order_duplicates_null_and_fragment_values() -> None:
    packet = summary_packet()
    claims = packet["items"][0]["fact"]["claims"]
    second = claims[1]["source_scope"]
    second["id"] = "scope:" + "f" * 24
    second["kind"] = "summary_group"
    second["fact_id"] = None
    second["packet_digest"] = "0" * 64
    second["relation_ids"] = []
    second["source_ref_ids"] = list(reversed(second["source_ref_ids"])) * 2
    second["fragment_index"], second["fragment_total"] = 0, 1
    claims[1]["evidence_ids"] = copy.deepcopy(second["source_ref_ids"])
    context = compact_packet(packet)
    assert len(context.wire["source_scopes"]) == 2
    assert [context.restore_summary_claim(claim) for claim in wire_claims(context)] == claims
    assert context.restore_source_scope(wire_claims(context)[1]["source_scope"]) == second


@pytest.mark.parametrize("changed", ["packet_digest", "relation_ids", "fragment_index", "source_ref_ids"])
def test_same_scope_identity_with_conflicting_contents_is_rejected(changed: str) -> None:
    packet = summary_packet()
    scope = packet["items"][0]["fact"]["claims"][1]["source_scope"]
    scope[changed] = {"packet_digest": "9" * 64, "relation_ids": [], "fragment_index": 0,
                      "source_ref_ids": [scope["source_ref_ids"][0]]}[changed]
    with pytest.raises(ValueError, match="来源范围.*冲突"):
        compact_packet(packet)


@pytest.mark.parametrize("changed,value", [
    ("future_private_field", "must-not-be-discarded"),
    ("fragment_index", True), ("fragment_index", "1"), ("packet_digest", 123),
    ("relation_ids", "relation:not-a-list"), ("source_ref_ids", []), ("kind", "future-kind"),
])
def test_scope_shape_is_strict_and_errors_never_echo_untrusted_values(changed, value) -> None:
    packet = summary_packet()
    for claim in packet["items"][0]["fact"]["claims"]:
        claim["source_scope"][changed] = value
    with pytest.raises(ValueError, match="汇总来源范围") as error:
        compact_packet(packet)
    assert "future_private_field" not in str(error.value)
    assert "must-not-be-discarded" not in str(error.value)
    assert "relation:not-a-list" not in str(error.value)
    assert "future-kind" not in str(error.value)


def test_missing_scope_field_and_missing_scope_evidence_are_rejected() -> None:
    packet = summary_packet()
    del packet["items"][0]["fact"]["claims"][0]["source_scope"]["fact_id"]
    with pytest.raises(ValueError, match="来源范围字段"):
        compact_packet(packet)
    packet = summary_packet()
    for claim in packet["items"][0]["fact"]["claims"]:
        claim["source_scope"]["source_ref_ids"] = ["evidence:missing"]
    with pytest.raises(ValueError, match="缺失证据"):
        compact_packet(packet)


def test_unknown_claim_fields_and_nested_scope_shaped_data_remain_complete() -> None:
    packet = summary_packet()
    claim = packet["items"][0]["fact"]["claims"][0]
    scope = copy.deepcopy(claim["source_scope"])
    claim["future_semantics"] = {"source_scope": scope, "text": "O1 D1 I1 原样保留"}
    packet["future_wrapper"] = {"claims": [copy.deepcopy(claim)], "source_scope": scope,
                                "future_private_field": "不能被当成来源元数据省略"}
    context = compact_packet(packet)
    assert isinstance(wire_claims(context)[0]["future_semantics"]["source_scope"], dict)
    assert isinstance(context.wire["extra"]["future_wrapper"]["source_scope"], dict)
    assert isinstance(context.wire["extra"]["future_wrapper"]["claims"][0]["source_scope"], dict)
    assert context.wire["extra"]["future_wrapper"]["future_private_field"] == "不能被当成来源元数据省略"
    assert context.restore_summary_claim(wire_claims(context)[0]) == claim


def test_null_scope_and_legacy_claims_keep_existing_wire_contract() -> None:
    packet = summary_packet()
    for claim in packet["items"][0]["fact"]["claims"]:
        claim["source_scope"] = None
    context = compact_packet(packet)
    assert "source_scopes" not in context.wire
    assert "source_scopes" not in context.wire["columns"]
    assert context.scope_ids == context.origin_ids == context.packet_digests == {}
    assert [context.restore_summary_claim(claim) for claim in wire_claims(context)] == packet["items"][0]["fact"]["claims"]


@pytest.mark.parametrize("view", ["symbol_graph", "resolution_counts"])
def test_synthesis_relationship_view_roundtrips_without_turning_counts_into_edges(view):
    packet = summary_packet()
    for claim in packet["items"][0]["fact"]["claims"]:
        claim["source_scope"].update(kind="summary_synthesis", fact_id=None, relation_ids=[],
                                    child_ids=["summary-input:" + "1" * 24], module_ids=["module:" + "2" * 24],
                                    fragment_index=0, fragment_total=1, relationship_view=view)
    context = compact_packet(packet)
    assert [context.restore_summary_claim(claim) for claim in wire_claims(context)] == packet["items"][0]["fact"]["claims"]
    assert context.restore_source_scope("I1")["relationship_view"] == view
    assert read_rows(context.wire, "relations") == []


def test_older_scope_defaults_relationship_view_to_unknown_only():
    packet = summary_packet()
    for claim in packet["items"][0]["fact"]["claims"]:
        claim["source_scope"].pop("relationship_view")
    context = compact_packet(packet)
    assert context.restore_source_scope("I1")["relationship_view"] is None


@pytest.mark.parametrize("value", ["private_untrusted_value", True, "resolution_counts"])
def test_declaration_cannot_forge_a_synthesis_relationship_view(value):
    packet = summary_packet()
    for claim in packet["items"][0]["fact"]["claims"]:
        claim["source_scope"]["relationship_view"] = value
    with pytest.raises(ValueError, match="汇总来源范围"):
        compact_packet(packet)


def test_scope_shaped_declaration_attributes_are_never_compressed_as_provenance() -> None:
    packet = summary_packet()
    fact = packet["items"][0]["fact"]
    fact["id"] = "symbol:current-declaration"
    context = compact_packet(packet)
    assert "source_scopes" not in context.wire
    assert all(isinstance(claim["source_scope"], dict) for claim in wire_claims(context))


def test_constant_hoisting_retains_existing_defaults_and_every_scope_value() -> None:
    packet = summary_packet()
    claim = packet["items"][0]["fact"]["claims"][1]
    claim["source_scope"]["id"] = "scope:" + "f" * 24
    context = compact_packet(packet)
    assert "source_scopes" in context.wire["defaults"]
    before = {name: read_rows(context.wire, name) for name in context.wire["columns"]}
    context.wire["defaults"]["future_table"] = {"complete": None}
    _hoist_constants(context.wire)
    assert context.wire["defaults"]["future_table"] == {"complete": None}
    assert {name: read_rows(context.wire, name) for name in context.wire["columns"]} == before


@pytest.mark.parametrize("map_name", ["scope_ids", "origin_ids", "packet_digests", "evidence_ids"])
def test_inverse_maps_cannot_resolve_unknown_source_identity(map_name: str) -> None:
    context = compact_packet(summary_packet())
    getattr(context, map_name).clear()
    with pytest.raises(ValueError):
        context.restore_source_scope("I1")

