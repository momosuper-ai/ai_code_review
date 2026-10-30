"""Lossless compact transport for Project Brain semantic evidence packets."""

import copy
import json

import pytest

from src.core.project_brain.context import compact_packet, read_rows


def fixture_packet() -> dict:
    """Build deliberately overlapping evidence and all resolution states."""
    module_id = "module:" + "a" * 24
    fact_id = "symbol:" + "b" * 24
    evidence_id = "evidence:" + "c" * 24
    other = "symbol:" + "d" * 24
    refs = [
        {"id": evidence_id, "commit_sha": "e" * 40, "blob_sha": "f" * 40,
         "path": "src/service.py", "line_start": 10, "line_end": 100,
         "excerpt_end": 12, "excerpt": "def handler():\n    prepare()\n    execute()",
         "symbol_id": fact_id},
        {"id": "evidence:" + "1" * 24, "commit_sha": "e" * 40, "blob_sha": "f" * 40,
         "path": "src/service.py", "line_start": 11, "line_end": 12,
         "excerpt_end": 12, "excerpt": "    prepare()\n    execute()", "symbol_id": fact_id},
    ]
    relations = []
    for index, resolution in enumerate(("resolved", "candidate", "unresolved")):
        relations.append({"id": "relation:" + str(index) * 24, "from_id": fact_id,
                          "type": "CALLS", "to_id": other if resolution == "resolved" else None,
                          "target_name": "other.execute", "resolution": resolution,
                          "knowledge_type": "FACT", "source_ref_ids": [refs[1]["id"]],
                          "candidates": [other] if resolution == "candidate" else []})
    fact = {"id": fact_id, "kind": "symbol", "name": "handler", "module_id": module_id,
            "source_ref_ids": [evidence_id], "extractor": "brain.python/1",
            "attributes": {"path": "src/service.py", "language": "python",
                           "signature": "handler()", "assigned_names": ["temporary"],
                           "shadowed_names": ["temporary"], "custom_semantics": {"mode": "strict"}}}
    return {"module_id": module_id, "module": "src/service", "coverage": ["动态调用未知"],
            "items": [{"fact": fact, "relations": relations, "evidence": refs}]}


def decoded_lines(source: dict) -> list[list]:
    """Independently reconstruct exact numbered lines from offered chunks."""
    lines = []
    for start, text in source.get("chunks", []):
        text_lines = text.split("\n")
        lines.extend([[number, value] for number, value in enumerate(text_lines, start)])
    return lines


def test_compaction_does_not_mutate_canonical_facts() -> None:
    packet = fixture_packet()
    before = copy.deepcopy(packet)
    result = compact_packet(packet)
    assert packet == before
    assert result.wire["protocol"] == "project-brain-context/1"
    assert result.wire["coverage"] == packet["coverage"]
    assert "assigned_names" not in json.dumps(result.wire)
    assert "shadowed_names" not in json.dumps(result.wire)
    assert "custom_semantics" in json.dumps(result.wire)
    assert result.wire["items"][0]["evidence"]


def test_relation_rows_retain_targets_candidates_and_resolution() -> None:
    result = compact_packet(fixture_packet())
    relations = read_rows(result.wire, "relations")
    states = {row["resolution"]: row for row in relations}
    assert states["resolved"]["to_id"]
    assert states["resolved"]["candidates"] == []
    assert states["candidate"]["to_id"] is None
    assert states["candidate"]["candidates"] == [states["resolved"]["to_id"]]
    assert states["unresolved"]["to_id"] is None
    assert states["unresolved"]["candidates"] == []
    assert all(row["target_name"] == "other.execute" for row in relations)
    assert all(row["source_ref_ids"] for row in relations)


def test_source_identity_and_exact_excerpt_ranges_survive() -> None:
    packet = fixture_packet()
    result = compact_packet(packet)
    sources = result.wire["sources"]
    assert len(sources) == 1
    source = next(iter(sources.values()))
    assert source["path"] == "src/service.py"
    assert source["commit_sha"] == "e" * 40
    assert source["blob_sha"] == "f" * 40
    assert result.wire["chunk_columns"] == ["line_start", "text"]
    assert source["chunks"] == [[10, "def handler():\n    prepare()\n    execute()"]]
    assert decoded_lines(source) == [[10, "def handler():"], [11, "    prepare()"], [12, "    execute()"]]
    rows = read_rows(result.wire, "evidence")
    by_id = {result.restore_evidence_ids([row["id"]])[0]: row for row in rows}
    for original in packet["items"][0]["evidence"]:
        compact = by_id[original["id"]]
        assert compact["line_start"] == original["line_start"]
        assert compact["line_end"] == original["line_end"]
        assert compact["excerpt_end"] == original["excerpt_end"]
        assert compact["symbol_id"]


def test_evidence_alias_restore_is_strict_and_compatible() -> None:
    result = compact_packet(fixture_packet())
    alias, canonical = next(iter(result.evidence_ids.items()))
    assert result.restore_evidence_ids([alias, canonical]) == [canonical, canonical]
    with pytest.raises(ValueError, match="证据"):
        result.restore_evidence_ids(["E99999"])
    with pytest.raises(ValueError, match="证据"):
        result.restore_evidence_ids(["evidence:" + "9" * 24])
    with pytest.raises(ValueError, match="证据"):
        result.restore_evidence_ids([42])
    with pytest.raises(ValueError, match="数组"):
        result.restore_evidence_ids(alias)


def test_repeated_declaration_fragments_share_facts_and_evidence() -> None:
    packet = fixture_packet()
    first = packet["items"][0]
    second = copy.deepcopy(first)
    first["relations"] = first["relations"][:1]
    first["fragment"] = {"index": 0, "total": 2}
    second["relations"] = second["relations"][1:]
    second["fragment"] = {"index": 1, "total": 2}
    packet["items"].append(second)
    result = compact_packet(packet)
    assert len(result.wire["facts"]) == 1
    assert len(result.wire["evidence"]) == 2
    assert len(result.wire["relations"]) == 3
    assert len(result.wire["items"]) == 2
    assert result.wire["items"][0]["fragment"] == {"index": 0, "total": 2}
    assert result.wire["items"][1]["fragment"] == {"index": 1, "total": 2}


def test_repeated_excerpt_content_appears_once_on_the_wire() -> None:
    result = compact_packet(fixture_packet())
    wire = json.dumps(result.wire, ensure_ascii=False)
    assert wire.count("prepare()") == 1
    assert wire.count("execute()") == 1
    assert wire.count("src/service.py") == 1


def test_conflicting_evidence_identity_is_rejected() -> None:
    packet = fixture_packet()
    duplicate = copy.deepcopy(packet["items"][0]["evidence"][0])
    duplicate["blob_sha"] = "9" * 40
    packet["items"][0]["evidence"].append(duplicate)
    with pytest.raises(ValueError, match="证据"):
        compact_packet(packet)


def test_conflicting_source_lines_are_not_merged() -> None:
    packet = fixture_packet()
    packet["items"][0]["evidence"][1]["excerpt"] = "    changed()\n    execute()"
    with pytest.raises(ValueError, match="片段"):
        compact_packet(packet)


def test_missing_relation_evidence_is_rejected() -> None:
    packet = fixture_packet()
    packet["items"][0]["relations"][0]["source_ref_ids"] = ["evidence:missing"]
    with pytest.raises(ValueError, match="证据"):
        compact_packet(packet)


def test_unknown_fact_and_relation_fields_are_preserved() -> None:
    packet = fixture_packet()
    packet["items"][0]["fact"]["future_semantics"] = {"flag": True}
    packet["items"][0]["relations"][0]["future_semantics"] = "guarded"
    result = compact_packet(packet)
    fact = read_rows(result.wire, "facts")[0]
    relation = read_rows(result.wire, "relations")[0]
    assert fact["extra"]["future_semantics"] == {"flag": True}
    assert relation["extra"]["future_semantics"] == "guarded"


def test_table_constants_are_hoisted_without_losing_values() -> None:
    packet = fixture_packet()
    template = packet["items"][0]["relations"][2]
    packet["items"][0]["relations"] = [
        {**copy.deepcopy(template), "id": f"relation:{index:024d}", "target_name": f"target_{index}"}
        for index in range(30)
    ]
    result = compact_packet(packet)
    rows = read_rows(result.wire, "relations")
    assert len(rows) == 30
    assert all(row["resolution"] == "unresolved" for row in rows)
    assert all(row["candidates"] == [] for row in rows)
    assert all(row["knowledge_type"] == "FACT" for row in rows)
    assert "knowledge_type" in result.wire["defaults"]["relations"]
    assert "knowledge_type" not in result.wire["columns"]["relations"]
    assert "id" in result.wire["columns"]["relations"]


def test_summary_claim_evidence_is_aliased_and_restorable() -> None:
    packet = fixture_packet()
    original = packet["items"][0]["evidence"][0]
    packet["items"] = [{"fact": {"module": "service", "claims": [
        {"text": "负责处理请求", "evidence_ids": [original["id"]]}]},
        "relations": [], "evidence": [original]}]
    result = compact_packet(packet)
    claim = read_rows(result.wire, "facts")[0]["extra"]["claims"][0]
    assert result.restore_evidence_ids(claim["evidence_ids"]) == [original["id"]]


def test_excerpt_never_expands_beyond_original_source_ref() -> None:
    packet = fixture_packet()
    packet["items"][0]["evidence"][0]["line_end"] = 11
    with pytest.raises(ValueError, match="片段"):
        compact_packet(packet)


def test_future_envelope_fields_cannot_override_the_protocol() -> None:
    packet = fixture_packet()
    packet["protocol"] = "future-canonical-protocol"
    packet["sources"] = {"meaning": "preserved"}
    result = compact_packet(packet)
    assert result.wire["protocol"] == "project-brain-context/1"
    assert result.wire["extra"]["protocol"] == "future-canonical-protocol"
    assert result.wire["extra"]["sources"] == {"meaning": "preserved"}


def test_shortened_or_noticed_source_excerpt_is_explicit() -> None:
    packet = fixture_packet()
    ref = packet["items"][0]["evidence"][0]
    ref["excerpt"] = "（此行过长，仅提供声明事实）"
    # Its original offered range is retained, but the notice must not become
    # twelve invented physical source lines in the deduplicated line pool.
    result = compact_packet(packet)
    row = next(row for row in read_rows(result.wire, "evidence")
               if result.restore_evidence_ids([row["id"]]) == [ref["id"]])
    assert row["snippet"] in result.wire["snippets"]
    assert result.wire["snippets"][row["snippet"]] == ref["excerpt"]
    assert row["line_end"] == ref["line_end"]
    assert row["excerpt_end"] == ref["excerpt_end"]


def test_many_candidates_are_all_present_and_distinct() -> None:
    packet = fixture_packet()
    candidates = [f"symbol:{index:024d}" for index in range(200)]
    relation = packet["items"][0]["relations"][1]
    relation["candidates"] = candidates
    result = compact_packet(packet)
    row = next(row for row in read_rows(result.wire, "relations") if row["resolution"] == "candidate")
    assert len(row["candidates"]) == 200
    assert len(set(row["candidates"])) == 200
    assert row["to_id"] is None


def test_relation_duplicate_identity_conflict_is_rejected() -> None:
    packet = fixture_packet()
    repeated = copy.deepcopy(packet["items"][0]["relations"][0])
    repeated["resolution"] = "candidate"
    packet["items"][0]["relations"].append(repeated)
    with pytest.raises(ValueError, match="关系"):
        compact_packet(packet)


def test_every_wire_table_has_the_complete_canonical_entity_count() -> None:
    packet = fixture_packet()
    result = compact_packet(packet)
    assert len(read_rows(result.wire, "facts")) == 1
    assert len(read_rows(result.wire, "relations")) == len(packet["items"][0]["relations"])
    assert len(read_rows(result.wire, "evidence")) == len(packet["items"][0]["evidence"])


def test_source_chunks_preserve_gaps_blank_lines_tabs_and_indentation() -> None:
    packet = fixture_packet()
    refs = packet["items"][0]["evidence"]
    first = refs[0]
    first["excerpt"] = "def handler():\n\n\tprepare()"
    second = refs[1]
    second["line_start"], second["line_end"], second["excerpt_end"] = 20, 21, 21
    second["excerpt"] = "    execute()\n"
    result = compact_packet(packet)
    source = next(iter(result.wire["sources"].values()))
    assert len(source["chunks"]) == 2
    assert decoded_lines(source) == [[10, "def handler():"], [11, ""], [12, "\tprepare()"],
                                    [20, "    execute()"], [21, ""]]
    assert all(number not in {13, 14, 15, 16, 17, 18, 19} for number, _ in decoded_lines(source))


def test_scope_audit_sets_stay_service_side_but_redefinition_survives() -> None:
    packet = fixture_packet()
    attrs = packet["items"][0]["fact"]["attributes"]
    attrs["closure_bindings"] = ["enclosing"]
    attrs["scope_shadowed_calls"] = [packet["items"][0]["evidence"][0]["id"]]
    attrs["redefined"] = True
    before = copy.deepcopy(packet)
    result = compact_packet(packet)
    compact_attrs = read_rows(result.wire, "facts")[0]["attributes"]
    assert "closure_bindings" not in compact_attrs
    assert "scope_shadowed_calls" not in compact_attrs
    assert compact_attrs["redefined"] is True
    assert packet == before
    assert len(read_rows(result.wire, "relations")) == 3
