"""Summary classifier optimization must never weaken source authentication."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.llm_client import ChatResult, TokenUsage
from src.core.project_brain.compiler import CompilationGuard, compile_packet, encoded
from src.core.project_brain.privacy import PrivacyLedger, PrivacyScanDeferred
from src.core.project_brain.schema import Compilation
from src.core.project_brain.trusted_packet import VerifiedPacket, VerifiedSummaryPacket, checkpoint_digest
from tests.unit.test_brain_hierarchy_planner import next_summary_tasks, ready
from tests.unit.test_brain_compiler_transport import model, transport  # noqa: F401


@pytest.fixture
def summary_input(transport, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", False)
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 0)
    bundle, leaves = transport.bundle, []
    for index, fact in enumerate(bundle.facts):
        compilation = Compilation(title=f"声明{index}", claims=[{
            "text": f"完整职责{index}", "evidence_ids": fact.source_ref_ids}], unknowns=[f"未知{index}"])
        leaves.append(SimpleNamespace(key=f"compile:{index}", kind="compile", payload={"module_id": fact.module_id},
            status="completed", input_hash=f"input{index}", result=compilation.model_dump()))
    task = next_summary_tasks(bundle, leaves)[0]
    unit = ready(task)
    unit.status, unit.result = "pending", None
    finalize = SimpleNamespace(kind="finalize", status="completed", result={
        "bundle_hash": checkpoint_digest(bundle.model_dump()), "source_commit": bundle.source_commit,
        "recipe_hash": "c" * 64})
    privacy = SimpleNamespace(kind="privacy", status="completed")
    return SimpleNamespace(bundle=bundle, unit=unit, units=[*leaves, unit, finalize, privacy],
                           finalize=finalize, privacy=privacy)


def certify(value, **overrides):
    return VerifiedSummaryPacket.from_checkpoints(value.bundle, value.unit, value.units,
        **{"source_commit": value.bundle.source_commit, "recipe_hash": "c" * 64, **overrides})


@pytest.mark.parametrize("fault", ["source", "recipe", "bundle", "privacy", "finalize", "missing", "duplicate"])
def test_proof_requires_complete_matching_checkpoints(summary_input, fault):
    value = summary_input
    if fault == "source": value.finalize.result["source_commit"] = "d" * 40
    elif fault == "recipe": value.finalize.result["recipe_hash"] = "d" * 64
    elif fault == "bundle": value.finalize.result["bundle_hash"] = "d" * 64
    elif fault == "privacy": value.privacy.status = "pending"
    elif fault == "finalize": value.finalize.status = "running"
    elif fault == "missing": value.units.remove(value.finalize)
    else: value.units.append(value.finalize)
    with pytest.raises(ValueError, match="事实隐私检查点"):
        certify(value)


@pytest.mark.parametrize("fault", ["child_hash", "selection", "evidence", "blob", "line", "claims", "scope", "bool", "float", "endpoint", "resolution", "extra"])
def test_source_mutation_cannot_issue_proof_even_with_recomputed_unit_hash(summary_input, fault):
    packet = summary_input.unit.payload
    item, meta = packet["items"][0], packet["synthesis"]
    child = meta["child_ids"][0]
    if fault == "child_hash": meta["child_hashes"][child] = "d" * 64
    elif fault == "selection": meta["selections"][child]["claim_end"] = 0
    elif fault == "evidence": item["evidence"] = []
    elif fault == "blob": item["evidence"][0]["blob_sha"] = "d" * 40
    elif fault == "line": item["evidence"][0]["line_start"] = 2
    elif fault == "claims": item["fact"]["claims"][0]["text"] = "untrusted replacement"
    elif fault == "scope": item["fact"]["claims"][0]["source_scope"] = {"kind": "declaration_fragment"}
    elif fault == "bool": meta["level"] = False
    elif fault == "float": meta["level"] = 0.0
    elif fault == "endpoint": item["relations"][0]["to_id"] = "symbol:" + "d" * 64
    elif fault == "resolution": item["relations"][0]["resolution"] = "candidate"
    else: packet["untrusted field"] = "untrusted value"
    summary_input.unit.input_hash = checkpoint_digest(packet)
    with pytest.raises(ValueError, match="来源契约"):
        certify(summary_input)


@pytest.mark.parametrize("source_state", [None, "pending", "failed"])
def test_schema_two_requires_complete_source_privacy_barrier(summary_input, source_state):
    # MAC/canonical identity authentication belongs to the Worker; this factory
    # independently requires the already authenticated schema2 source barrier.
    summary_input.bundle.source_identity = {"synthetic": True}
    summary_input.finalize.result["bundle_hash"] = checkpoint_digest(summary_input.bundle.model_dump())
    if source_state is not None:
        summary_input.units.append(SimpleNamespace(kind="source_privacy", status=source_state))
    with pytest.raises(ValueError, match="事实隐私检查点"):
        certify(summary_input)


@pytest.mark.asyncio
async def test_mutation_after_proof_stops_before_classification_or_reservation(summary_input):
    packet, proof = certify(summary_input)
    guard = SimpleNamespace(clean_summary=AsyncMock(), clean=AsyncMock(), reserve_model_call=AsyncMock())
    packet["items"][0]["fact"]["title"] = "changed after authentication"
    with pytest.raises(ValueError, match="被修改"):
        await compile_packet(packet, guard, summary_proof=proof)
    guard.clean_summary.assert_not_awaited()
    guard.clean.assert_not_awaited()
    guard.reserve_model_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_proof", ["summary_as_leaf", "leaf_as_summary", "flag_as_summary"])
async def test_summary_cannot_enter_excerpt_only_leaf_proof_branch(summary_input, wrong_proof):
    packet, summary_proof = certify(summary_input)
    guard = SimpleNamespace(clean_summary=AsyncMock(), clean=AsyncMock(), reserve_model_call=AsyncMock())
    args = ({"proof": summary_proof} if wrong_proof == "summary_as_leaf" else
            {"summary_proof": VerifiedPacket(checkpoint_digest(packet))} if wrong_proof == "leaf_as_summary" else
            {"summary_proof": SimpleNamespace(validate=lambda value: None)})
    with pytest.raises(ValueError, match="证明类型不匹配"):
        await compile_packet(packet, guard, **args)
    guard.clean_summary.assert_not_awaited()
    guard.clean.assert_not_awaited()
    guard.reserve_model_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_prose_classification_and_late_finding_keep_canonical_identity(summary_input):
    packet, proof = certify(summary_input)
    before = copy.deepcopy(packet)
    guard = CompilationGuard(lambda *args: None, lambda *args: None)
    guard.local = object()  # Only the scan boundary below is synthetic.
    seen = []
    secret = packet["items"][0]["fact"]["claims"][0]["text"]
    async def scan(records, scope, start):
        seen.extend(records)
        guard.filter._dynamic_values[secret] = "SECRET"
    guard._scan = scan
    safe = await guard.clean_summary(packet, proof)
    texts = {record["text"] for record in seen}
    assert secret in texts
    assert set(packet["synthesis"]["all_unknowns"]) <= texts
    assert set(packet["coverage"]) <= texts
    assert packet["items"][0]["evidence"][0]["path"] in texts
    assert secret not in encoded(safe)
    assert safe["synthesis"]["child_hashes"] == before["synthesis"]["child_hashes"]
    assert packet == before
    proof.validate(packet)


@pytest.mark.asyncio
async def test_full_original_policy_runs_even_for_machine_values_and_receipt_hit(summary_input, tmp_path):
    packet, proof = certify(summary_input)
    ledger = PrivacyLedger(tmp_path)
    guard = CompilationGuard(lambda *args: None, lambda *args: None, ledger)
    guard.persist_rule_findings = True
    seen = []
    apply = guard._apply_local_policy
    def policy(value, scope):
        seen.append(copy.deepcopy(value))
        apply(value, scope)
    guard._apply_local_policy = policy
    for _ in range(2):
        await guard.clean_summary(packet, proof)
    assert seen[0] == packet and seen[2] == packet
    assert packet["synthesis"]["child_hashes"] == seen[2]["synthesis"]["child_hashes"]


@pytest.mark.asyncio
async def test_semantic_wire_and_scope_still_bind_complete_canonical_input(summary_input, monkeypatch):
    packet, proof = certify(summary_input)
    before = copy.deepcopy(packet)
    guard = CompilationGuard(lambda *args: None, lambda *args: None)
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=encoded({
        "title": "模块概览", "slots": {"S1": {"texts": ["综合职责"], "unknowns": ["动态行为未知"]}},
        "unknowns": [], "document_path": None}))])
    result = await compile_packet(packet, guard, summary_proof=proof)
    assert len(mock.calls) == 1
    wire = json.loads(mock.calls[0][1]["content"])
    assert wire["inherited_limitations"]["count"] == len(packet["synthesis"]["all_unknowns"])
    scope = result.claims[0].source_scope
    assert scope.packet_digest == checkpoint_digest(packet)
    assert scope.source_ref_ids == sorted({r["id"] for item in packet["items"] for r in item["evidence"]})
    assert packet == before


@pytest.mark.asyncio
async def test_durable_input_scan_defers_without_implicit_semantic_generation(summary_input, tmp_path, monkeypatch):
    packet, proof = certify(summary_input)
    guard = CompilationGuard(lambda *args: None, lambda *args: None, PrivacyLedger(tmp_path))
    # A receipt may already exist, but spent input calls still require the
    # ordinary fresh output window; a proof does not waive that boundary.
    guard.calls = 1
    model_boundary = AsyncMock()
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", model_boundary)
    with pytest.raises(PrivacyScanDeferred):
        await compile_packet(packet, guard, summary_proof=proof)
    model_boundary.assert_not_called()


@pytest.mark.asyncio
async def test_same_role_receipts_survive_wrapper_change_restart_and_late_finding(summary_input, tmp_path):
    packet, proof = certify(summary_input)
    ledger = PrivacyLedger(tmp_path)
    sent = []
    async def local_chat(messages, **kwargs):
        sent.append(json.loads(messages[1]["content"]))
        return ChatResult(ChatCompletionMessage(role="assistant", content='{"items":[]}'), TokenUsage(20, 10), "stop")
    local = SimpleNamespace(chat=local_chat)
    def guard_for(current):
        guard = CompilationGuard(lambda *args: None, lambda *args: None, current)
        guard.local = local
        return guard
    first = guard_for(ledger)
    claim = packet["items"][0]["fact"]["claims"][0]["text"]
    await first.clean(encoded({"claims.text": [claim]}))
    prior = len(sent)
    await first.clean_summary(packet, proof)
    # The identical complete text in the identical semantic role was already
    # classified as output; a summary wrapper cannot force it to be paid again.
    assert all(claim not in value.get("claims.text", []) for value in sent[prior:])
    count = len(sent)
    restarted = guard_for(PrivacyLedger(tmp_path, create=False))
    await restarted.clean_summary(packet, proof)
    assert len(sent) == count and restarted.calls == 0 and restarted.cache_hits > 0
    restarted.ledger.remember_policy([{"value": claim, "type": "SECRET"}],
        [{"field": "claims.text", "text": claim}], kind="deterministic_rule", scope=ledger.digest("later"))
    safe = await restarted.clean_summary(packet, proof)
    assert len(sent) == count  # Sticky policy still applies on a receipt hit.
    assert claim not in encoded(safe)
    proof.validate(packet)
