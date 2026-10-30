"""Offline synthesis contracts: one global slot, complete retained provenance."""

import copy
import hashlib
import json

import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.project_brain.compiler import compilation_transport, compile_packet, encoded, packet_wire
from src.core.project_brain.context import compact_packet
from src.core.project_brain.privacy_projection import compilation_privacy_records
from src.core.project_brain.schema import InferenceScope, InferredClaim, stable_id
from src.core.project_brain.source_bound import SourceBoundContractError, SourceBoundPlan
from src.core.project_brain.synthesis_wire import expand_synthesis_wire
from src.core.project_brain.synthesis_wire import canonical_relationship_digest, validate_resolution_counts_wire
from tests.unit.test_brain_compiler_transport import model, transport
from tests.unit.test_brain_strict_transport import formatter_result


@pytest.fixture
def synthesis(transport):
    """Two authenticated-shaped child inputs; no real gateway or code execution."""
    packet = {"module_id": next(iter(transport.bundle.modules)), "module": "pkg", "coverage": ["静态关系不是运行时证明"], "items": []}
    ids, hashes, selections = [], {}, {}
    for index, (fact, ref) in enumerate(zip(transport.bundle.facts, transport.bundle.source_refs, strict=True)):
        key = f"compile:{index}"
        child = stable_id("summary-input", key, "0", "1", "0", "1")
        claims = [InferredClaim(text=f"完整子职责{index}，保留引号\"与换行\n以及模块边界。", evidence_ids=[ref.id]).model_dump()]
        unknowns = [f"子概览未知项{index}"]
        hashes[child] = hashlib.sha256(encoded({"claims": claims, "unknowns": unknowns}).encode()).hexdigest()
        selections[child] = {"source_key": key, "claim_start": 0, "claim_end": 1, "unknown_start": 0, "unknown_end": 1}
        ids.append(child)
        packet["items"].append({"fact": {"module": "pkg", "title": fact.name, "claims": claims, "unknowns": unknowns, "child_id": child},
                                "relations": [transport.bundle.relations[0].model_dump()] if index == 0 else [], "evidence": [ref.model_dump()]})
    packet["synthesis"] = {"purpose": "module", "level": 0, "index": 0, "child_ids": ids,
                            "module_ids": [packet["module_id"]], "leaf_ids": ["compile:0", "compile:1"],
                            "child_hashes": hashes, "selections": selections,
                            "all_unknowns": ["完整历史限制仅存服务详情，绝不能删除", *[item["fact"]["unknowns"][0] for item in packet["items"]]]}
    return packet


def response(*, text="该模块由两个子组件协作，候选行为仍需核实。"):
    return {"title": "模块整体职责", "slots": {"S1": {"texts": [text], "unknowns": ["本次综合限制"]}}, "unknowns": [], "document_path": None}


def plan(packet):
    return SourceBoundPlan.from_packet(packet, compact_packet(packet))


def test_complete_collection_binds_single_slot_and_all_canonical_sources(synthesis):
    before = copy.deepcopy(synthesis)
    bound = plan(synthesis)
    assert list(bound.schema()["properties"]["slots"]["properties"]) == ["S1"]
    result = bound.bind(encoded(response()))
    scope = result.claims[0].source_scope
    assert scope.kind == "summary_synthesis"
    assert scope.relationship_view == "symbol_graph"
    assert scope.fact_id is None and scope.relation_ids == []
    assert scope.child_ids == synthesis["synthesis"]["child_ids"]
    assert scope.module_ids == synthesis["synthesis"]["module_ids"]
    assert set(result.claims[0].evidence_ids) == {ref["id"] for item in synthesis["items"] for ref in item["evidence"]}
    assert scope.packet_digest == hashlib.sha256(encoded(synthesis).encode()).hexdigest()
    assert synthesis == before
    # Overview limitations are intentionally synthesized; inherited details
    # stay complete in the canonical input and are retained by assembly.
    assert result.unknowns == ["本次综合限制"]
    assert "完整历史限制仅存服务详情，绝不能删除" in synthesis["synthesis"]["all_unknowns"]


def test_wire_keeps_every_overview_string_without_duplicate_evidence_payload(synthesis):
    wire = plan(synthesis).wire(compact_packet(synthesis).wire)
    assert wire["protocol"] == "project-brain-synthesis/2" and wire["aggregate_slot"] == "S1"
    for child, item in zip(wire["children"], synthesis["items"], strict=True):
        assert [claim["text"] for claim in child["claims"]] == [claim["text"] for claim in item["fact"]["claims"]]
        assert child["unknowns"] == item["fact"]["unknowns"]
        assert {claim["knowledge_type"] for claim in child["claims"]} == {"AI_INFERRED"}
    assert wire["coverage"] == synthesis["coverage"]
    assert wire["inherited_limitations"] == {"count": 3, "detail_retained": True}
    assert "完整历史限制仅存服务详情，绝不能删除" not in encoded(wire)
    assert all(ref["id"] not in encoded(wire) and ref["path"] not in encoded(wire)
               for item in synthesis["items"] for ref in item["evidence"])
    assert all(child not in encoded(wire) for child in synthesis["synthesis"]["child_ids"])


def test_relationship_topology_retains_candidate_and_unresolved_states(synthesis):
    relation = synthesis["items"][0]["relations"][0]
    target = relation["to_id"]
    for resolution in ("candidate", "unresolved"):
        extra = copy.deepcopy(relation)
        extra.update(id=stable_id("relation", resolution), to_id=None, resolution=resolution,
                     candidates=[target, "symbol:alternative"] if resolution == "candidate" else [])
        synthesis["items"][0]["relations"].append(extra)
    wire = plan(synthesis).wire({})
    by_state = {value["resolution"]: value for value in expand_synthesis_wire(wire)["relationships"]}
    assert set(by_state) == {"resolved", "candidate", "unresolved"}
    assert len(by_state["candidate"]["candidates"]) == 2
    assert by_state["resolved"]["to"] in by_state["candidate"]["candidates"]
    assert by_state["unresolved"]["to"] is None
    assert plan(synthesis).scopes["S1"].relation_ids == []


@pytest.mark.parametrize("purpose,level", [("module", 1), ("project", 0), ("project", 1)])
def test_upper_view_keeps_canonical_graph_but_only_transmits_explicit_counts(synthesis, purpose, level):
    synthesis["synthesis"].update(purpose=purpose, level=level)
    before = copy.deepcopy(synthesis)
    bound = plan(synthesis)
    wire = bound.wire({})
    assert wire["protocol"] == "project-brain-synthesis/3"
    assert wire["relationships"]["view"] == "resolution_counts"
    assert wire["relationships"]["total"] == 1
    assert wire["relationships"]["detail_retained"] is True
    relations = [r for item in synthesis["items"] for r in item["relations"]]
    assert wire["relationships"]["canonical_digest"] == canonical_relationship_digest(relations)
    assert "entities" not in wire
    assert synthesis == before
    assert validate_resolution_counts_wire(wire) == wire
    assert bound.scopes["S1"].relationship_view == "resolution_counts"
    assert bound.scopes["S1"].source_ref_ids == sorted(ref["id"] for item in synthesis["items"] for ref in item["evidence"])
    assert [child["claims"][0]["text"] for child in wire["children"]] == [item["fact"]["claims"][0]["text"] for item in synthesis["items"]]
    assert [child["unknowns"] for child in wire["children"]] == [item["fact"]["unknowns"] for item in synthesis["items"]]
    assert all(r["id"] not in encoded(wire) and r["target_name"] not in encoded(wire["relationships"]) for r in relations)


@pytest.mark.asyncio
async def test_upper_privacy_still_scans_all_raw_relations_and_digest_binds_preclean_input(synthesis, transport, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "auto")
    synthesis["synthesis"]["level"] = 1
    secret = "synthetic-hidden-topology-credential"
    synthesis["items"][0]["relations"][0]["target_name"] = secret
    original_digest = canonical_relationship_digest([r for item in synthesis["items"] for r in item["relations"]])
    clean = transport.guard.clean
    scans = []
    async def scan(text):
        scans.append(json.loads(text))
        if secret in text:
            transport.guard.filter._dynamic_values[secret] = "SECRET"
        return await clean(text)
    monkeypatch.setattr(transport.guard, "clean", scan)
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=encoded(response()))])
    compiled = await compile_packet(synthesis, transport.guard)
    wire = json.loads(mock.calls[0][1]["content"])
    assert scans[0] == synthesis
    assert wire["relationships"]["canonical_digest"] == original_digest
    assert secret not in encoded(wire)
    assert compiled.claims[0].source_scope.relationship_view == "resolution_counts"
    assert compiled.claims[0].source_scope.packet_digest == hashlib.sha256(encoded(synthesis).encode()).hexdigest()


@pytest.mark.parametrize("collection", ["evidence", "relations"])
def test_conflicting_canonical_identity_cannot_enter_compact_transport(synthesis, collection):
    original_context = compact_packet(synthesis)
    value = copy.deepcopy(synthesis["items"][0][collection][0])
    if collection == "evidence":
        value["path"] = "private_untrusted_value"
    else:
        value["target_name"] = "private_untrusted_value"
        synthesis["items"][1]["evidence"].append(copy.deepcopy(synthesis["items"][0]["evidence"][0]))
    synthesis["items"][1][collection].append(value)
    with pytest.raises(ValueError) as caught:
        SourceBoundPlan.from_packet(synthesis, original_context)
    assert str(caught.value) == "综合知识输入契约无效"
    assert "private_untrusted_value" not in str(caught.value)


@pytest.mark.parametrize("change", ["metadata_extra", "item_extra", "claim_extra", "hash", "child", "span", "span_bool", "module", "empty", "identity"])
def test_invalid_input_is_rejected_with_fixed_safe_diagnostic(synthesis, change):
    meta, item = synthesis["synthesis"], synthesis["items"][0]
    child = meta["child_ids"][0]
    if change == "metadata_extra": meta["private_untrusted_key"] = "private_untrusted_value"
    elif change == "item_extra": item["private_untrusted_key"] = "private_untrusted_value"
    elif change == "claim_extra": item["fact"]["claims"][0]["private_untrusted_key"] = "private_untrusted_value"
    elif change == "hash": meta["child_hashes"][child] = "private_untrusted_value"
    elif change == "child": meta["child_ids"].append(child)
    elif change == "span": meta["selections"][child]["claim_end"] = 2
    elif change == "span_bool": meta["selections"][child]["claim_end"] = True
    elif change == "module": meta["module_ids"] = []
    elif change == "empty": item["fact"].update(claims=[], unknowns=[])
    else: item["fact"]["child_id"] = "private_untrusted_value"
    with pytest.raises(ValueError) as caught:
        plan(synthesis)
    assert "private_untrusted_key" not in str(caught.value)
    assert "private_untrusted_value" not in str(caught.value)


def test_model_cannot_return_child_slots_or_select_provenance(synthesis):
    value = response()
    value["slots"]["S2"] = {"texts": ["额外局部说明"], "unknowns": []}
    with pytest.raises(SourceBoundContractError):
        plan(synthesis).bind(encoded(value))


def test_model_cannot_choose_or_upgrade_relationship_view(synthesis):
    synthesis["synthesis"]["level"] = 1
    value = response()
    value["slots"]["S1"]["relationship_view"] = "symbol_graph"
    with pytest.raises(ValueError):
        plan(synthesis).bind(encoded(value))
    assert plan(synthesis).scopes["S1"].relationship_view == "resolution_counts"


def test_synthesis_scope_roundtrips_complete_children_and_modules_in_summary_context(synthesis):
    compiled = plan(synthesis).bind(encoded(response()))
    item = {"fact": {"module": "pkg", "title": compiled.title, "claims": [claim.model_dump() for claim in compiled.claims],
                     "unknowns": compiled.unknowns}, "relations": [],
            "evidence": [ref for value in synthesis["items"] for ref in value["evidence"]]}
    context = compact_packet({"module_id": "project", "module": "项目概览", "items": [item], "coverage": []})
    assert context.restore_source_scope("I1") == compiled.claims[0].source_scope.model_dump()
    records = compilation_privacy_records(compiled)
    assert not any(record["text"] in synthesis["synthesis"]["child_ids"] + synthesis["synthesis"]["module_ids"] for record in records)


def test_legacy_scope_implicitly_restores_empty_synthesis_identity_fields(synthesis):
    scope = InferenceScope(id="scope:" + "0" * 24, kind="summary_group", packet_digest="a" * 64,
                           fact_id=None, relation_ids=[], source_ref_ids=[synthesis["items"][0]["evidence"][0]["id"]],
                           fragment_index=0, fragment_total=1).model_dump()
    scope.pop("child_ids")
    scope.pop("module_ids")
    item = copy.deepcopy(synthesis["items"][0])
    item["fact"]["claims"][0]["source_scope"] = scope
    context = compact_packet({"module_id": "project", "module": "项目概览", "items": [item], "coverage": []})
    restored = context.restore_source_scope("I1")
    assert {key: value for key, value in restored.items() if key not in {"child_ids", "module_ids"}} == scope
    assert restored["child_ids"] == restored["module_ids"] == []


def test_unknown_only_complete_fragment_still_retains_full_canonical_sources(synthesis):
    child = synthesis["synthesis"]["child_ids"][0]
    selection = synthesis["synthesis"]["selections"][child]
    selection["claim_start"] = selection["claim_end"] = 1
    changed = stable_id("summary-input", selection["source_key"], "1", "1", "0", "1")
    synthesis["synthesis"]["child_ids"][0] = changed
    synthesis["synthesis"]["selections"][changed] = synthesis["synthesis"]["selections"].pop(child)
    synthesis["synthesis"]["child_hashes"][changed] = synthesis["synthesis"]["child_hashes"].pop(child)
    synthesis["items"][0]["fact"].update(child_id=changed, claims=[])
    bound = plan(synthesis)
    assert bound.synthesis_wire["children"][0]["claims"] == []
    assert bound.synthesis_wire["children"][0]["unknowns"]
    assert set(bound.scopes["S1"].source_ref_ids) == {ref["id"] for item in synthesis["items"] for ref in item["evidence"]}
    value = response()
    value["slots"]["S1"]["evidence_ids"] = ["forged"]
    with pytest.raises(ValueError):
        plan(synthesis).bind(encoded(value))


@pytest.mark.parametrize("mode", ["auto", "strict_tool", "source_bound"])
def test_each_transport_mode_uses_single_service_bound_synthesis_schema(synthesis, monkeypatch, mode):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", mode)
    messages, kwargs, bound = compilation_transport(synthesis, compact_packet(synthesis))
    assert bound is not None and len(bound.scopes) == 1
    assert json.loads(messages[1]["content"])["protocol"] == "project-brain-synthesis/2"
    assert packet_wire(synthesis) == json.loads(messages[1]["content"])
    if mode == "auto":
        schema = kwargs["json_schema_builder"]()["json_schema"]["schema"]
        assert "tools" not in kwargs
    else:
        schema = kwargs["tools"][0]["function"]["parameters"]
    assert set(schema["properties"]) == {"title", "slots", "unknowns", "document_path"}
    assert list(schema["properties"]["slots"]["properties"]) == ["S1"]
    assert "source_scope" not in encoded(schema) and "evidence_ids" not in encoded(schema)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "strict_tool", "source_bound"])
async def test_compiler_scans_full_canonical_input_and_binds_full_sources(synthesis, transport, monkeypatch, mode):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", mode)
    result = (ChatCompletionMessage(role="assistant", content=encoded(response())) if mode == "auto"
              else formatter_result(encoded(response())))
    mock = model(monkeypatch, [result])
    scans = []
    clean = transport.guard.clean
    async def scan(text):
        scans.append(json.loads(text))
        return await clean(text)
    monkeypatch.setattr(transport.guard, "clean", scan)
    compiled = await compile_packet(synthesis, transport.guard)
    assert scans[0] == synthesis and scans[0]["synthesis"]["all_unknowns"]
    assert compiled.claims[0].source_scope.kind == "summary_synthesis"
    assert len(mock.calls) == 1
    assert json.loads(mock.calls[0][1]["content"])["protocol"] == "project-brain-synthesis/2"
    assert compiled.claims[0].source_scope.child_ids == synthesis["synthesis"]["child_ids"]


@pytest.mark.asyncio
async def test_new_privacy_findings_change_wire_text_without_changing_canonical_digest(synthesis, transport, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "auto")
    secret = "synthetic-new-overview-credential"
    synthesis["items"][0]["fact"]["claims"][0]["text"] = "职责中出现凭证：" + secret
    digest = hashlib.sha256(encoded(synthesis).encode()).hexdigest()
    clean = transport.guard.clean
    async def scan(text):
        if secret in text:
            transport.guard.filter._dynamic_values[secret] = "SECRET"
        return await clean(text)
    monkeypatch.setattr(transport.guard, "clean", scan)
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=encoded(response()))])
    compiled = await compile_packet(synthesis, transport.guard)
    assert secret not in mock.calls[0][1]["content"]
    assert compiled.claims[0].source_scope.packet_digest == digest


@pytest.mark.asyncio
async def test_privacy_mutating_checkpoint_identity_prevents_semantic_request(synthesis, transport, monkeypatch):
    clean = transport.guard.clean
    async def scan(text):
        value = json.loads(await clean(text))
        if "synthesis" in value:
            value["synthesis"]["child_hashes"][value["synthesis"]["child_ids"][0]] = "0" * 64
        return encoded(value)
    monkeypatch.setattr(transport.guard, "clean", scan)
    mock = model(monkeypatch, [])
    with pytest.raises(ValueError, match="综合来源身份"):
        await compile_packet(synthesis, transport.guard)
    assert mock.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "strict_tool", "source_bound"])
async def test_hmac_redacted_source_path_copy_keeps_canonical_binding(synthesis, transport, monkeypatch, tmp_path, mode):
    """A credential fingerprint can also match a source filename component."""
    from src.core.project_brain.privacy import LedgerPrivacyFilter, PrivacyLedger

    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", mode)
    original = copy.deepcopy(synthesis)
    digest = hashlib.sha256(encoded(synthesis).encode()).hexdigest()
    ledger = PrivacyLedger(tmp_path / "private")
    ledger.remember([{"value": "caller", "type": "SECRET"}])
    transport.guard.filter = LedgerPrivacyFilter(ledger)
    assert transport.guard.filter.sanitize_text("pkg/caller.py") != "pkg/caller.py"
    result = (ChatCompletionMessage(role="assistant", content=encoded(response())) if mode == "auto"
              else formatter_result(encoded(response())))
    mock = model(monkeypatch, [result])
    compiled = await compile_packet(synthesis, transport.guard)
    assert synthesis == original
    assert compiled.claims[0].source_scope.packet_digest == digest
    assert compiled.claims[0].source_scope.source_ref_ids == plan(original).scopes["S1"].source_ref_ids
    assert len(mock.calls) == 1
    wire = json.loads(mock.calls[0][1]["content"])
    assert wire["protocol"] == "project-brain-synthesis/2"
    assert all(ref["path"] not in encoded(wire) for item in original["items"] for ref in item["evidence"])


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("path", "pkg/forged.py"), ("id", "evidence:" + "f" * 24),
    ("blob_sha", "f" * 40), ("commit_sha", "f" * 40), ("line_start", 2), ("symbol_id", "symbol:" + "f" * 24)])
async def test_synthesis_evidence_mutation_still_prevents_semantic_request(synthesis, transport, monkeypatch, field, value):
    clean = transport.guard.clean
    async def scan(text):
        data = json.loads(await clean(text))
        if "synthesis" in data:
            data["items"][0]["evidence"][0][field] = value
        return encoded(data)
    monkeypatch.setattr(transport.guard, "clean", scan)
    mock = model(monkeypatch, [])
    with pytest.raises(ValueError):
        await compile_packet(synthesis, transport.guard)
    assert mock.calls == []


@pytest.fixture
def inherited_synthesis(synthesis):
    synthesis["synthesis"]["level"] = 1
    synthesis["items"][0]["fact"]["claims"][0]["source_scope"] = InferenceScope(
        id="scope:" + "d" * 24, kind="summary_synthesis", packet_digest="d" * 64,
        fact_id=None, relation_ids=[], source_ref_ids=[synthesis["items"][0]["evidence"][0]["id"]],
        fragment_index=0, fragment_total=2, child_ids=["summary-input:" + "e" * 24],
        module_ids=synthesis["synthesis"]["module_ids"], relationship_view="symbol_graph").model_dump()
    return synthesis


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("id", "scope:" + "f" * 24), ("kind", "summary_group"), ("packet_digest", "f" * 64),
    ("fact_id", "symbol:" + "f" * 24), ("relation_ids", ["relation:" + "f" * 24]),
    ("source_ref_ids", "duplicate"), ("fragment_index", 1), ("fragment_total", 3),
    ("fragment_index", False), ("fragment_total", 2.0),
    ("child_ids", ["summary-input:" + "f" * 24]), ("module_ids", ["module:" + "f" * 24]),
    ("relationship_view", "resolution_counts"), ("future_private_field", "private_untrusted_value")])
async def test_inherited_scope_machine_mutations_are_rejected_before_semantic_io(
        inherited_synthesis, transport, monkeypatch, field, value):
    clean = transport.guard.clean
    async def scan(text):
        data = json.loads(await clean(text))
        if "synthesis" in data:
            scope = data["items"][0]["fact"]["claims"][0]["source_scope"]
            scope[field] = scope[field] * 2 if field == "source_ref_ids" else value
        return encoded(data)
    monkeypatch.setattr(transport.guard, "clean", scan)
    mock = model(monkeypatch, [])
    with pytest.raises(ValueError) as caught:
        await compile_packet(inherited_synthesis, transport.guard)
    assert str(caught.value) == "隐私过滤改变了服务侧综合来源身份"
    assert "private_untrusted" not in str(caught.value)
    assert mock.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["claim_evidence", "remove_scope", "claim_text", "relation_target"])
async def test_synthesis_binding_or_arbitrary_text_replacement_is_rejected_before_io(
        inherited_synthesis, transport, monkeypatch, change):
    clean = transport.guard.clean
    async def scan(text):
        data = json.loads(await clean(text))
        if "synthesis" in data:
            claim = data["items"][0]["fact"]["claims"][0]
            if change == "claim_evidence": claim["evidence_ids"] *= 2
            elif change == "remove_scope": claim["source_scope"] = None
            elif change == "claim_text": claim["text"] = "private_untrusted_value"
            else: data["items"][0]["relations"][0]["target_name"] = "private_untrusted_value"
        return encoded(data)
    monkeypatch.setattr(transport.guard, "clean", scan)
    mock = model(monkeypatch, [])
    with pytest.raises(ValueError, match="综合来源身份"):
        await compile_packet(inherited_synthesis, transport.guard)
    assert mock.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_kind", ["current", "old", "none"])
async def test_exact_text_target_redaction_preserves_current_legacy_or_absent_scope(
        inherited_synthesis, transport, monkeypatch, scope_kind):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "auto")
    claim = inherited_synthesis["items"][0]["fact"]["claims"][0]
    if scope_kind == "old":
        claim["source_scope"].pop("relationship_view")
    elif scope_kind == "none":
        claim["source_scope"] = None
    secret = "synthetic-valid-redaction-credential"
    claim["text"] += " " + secret
    inherited_synthesis["items"][0]["relations"][0]["target_name"] = secret
    before = copy.deepcopy(inherited_synthesis)
    clean = transport.guard.clean
    async def scan(text):
        if secret in text:
            transport.guard.filter._dynamic_values[secret] = "SECRET"
        return await clean(text)
    monkeypatch.setattr(transport.guard, "clean", scan)
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=encoded(response()))])
    result = await compile_packet(inherited_synthesis, transport.guard)
    assert inherited_synthesis == before
    assert len(mock.calls) == 1
    assert secret not in mock.calls[0][1]["content"]
    assert result.claims[0].source_scope.packet_digest == hashlib.sha256(encoded(before).encode()).hexdigest()
