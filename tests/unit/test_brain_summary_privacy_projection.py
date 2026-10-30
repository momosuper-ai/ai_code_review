"""Summary classification excludes authenticated positions, never lookalikes."""

import copy

import pytest

from src.core.project_brain.privacy import scan_records
from src.core.project_brain.privacy_projection import summary_privacy_records
from src.core.project_brain.schema import InferredClaim, InferenceScope, Relation, SourceRef, stable_id


@pytest.fixture
def summary_packet():
    module = "module:" + "1" * 64
    fact = "fact:" + "2" * 64
    evidence = "evidence:" + "3" * 64
    relation = "relation:" + "4" * 64
    scope = InferenceScope(id="scope:" + "5" * 24, kind="declaration_fragment",
        packet_digest="6" * 64, fact_id=fact, relation_ids=[relation], source_ref_ids=[evidence],
        fragment_index=0, fragment_total=1)
    claim = InferredClaim(text="Complete child duties\nincluding quoted \"configuration\".",
        evidence_ids=[evidence], source_scope=scope).model_dump()
    selection = {"source_key": "compile:0", "claim_start": 0, "claim_end": 1, "unknown_start": 0, "unknown_end": 1}
    child = stable_id("summary-input", "compile:0", "0", "1", "0", "1")
    ref = SourceRef(id=evidence, commit_sha="a" * 40, path="sources/s" + "b" * 64,
        display_path="app/component.py", blob_sha="c" * 40, line_start=1, line_end=3, symbol_id=fact).model_dump()
    rel = Relation(id=relation, from_id=fact, to_id="fact:" + "7" * 64,
        type="CALLS", target_name="complete_call_target", resolution="resolved", source_ref_ids=[evidence]).model_dump()
    return {"module_id": module, "module": "Complete module display", "coverage": ["Complete coverage limitation"],
        "items": [{"fact": {"module": "Complete child module", "title": "Complete child title", "claims": [claim],
                             "unknowns": ["Complete child unknown"], "child_id": child},
                   "relations": [rel], "evidence": [ref]}],
        "synthesis": {"purpose": "module", "level": 0, "index": 0, "child_ids": [child], "module_ids": [module],
                      "leaf_ids": ["compile:0"], "child_hashes": {child: "d" * 64},
                      "all_unknowns": ["Complete inherited detail absent from semantic wire"],
                      "selections": {child: selection}}}


def test_all_complete_narratives_and_display_paths_are_classified(summary_packet):
    before = copy.deepcopy(summary_packet)
    records = summary_privacy_records(summary_packet)
    by_field = {}
    for record in records:
        by_field.setdefault(record["field"], set()).add(record["text"])
    fact = summary_packet["items"][0]["fact"]
    assert by_field["module_name"] == {summary_packet["module"], fact["module"]}
    assert by_field["title"] == {fact["title"]}
    assert by_field["claims.text"] == {fact["claims"][0]["text"]}
    assert by_field["unknowns"] == {*fact["unknowns"], *summary_packet["synthesis"]["all_unknowns"]}
    assert by_field["source_issue"] == set(summary_packet["coverage"])
    assert by_field["source_path"] == {summary_packet["items"][0]["evidence"][0]["display_path"]}
    assert by_field["code_identifier"] == {summary_packet["items"][0]["relations"][0]["target_name"]}
    assert summary_packet == before


def test_machine_positions_are_excluded_but_canonical_packet_is_unchanged(summary_packet):
    records = summary_privacy_records(summary_packet)
    text = {record["text"] for record in records}
    item, meta = summary_packet["items"][0], summary_packet["synthesis"]
    for value in [summary_packet["module_id"], *meta["child_ids"], *meta["child_hashes"].values(),
                  *meta["leaf_ids"], item["evidence"][0]["path"], item["evidence"][0]["commit_sha"],
                  item["evidence"][0]["blob_sha"], item["relations"][0]["resolution"],
                  item["fact"]["claims"][0]["source_scope"]["packet_digest"]]:
        assert value not in text
    assert len(records) < len(scan_records(summary_packet)) / 2


def test_id_and_hash_looking_free_text_is_always_classified(summary_packet):
    value = summary_packet["items"][0]["fact"]["claims"][0]["source_scope"]["packet_digest"]
    fact = summary_packet["items"][0]["fact"]
    summary_packet["module"] = fact["module"] = fact["title"] = value
    summary_packet["coverage"] = [value]
    summary_packet["synthesis"]["all_unknowns"] = [value]
    fact["unknowns"] = [value]
    fact["claims"][0]["text"] = value
    summary_packet["items"][0]["relations"][0]["target_name"] = value
    summary_packet["items"][0]["evidence"][0]["display_path"] = value
    roles = {record["field"] for record in summary_privacy_records(summary_packet) if record["text"] == value}
    assert {"module_name", "title", "claims.text", "unknowns", "source_issue", "source_path"} <= roles


def test_legacy_explicit_null_display_still_scans_original_path(summary_packet):
    ref = summary_packet["items"][0]["evidence"][0]
    ref["display_path"], ref["path"] = None, "legacy/private_name/component.py"
    assert {"field": "source_path", "text": ref["path"]} in summary_privacy_records(summary_packet)


def test_unknown_only_fragment_preserves_title_and_all_unknowns(summary_packet):
    fact = summary_packet["items"][0]["fact"]
    fact["claims"] = []
    old = fact["child_id"]
    child = stable_id("summary-input", "compile:0", "0", "0", "0", "1")
    fact["child_id"] = child
    meta = summary_packet["synthesis"]
    meta["child_ids"] = [child]
    meta["child_hashes"] = {child: meta["child_hashes"][old]}
    selection = meta["selections"].pop(old)
    selection["claim_end"] = 0
    meta["selections"][child] = selection
    records = summary_privacy_records(summary_packet)
    assert {"field": "title", "text": fact["title"]} in records
    assert {"field": "unknowns", "text": fact["unknowns"][0]} in records
    assert {"field": "unknowns", "text": meta["all_unknowns"][0]} in records


@pytest.mark.parametrize("position", ["packet", "synthesis", "item", "fact", "claim", "scope", "relation", "evidence", "selection"])
def test_unknown_wrappers_are_rejected_with_a_closed_error(summary_packet, position):
    meta = summary_packet["synthesis"]
    item = summary_packet["items"][0]
    values = {"packet": summary_packet, "synthesis": meta, "item": item, "fact": item["fact"],
              "claim": item["fact"]["claims"][0], "scope": item["fact"]["claims"][0]["source_scope"],
              "relation": item["relations"][0], "evidence": item["evidence"][0],
              "selection": meta["selections"][meta["child_ids"][0]]}
    values[position]["private_unknown_wrapper_key"] = {"evidence_ids": 12345678, "private_boolean": True}
    with pytest.raises(ValueError) as caught:
        summary_privacy_records(summary_packet)
    assert str(caught.value) == "汇总隐私投影契约无效"
    assert "private" not in str(caught.value) and "12345678" not in str(caught.value)


@pytest.mark.parametrize("position,field", [("claim", "source_scope"), ("scope", "relationship_view"),
    ("scope", "module_ids"), ("evidence", "display_path"), ("evidence", "symbol_id"),
    ("relation", "candidates"), ("relation", "knowledge_type")])
def test_defaults_cannot_hide_missing_nullable_or_machine_fields(summary_packet, position, field):
    item = summary_packet["items"][0]
    values = {"claim": item["fact"]["claims"][0], "scope": item["fact"]["claims"][0]["source_scope"],
              "relation": item["relations"][0], "evidence": item["evidence"][0]}
    del values[position][field]
    with pytest.raises(ValueError, match="^汇总隐私投影契约无效$"):
        summary_privacy_records(summary_packet)


@pytest.mark.parametrize("fault", ["level_bool", "index_float", "selection_bool", "ref_line_bool", "scope_bool",
    "unknown_scalar", "coverage_scalar", "bad_purpose", "bad_resolution", "bad_type", "candidate_missing",
    "claim_foreign_ref", "relation_foreign_ref", "source_conflict", "child_hash_missing", "child_missing"])
def test_typed_machine_metadata_and_source_membership_are_strict(summary_packet, fault):
    meta, item = summary_packet["synthesis"], summary_packet["items"][0]
    selection = meta["selections"][meta["child_ids"][0]]
    if fault == "level_bool": meta["level"] = False
    elif fault == "index_float": meta["index"] = 0.0
    elif fault == "selection_bool": selection["claim_start"] = False
    elif fault == "ref_line_bool": item["evidence"][0]["line_start"] = True
    elif fault == "scope_bool": item["fact"]["claims"][0]["source_scope"]["fragment_index"] = False
    elif fault == "unknown_scalar": item["fact"]["unknowns"] = [12345678]
    elif fault == "coverage_scalar": summary_packet["coverage"] = [True]
    elif fault == "bad_purpose": meta["purpose"] = "private_unknown_purpose"
    elif fault == "bad_resolution": item["relations"][0]["resolution"] = "private_unknown_status"
    elif fault == "bad_type": item["relations"][0]["type"] = "private_unknown_type"
    elif fault == "candidate_missing": item["relations"][0].update(resolution="candidate", to_id=None, candidates=[])
    elif fault == "claim_foreign_ref": item["fact"]["claims"][0]["evidence_ids"] = ["evidence:" + "f" * 64]
    elif fault == "relation_foreign_ref": item["relations"][0]["source_ref_ids"] = ["evidence:" + "f" * 64]
    elif fault == "source_conflict": item["evidence"].append({**item["evidence"][0], "blob_sha": "f" * 40})
    elif fault == "child_hash_missing": meta["child_hashes"].clear()
    else: meta["child_ids"].clear()
    with pytest.raises(ValueError) as caught:
        summary_privacy_records(summary_packet)
    assert str(caught.value) == "汇总隐私投影契约无效"


def test_candidate_and_unresolved_states_are_retained_not_rewritten(summary_packet):
    relation = summary_packet["items"][0]["relations"][0]
    relation.update(resolution="candidate", to_id=None, candidates=["fact:" + "8" * 64])
    before = copy.deepcopy(summary_packet)
    candidate = summary_privacy_records(summary_packet)
    assert summary_packet == before
    relation.update(resolution="unresolved", candidates=[])
    unresolved = summary_privacy_records(summary_packet)
    assert candidate == unresolved


def test_project_summary_uses_explicit_project_metadata(summary_packet):
    summary_packet["module_id"] = "project"
    summary_packet["synthesis"].update(purpose="project", level=1)
    assert summary_privacy_records(summary_packet)
