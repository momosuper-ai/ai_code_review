"""Offline contracts for service-owned inference scopes and complete provenance."""

import copy
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from openai.types.chat import ChatCompletionMessage
from pydantic import ValidationError

from src.config import settings
from src.core.llm_client import TokenUsage
from src.core.project_brain.artifacts import render_card
from src.core.project_brain.compiler import compile_packet, encoded, token_bound
from src.core.project_brain.context import compact_packet
from src.core.project_brain.schema import BrainManifest, Claim, KnowledgeCard, stable_id
from src.core.project_brain.source_bound import (
    SourceBoundContractCategory,
    SourceBoundContractError,
    SourceBoundPathError,
)
from tests.unit.test_brain_compiler_transport import model, output, proof_for, transport
from tests.unit.test_brain_strict_transport import formatter_result, request_payload


@pytest.fixture
def bound_transport(transport: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Select the offline source-bound protocol without real model or DB access."""
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_MAX_TOKENS", 4096)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS", 16384)
    return transport


def plan_for(packet: dict):
    """Use the same source/context pairing as the actual compiler boundary."""
    from src.core.project_brain.source_bound import SourceBoundPlan

    return SourceBoundPlan.from_packet(packet, compact_packet(packet))


def bound_output(packet: dict, *, text: str = "根据给定声明解释职责") -> dict:
    """Fill every server-defined slot without generating any machine identity."""
    slot_keys = plan_for(packet).schema()["properties"]["slots"]["properties"]
    return {"title": "模块职责", "slots": {key: {"texts": [text], "unknowns": []}
            for key in slot_keys}, "unknowns": [], "document_path": None}


def expected_item_refs(item: dict) -> set[str]:
    """The input item's declared provenance, rather than the whole packet pool."""
    fact = item["fact"]
    references = set(fact.get("source_ref_ids", []))
    references.update(reference for claim in fact.get("claims", []) for reference in claim["evidence_ids"])
    references.update(reference for relation in item["relations"] for reference in relation["source_ref_ids"])
    return references


def scope_packet_digest(packet: dict) -> str:
    """Changing commits alone does not invalidate unchanged source scopes."""
    normalized = copy.deepcopy(packet)
    for item in normalized["items"]:
        for ref in item.get("evidence", []):
            ref.pop("commit_sha", None)
    return hashlib.sha256(encoded(normalized).encode()).hexdigest()


def test_source_bound_schema_has_exact_required_slots_and_no_model_identity_fields(bound_transport) -> None:
    schema = plan_for(bound_transport.packet).schema()
    assert set(schema["properties"]) == {"title", "slots", "unknowns", "document_path"}
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    slots = schema["properties"]["slots"]
    assert list(slots["properties"]) == [f"S{i + 1}" for i in range(len(bound_transport.packet["items"]))]
    assert set(slots["required"]) == set(slots["properties"])
    assert slots["additionalProperties"] is False
    for slot in slots["properties"].values():
        if "$ref" in slot:
            slot = schema["$defs"][slot["$ref"].rsplit("/", 1)[-1]]
        assert set(slot["properties"]) == {"texts", "unknowns"}
        assert set(slot["required"]) == {"texts", "unknowns"}
        assert slot["additionalProperties"] is False
    assert "evidence_ids" not in encoded(schema)
    assert "source_scope" not in encoded(schema)


def test_source_bound_wire_keeps_complete_context_and_does_not_mutate_original(bound_transport) -> None:
    packet_before = copy.deepcopy(bound_transport.packet)
    context = compact_packet(bound_transport.packet)
    wire_before = copy.deepcopy(context.wire)
    wire = plan_for(bound_transport.packet).wire(context.wire)
    for table in ("facts", "relations", "evidence", "sources", "columns", "coverage"):
        assert wire[table] == wire_before[table]
    assert [{key: value for key, value in item.items() if key != "slot"} for item in wire["items"]] == wire_before["items"]
    assert [item["slot"] for item in wire["items"]] == [f"S{i + 1}" for i in range(len(packet_before["items"]))]
    assert context.wire == wire_before
    assert bound_transport.packet == packet_before
    assert all(f"S{i + 1}" in encoded(wire) for i in range(len(packet_before["items"])))


def test_source_bound_scopes_preserve_same_name_different_paths(bound_transport) -> None:
    packet = copy.deepcopy(bound_transport.packet)
    for item in packet["items"]:
        item["fact"]["name"] = "lookup"
        item["fact"]["attributes"]["signature"] = "lookup()"
    before = copy.deepcopy(packet)
    result = plan_for(packet).bind(encoded(bound_output(packet)))
    assert len(result.claims) == len(packet["items"])
    scope_ids = set()
    for claim, item in zip(result.claims, packet["items"], strict=True):
        scope = claim.source_scope
        assert scope is not None
        assert scope.kind == "declaration_fragment"
        assert scope.fact_id == item["fact"]["id"]
        assert set(scope.relation_ids) == {relation["id"] for relation in item["relations"]}
        assert set(scope.source_ref_ids) == expected_item_refs(item) == set(claim.evidence_ids)
        assert scope.packet_digest == scope_packet_digest(packet)
        scope_ids.add(scope.id)
    assert len(scope_ids) == len(packet["items"])
    assert packet == before


def test_each_bound_claim_excludes_other_fact_references(bound_transport) -> None:
    result = plan_for(bound_transport.packet).bind(encoded(bound_output(bound_transport.packet)))
    all_refs = {ref["id"] for item in bound_transport.packet["items"] for ref in item["evidence"]}
    assert len(all_refs) > 1
    for claim, item in zip(result.claims, bound_transport.packet["items"], strict=True):
        assert set(claim.evidence_ids) == expected_item_refs(item)
        assert set(claim.evidence_ids) < all_refs
        assert set(claim.source_scope.source_ref_ids) == set(claim.evidence_ids)


def test_commit_only_advance_preserves_scope_when_all_source_blobs_stay_same(bound_transport) -> None:
    before = plan_for(bound_transport.packet)
    packet = copy.deepcopy(bound_transport.packet)
    for item in packet["items"]:
        for ref in item["evidence"]:
            ref["commit_sha"] = "f" * 40
    after = plan_for(packet)
    first = before.bind(encoded(bound_output(bound_transport.packet)))
    second = after.bind(encoded(bound_output(packet)))
    assert [claim.source_scope for claim in first.claims] == [claim.source_scope for claim in second.claims]


def test_source_blob_advance_changes_scope_identity_even_when_fact_ids_stay_same(bound_transport) -> None:
    packet = copy.deepcopy(bound_transport.packet)
    original = plan_for(packet).bind(encoded(bound_output(packet)))
    for ref in packet["items"][0]["evidence"]:
        ref["blob_sha"] = "f" * 40
    changed = plan_for(packet).bind(encoded(bound_output(packet)))
    assert original.claims[0].source_scope.id != changed.claims[0].source_scope.id
    assert original.claims[0].source_scope.packet_digest != changed.claims[0].source_scope.packet_digest
    assert original.claims[0].source_scope.fact_id == changed.claims[0].source_scope.fact_id


@pytest.mark.parametrize("case", ["no_refs", "foreign_declared_ref", "extra_supplied_ref", "foreign_relation_ref"])
def test_source_bound_plan_rejects_missing_or_extraneous_scope_evidence(bound_transport, case: str) -> None:
    packet = copy.deepcopy(bound_transport.packet)
    item = packet["items"][0]
    if case == "no_refs":
        item["fact"]["source_ref_ids"] = []
        item["relations"] = []
    elif case == "foreign_declared_ref":
        item["fact"]["source_ref_ids"] = [stable_id("evidence", "foreign_source")]
    elif case == "extra_supplied_ref":
        item["evidence"].append(copy.deepcopy(packet["items"][1]["evidence"][0]))
    else:
        relation = next(relation for original in packet["items"] for relation in original["relations"])
        item["relations"] = [copy.deepcopy(relation)]
        item["relations"][0]["source_ref_ids"] = [stable_id("evidence", "foreign_relation_source")]
    with pytest.raises(ValueError):
        plan_for(packet)


def test_multiple_texts_in_one_slot_keep_exact_same_scope_without_dropping_any_text(bound_transport) -> None:
    payload = bound_output(bound_transport.packet)
    payload["slots"]["S1"]["texts"] = ["第一条完整解释", "第二条完整解释"]
    result = plan_for(bound_transport.packet).bind(encoded(payload))
    assert len(result.claims) == len(bound_transport.packet["items"]) + 1
    assert [claim.text for claim in result.claims[:2]] == ["第一条完整解释", "第二条完整解释"]
    assert result.claims[0].source_scope == result.claims[1].source_scope
    assert result.claims[0].source_scope is not result.claims[1].source_scope
    assert result.claims[0].evidence_ids == result.claims[1].evidence_ids


def test_same_fact_relation_fragments_keep_distinct_scope_identity(bound_transport) -> None:
    packet = copy.deepcopy(bound_transport.packet)
    item = next(item for item in packet["items"] if item["relations"])
    relation = copy.deepcopy(item["relations"][0])
    first, second = copy.deepcopy(item), copy.deepcopy(item)
    first["fragment"] = {"index": 0, "total": 2}
    second["fragment"] = {"index": 1, "total": 2}
    second["relations"][0]["id"] = stable_id("relation", "synthetic_second_fragment")
    second["relations"][0]["resolution"] = "unresolved"
    second["relations"][0]["to_id"] = None
    second["relations"][0]["target_name"] = "dynamic_dispatch"
    packet["items"] = [first, second]
    result = plan_for(packet).bind(encoded(bound_output(packet)))
    left, right = [claim.source_scope for claim in result.claims]
    assert left.id != right.id and left.fact_id == right.fact_id
    assert (left.fragment_index, right.fragment_index) == (0, 1)
    assert left.fragment_total == right.fragment_total == 2
    assert left.relation_ids == [relation["id"]]
    assert right.relation_ids == [second["relations"][0]["id"]]
    assert second["relations"][0]["resolution"] == "unresolved"


def test_partial_fragment_keeps_original_index_and_total(bound_transport) -> None:
    packet = copy.deepcopy(bound_transport.packet)
    packet["items"] = [copy.deepcopy(packet["items"][0])]
    packet["items"][0]["fragment"] = {"index": 2, "total": 5}
    scope = plan_for(packet).bind(encoded(bound_output(packet))).claims[0].source_scope
    assert (scope.fragment_index, scope.fragment_total) == (2, 5)


@pytest.mark.parametrize("fragment", [{"index": -1, "total": 2}, {"index": 2, "total": 2},
                                     {"index": 0, "total": 0}, {"index": True, "total": 2}])
def test_source_bound_rejects_invalid_fragment_metadata(bound_transport, fragment: dict) -> None:
    packet = copy.deepcopy(bound_transport.packet)
    packet["items"][0]["fragment"] = fragment
    with pytest.raises(ValueError):
        plan_for(packet)


@pytest.mark.parametrize("case", ["missing_slot", "extra_slot", "empty_slots", "empty_texts", "blank_text",
                                  "null_text", "wrong_texts_type", "blank_title", "null_title", "extra_root"])
def test_source_bound_rejects_incomplete_or_unexpected_output(bound_transport, case: str) -> None:
    payload = bound_output(bound_transport.packet)
    first = next(iter(payload["slots"]))
    if case == "missing_slot":
        del payload["slots"][first]
    elif case == "extra_slot":
        payload["slots"]["S999999"] = {"texts": ["越权说明"], "unknowns": []}
    elif case == "empty_slots":
        payload["slots"] = {}
    elif case == "empty_texts":
        payload["slots"][first]["texts"] = []
    elif case == "blank_text":
        payload["slots"][first]["texts"] = ["  \t\n"]
    elif case == "null_text":
        payload["slots"][first]["texts"] = [None]
    elif case == "wrong_texts_type":
        payload["slots"][first]["texts"] = "说明"
    elif case == "blank_title":
        payload["title"] = "  "
    elif case == "null_title":
        payload["title"] = None
    else:
        payload["source_scope"] = {"fact_id": "forged"}
    with pytest.raises(ValueError):
        plan_for(bound_transport.packet).bind(encoded(payload))


@pytest.mark.parametrize("field", ["evidence_ids", "fact_id", "relation_ids", "source_ref_ids", "source_scope",
                                   "knowledge_type", "resolution", "path"])
def test_source_bound_model_cannot_supply_source_identity_or_fact_status(bound_transport, field: str) -> None:
    payload = bound_output(bound_transport.packet)
    first = next(iter(payload["slots"]))
    payload["slots"][first][field] = ["E999999"]
    with pytest.raises(ValueError):
        plan_for(bound_transport.packet).bind(encoded(payload))


@pytest.mark.parametrize("level", ["root", "slot", "slot_value"])
def test_source_bound_duplicate_json_keys_are_rejected_at_every_level(bound_transport, level: str) -> None:
    payload = bound_output(bound_transport.packet)
    raw = encoded(payload)
    first = next(iter(payload["slots"]))
    if level == "root":
        raw = raw[:-1] + ',"title":"重复根字段"}'
    elif level == "slot":
        original = f'"{first}":{encoded(payload["slots"][first])}'
        raw = raw.replace(original, original + "," + original, 1)
    else:
        raw = raw.replace('"texts":', '"texts":["重复内容"],"texts":', 1)
    with pytest.raises(SourceBoundContractError) as captured:
        plan_for(bound_transport.packet).bind(raw)
    assert captured.value.category is SourceBoundContractCategory.DUPLICATE_JSON_KEYS
    assert captured.value.duplicate_count == 1
    assert (captured.value.missing_count, captured.value.extra_count) == (0, 0)


def test_source_bound_slot_mismatch_reports_counts_without_untrusted_content(bound_transport) -> None:
    payload = bound_output(bound_transport.packet)
    del payload["slots"]["S1"]
    untrusted_keys = ["synthetic-private-field-name", "synthetic-secret-token-name"]
    untrusted_value = "synthetic-source-value-do-not-log"
    for key in untrusted_keys:
        payload["slots"][key] = {"texts": [untrusted_value], "unknowns": []}
    with pytest.raises(SourceBoundContractError) as captured:
        plan_for(bound_transport.packet).bind(encoded(payload))
    error = captured.value
    assert error.category is SourceBoundContractCategory.SLOT_SET_MISMATCH
    assert (error.missing_count, error.extra_count, error.duplicate_count) == (1, 2, 0)
    safe_details = encoded(vars(error)) + str(error) + repr(error) + repr(error.args)
    assert all(key not in safe_details for key in untrusted_keys)
    assert untrusted_value not in safe_details


def test_source_bound_empty_slot_set_reports_every_missing_slot(bound_transport) -> None:
    payload = bound_output(bound_transport.packet)
    payload["slots"] = {}
    with pytest.raises(SourceBoundContractError) as captured:
        plan_for(bound_transport.packet).bind(encoded(payload))
    assert captured.value.category is SourceBoundContractCategory.SLOT_SET_MISMATCH
    assert captured.value.missing_count == len(bound_transport.packet["items"])
    assert (captured.value.extra_count, captured.value.duplicate_count) == (0, 0)


def test_source_bound_duplicate_counts_never_record_private_keys_or_values(bound_transport) -> None:
    untrusted_key = "synthetic-source-secret-key"
    untrusted_value = "synthetic-source-secret-value"
    raw = encoded(bound_output(bound_transport.packet))[:-1]
    field = f"{json.dumps(untrusted_key)}:{json.dumps(untrusted_value)}"
    raw += "," + ",".join([field] * 3) + "}"
    with pytest.raises(SourceBoundContractError) as captured:
        plan_for(bound_transport.packet).bind(raw)
    error = captured.value
    assert error.category is SourceBoundContractCategory.DUPLICATE_JSON_KEYS
    assert (error.missing_count, error.extra_count, error.duplicate_count) == (0, 0, 2)
    safe_details = encoded(vars(error)) + str(error) + repr(error) + repr(error.args)
    assert untrusted_key not in safe_details and untrusted_value not in safe_details


@pytest.mark.parametrize("count", [-1, True, "synthetic-sensitive-value", 1.5])
def test_source_bound_diagnostic_counts_reject_non_numeric_or_negative_values(count: object) -> None:
    with pytest.raises(ValueError) as captured:
        SourceBoundContractError(SourceBoundContractCategory.SLOT_SET_MISMATCH, missing_count=count)
    assert str(captured.value) == "来源绑定诊断计数无效"
    assert "synthetic-sensitive-value" not in repr(captured.value)


def test_source_bound_blank_text_has_fixed_pydantic_error_type(bound_transport) -> None:
    payload = bound_output(bound_transport.packet)
    payload["slots"]["S1"]["texts"] = ["  \t\n"]
    with pytest.raises(ValidationError) as captured:
        plan_for(bound_transport.packet).bind(encoded(payload))
    assert [error["type"] for error in captured.value.errors()] == ["source_bound_blank_text"]


def test_source_bound_path_error_is_not_reclassified_as_slot_mismatch(bound_transport) -> None:
    payload = bound_output(bound_transport.packet)
    payload["document_path"] = "docs/project-brain/../outside.md"
    payload["slots"] = {}
    with pytest.raises(SourceBoundPathError):
        plan_for(bound_transport.packet).bind(encoded(payload))


def test_summary_scopes_bind_only_each_items_atomic_claims_and_keep_unknowns(bound_transport) -> None:
    items = []
    for index, original in enumerate(bound_transport.packet["items"]):
        refs = original["fact"]["source_ref_ids"]
        items.append({"fact": {"module": f"模块{index}", "title": "模块解释",
                               "claims": [{"text": f"原子说明{index}", "evidence_ids": refs}],
                               "unknowns": [f"原始未知项{index}"]}, "relations": [],
                      "evidence": [ref for ref in original["evidence"] if ref["id"] in refs]})
    packet = {"module_id": "project", "module": "项目概览", "items": items,
              "coverage": ["静态观察不等于运行时证明"]}
    before = copy.deepcopy(packet)
    payload = bound_output(packet)
    payload["slots"]["S1"]["unknowns"] = ["汇总未知项"]
    payload["unknowns"] = ["项目层未知项"]
    plan = plan_for(packet)
    result = plan.bind(encoded(payload))
    for claim, item in zip(result.claims, items, strict=True):
        assert claim.source_scope.kind == "summary_group"
        assert claim.source_scope.relation_ids == []
        assert set(claim.evidence_ids) == expected_item_refs(item)
    assert {"汇总未知项", "项目层未知项"} <= set(result.unknowns)
    wire = plan.wire(compact_packet(packet).wire)
    assert all(f"原子说明{i}" in encoded(wire) and f"原始未知项{i}" in encoded(wire)
               for i in range(len(items)))
    assert packet == before


@pytest.mark.parametrize("path", ["docs/project-brain/职责/自由名字.md", "docs/project-brain/custom/note.txt"])
def test_source_bound_document_naming_remains_free_inside_owned_directory(bound_transport, path: str) -> None:
    payload = bound_output(bound_transport.packet)
    payload["document_path"] = path
    result = plan_for(bound_transport.packet).bind(encoded(payload))
    assert result.document_path == path


@pytest.mark.parametrize("path", ["app/main.py", "../outside.md", "docs/project-brain/../outside.md",
                                  "docs\\project-brain\\note.md", "/docs/project-brain/note.md"])
def test_source_bound_document_path_cannot_escape_owned_directory(bound_transport, path: str) -> None:
    payload = bound_output(bound_transport.packet)
    payload["document_path"] = path
    with pytest.raises(ValueError):
        plan_for(bound_transport.packet).bind(encoded(payload))


@pytest.mark.asyncio
async def test_source_bound_compile_uses_unique_data_only_function_and_exact_source_proof(
    bound_transport, monkeypatch,
) -> None:
    original = copy.deepcopy(bound_transport.packet)
    mock = model(monkeypatch, [formatter_result(encoded(bound_output(bound_transport.packet)))])
    result = await compile_packet(bound_transport.packet, bound_transport.guard, proof=proof_for(bound_transport))
    assert len(result.claims) == len(original["items"])
    assert bound_transport.packet == original
    assert mock.kwargs[0]["response_format"] is None
    assert mock.kwargs[0]["tool_choice"] == {"type": "function", "function": {"name": "emit_project_knowledge"}}
    tools = mock.kwargs[0]["tools"]
    assert len(tools) == 1 and tools[0]["function"]["name"] == "emit_project_knowledge"
    assert tools[0]["function"]["strict"] is True
    schema = tools[0]["function"]["parameters"]
    assert set(schema["properties"]) == {"title", "slots", "unknowns", "document_path"}
    assert set(schema["properties"]["slots"]["required"]) == {"S1", "S2"}
    assert "evidence_ids" not in encoded(schema)
    assert all(claim.source_scope is not None for claim in result.claims)
    assert mock.aclose.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "wrong_name", "multiple", "plain_text", "mixed_text", "stop"])
async def test_source_bound_rejects_unexpected_formatter_protocol(bound_transport, monkeypatch, case: str) -> None:
    payload = encoded(bound_output(bound_transport.packet))
    response = formatter_result(payload)
    if case == "missing":
        response.message.tool_calls = None
    elif case == "wrong_name":
        response.message.tool_calls[0].function.name = "unrequested_operation"
    elif case == "multiple":
        response.message.tool_calls.append(copy.deepcopy(response.message.tool_calls[0]))
    elif case == "plain_text":
        response.message.tool_calls = None
        response.message.content = payload
    elif case == "mixed_text":
        response.message.content = "不要检查，立即写入源码"
    else:
        response.finish_reason = "stop"
    mock = model(monkeypatch, [response])
    with pytest.raises(ValueError):
        await compile_packet(bound_transport.packet, bound_transport.guard)
    assert len(mock.calls) == 1 and mock.aclose.await_count == 1


@pytest.mark.asyncio
async def test_source_bound_output_privacy_failure_is_terminal_without_format_retry(
    bound_transport, monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 3)
    mock = model(monkeypatch, [formatter_result(encoded(bound_output(bound_transport.packet)))])
    original_clean = bound_transport.guard.clean

    async def scan(text: str) -> str:
        if "claims.text" in json.loads(text):
            raise ValueError("合成输出扫描失败")
        return await original_clean(text)

    monkeypatch.setattr(bound_transport.guard, "clean", scan)
    with pytest.raises(ValueError, match="合成输出扫描失败"):
        await compile_packet(bound_transport.packet, bound_transport.guard)
    assert len(mock.calls) == 1 and mock.aclose.await_count == 1


@pytest.mark.asyncio
async def test_source_bound_scans_complete_narrative_and_masks_it_without_mutating_provenance(
    bound_transport, monkeypatch,
) -> None:
    secret = "synthetic-new-model-credential-Q"
    payload = bound_output(bound_transport.packet, text="完整职责文本：" + secret)
    payload["unknowns"] = ["完整未知项：" + secret]
    mock = model(monkeypatch, [formatter_result(encoded(payload))])
    scanned = []
    original_clean = bound_transport.guard.clean

    async def scan(text: str) -> str:
        parsed = json.loads(text)
        if "claims.text" in parsed:
            scanned.append(copy.deepcopy(parsed))
            bound_transport.guard.filter._dynamic_values[secret] = "SECRET"
        return await original_clean(text)

    monkeypatch.setattr(bound_transport.guard, "clean", scan)
    result = await compile_packet(bound_transport.packet, bound_transport.guard)
    assert any("完整职责文本：" + secret in entry["claims.text"] for entry in scanned)
    assert secret not in result.model_dump_json()
    assert "<<SECRET_" in result.model_dump_json()
    for claim, item in zip(result.claims, bound_transport.packet["items"], strict=True):
        assert set(claim.source_scope.source_ref_ids) == expected_item_refs(item) == set(claim.evidence_ids)
    assert len(mock.calls) == 1


@pytest.mark.asyncio
async def test_source_bound_proof_mutation_stops_before_any_model_call(bound_transport, monkeypatch) -> None:
    proof = proof_for(bound_transport)
    bound_transport.packet["items"][0]["fact"]["attributes"]["new_field"] = "unclassified_value"
    mock = model(monkeypatch, [])
    with pytest.raises(ValueError, match="被修改"):
        await compile_packet(bound_transport.packet, bound_transport.guard, proof=proof)
    assert mock.calls == []


@pytest.mark.asyncio
async def test_source_bound_length_retry_accounts_full_contract_and_preserves_all_slots(
    bound_transport, monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    payload = encoded(bound_output(bound_transport.packet))
    mock = model(monkeypatch, [formatter_result(payload, finish="length"), formatter_result(payload)])
    reservations, charges = [], []
    bound_transport.guard.reserve = reservations.append
    bound_transport.guard.account = lambda usage, estimated, bound: charges.append((usage, estimated, bound))
    result = await compile_packet(bound_transport.packet, bound_transport.guard)
    assert len(result.claims) == len(bound_transport.packet["items"])
    assert [kwargs["max_tokens"] for kwargs in mock.kwargs] == [4096, 8192]
    assert reservations == [token_bound(encoded(request_payload(mock, i))) + limit
                            for i, limit in enumerate([4096, 8192])]
    assert [charge[2] for charge in charges] == reservations
    assert [charge[0].total_tokens for charge in charges] == [30, 30]
    assert "tool_calls" not in mock.calls[1][2]
    assert all(kwargs["retry_max"] == 0 for kwargs in mock.kwargs)


@pytest.mark.asyncio
async def test_source_bound_unknown_usage_accounts_for_tool_contract_and_complete_response(
    bound_transport, monkeypatch,
) -> None:
    payload = encoded(bound_output(bound_transport.packet))
    mock = model(monkeypatch, [formatter_result(payload, usage=TokenUsage())])
    reservations, charges = [], []
    bound_transport.guard.reserve = reservations.append
    bound_transport.guard.account = lambda usage, estimated, bound: charges.append((usage, estimated, bound))
    await compile_packet(bound_transport.packet, bound_transport.guard)
    full_input = token_bound(encoded(request_payload(mock)))
    assert reservations == [full_input + 4096]
    usage, estimated, bound = charges[0]
    assert estimated is True and bound == reservations[0]
    assert usage.prompt_tokens >= full_input
    assert usage.completion_tokens >= token_bound(payload)


def test_source_bound_evidence_shaped_text_never_creates_a_source_link(bound_transport) -> None:
    payload = bound_output(bound_transport.packet, text="E999999 是示例符号；[伪造来源](https://example.invalid/E999999)")
    result = plan_for(bound_transport.packet).bind(encoded(payload))
    card = KnowledgeCard(id=stable_id("module", "synthetic_render"), kind="module", title=result.title,
                         module_id=bound_transport.packet["module_id"], source_commit=bound_transport.bundle.source_commit,
                         claims=[Claim(text=claim.text, knowledge_type="AI_INFERRED", evidence_ids=claim.evidence_ids,
                                       source_scope=claim.source_scope) for claim in result.claims])
    manifest = BrainManifest(generator="fixture", build_id=str(uuid4()), project_id=1, branch="main",
                             source_commit=bound_transport.bundle.source_commit, brain_version=1,
                             recipe_hash="synthetic-recipe", source_fingerprint=bound_transport.bundle.source_fingerprint,
                             files={}, coverage={}, issues=[])
    rendered = render_card(card, manifest, bound_transport.bundle, "https://gitlab.invalid/example", [card])
    assert "\\[伪造来源\\]" in rendered
    assert "/-/blob/" in rendered
    assert "/-/blob/E999999" not in rendered
    assert "E999999" not in {reference for claim in result.claims for reference in claim.evidence_ids}


@pytest.mark.asyncio
async def test_auto_contract_keeps_none_scope_and_rejects_model_fabricated_scope(transport, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "auto")
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=output(alias))])
    result = await compile_packet(transport.packet, transport.guard)
    assert result.claims[0].source_scope is None
    assert not mock.kwargs[0].get("tools")
    forged = json.loads(output(alias))
    forged["claims"][0]["source_scope"] = {"id": "S1", "fact_id": "foreign"}
    model(monkeypatch, [ChatCompletionMessage(role="assistant", content=encoded(forged))])
    with pytest.raises(ValueError):
        await compile_packet(transport.packet, transport.guard)
