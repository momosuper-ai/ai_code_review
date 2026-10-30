"""A fresh output window prevents privacy continuation from regenerating knowledge."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.llm_client import ChatResult, TokenUsage
from src.core.project_brain.compiler import CompilationGuard, CompilationLimitError, compile_packet, encoded
from src.core.project_brain.exceptions import BudgetBusy, BudgetExhausted
from src.core.project_brain.privacy import PrivacyLedger, PrivacyScanDeferred
from src.core.project_brain.schema import stable_id
from src.core.project_brain.trusted_packet import checkpoint_digest
from src.utils.logger import logger
from tests.unit.test_brain_strict_transport import formatter_result
from tests.unit.test_brain_trusted_packet import checkpoint, proof  # noqa: F401


@pytest.fixture
def window(checkpoint: tuple[dict, dict, dict], tmp_path: Path,
           monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Exercise real packing, HMAC receipts and replay with synthetic clients."""
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    monkeypatch.setattr(settings, "PRIVACY_FILTER_LEVEL", "minimal")
    monkeypatch.setattr(settings, "PRIVACY_CUSTOM_PATTERNS", "")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_RECORDS_PER_PACKET", 1)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT", 8)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_CALL_TIMEOUT", 60)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_MAX_TOKENS", 4096)
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    packet, bundle, finalize = checkpoint
    original = bundle["source_refs"][0]
    excerpts = ["synthetic alpha OrionTestValue", "synthetic beta complete", "synthetic gamma complete"]
    refs = [{**copy.deepcopy(original), "id": stable_id("evidence", str(index))}
            for index in range(3)]
    ids = [ref["id"] for ref in refs]
    bundle["source_refs"] = refs
    bundle["facts"][0]["source_ref_ids"] = ids
    packet["items"][0]["fact"]["source_ref_ids"] = ids
    packet["items"][0]["evidence"] = [{**ref, "excerpt": excerpt, "excerpt_end": 2}
                                       for ref, excerpt in zip(refs, excerpts, strict=True)]
    finalize["bundle_hash"] = checkpoint_digest(bundle)
    finalize["packets"][0]["packet_digest"] = checkpoint_digest(packet)
    approved = proof(checkpoint)
    local_calls = []

    async def scan(messages: list[dict], **kwargs: object) -> ChatResult:
        data = json.loads(messages[-1]["content"])
        local_calls.append(data)
        items = [{"value": "OrionTestValue", "type": "SECRET"}] if "OrionTestValue" in encoded(data) else []
        return ChatResult(ChatCompletionMessage(role="assistant", content=encoded({"items": items})),
                          TokenUsage(100, 10), "stop")

    local = SimpleNamespace(chat=AsyncMock(side_effect=scan), aclose=AsyncMock())
    semantic = SimpleNamespace(chat=AsyncMock(), aclose=AsyncMock(), set_privacy_filter=MagicMock())
    constructors = []

    def client_factory(*args: object, **kwargs: object) -> object:
        constructors.append("semantic" if "model_name" in kwargs else "privacy")
        return semantic if "model_name" in kwargs else local

    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", client_factory)

    def fresh_guard() -> CompilationGuard:
        return CompilationGuard(lambda *args: None, lambda *args: None, PrivacyLedger(tmp_path / "private"))

    def response(text_count: int) -> ChatResult:
        return formatter_result(encoded({"title": "职责标题", "slots": {
            "S1": {"texts": [f"完整合成解释{index}" for index in range(text_count)], "unknowns": []}},
            "unknowns": [], "document_path": None}))

    return SimpleNamespace(packet=packet, approved=approved, local=local, semantic=semantic,
                           local_calls=local_calls, constructors=constructors, fresh_guard=fresh_guard,
                           response=response, private=tmp_path / "private")


async def prepare_input(window: SimpleNamespace) -> None:
    """A complete four-call input checkpoint ends before any semantic client."""
    first = window.fresh_guard()
    try:
        with pytest.raises(PrivacyScanDeferred):
            await compile_packet(window.packet, first, proof=window.approved)
        assert first.calls == 4
        assert window.semantic.chat.await_count == 0
        assert "semantic" not in window.constructors
    finally:
        await first.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("texts,output_calls", [(2, 5), (4, 7)])
async def test_input_four_scans_then_fresh_output_window_generates_once(window: SimpleNamespace, texts: int,
                                                                    output_calls: int) -> None:
    await prepare_input(window)
    window.semantic.chat.return_value = window.response(texts)
    next_delivery = window.fresh_guard()
    try:
        result = await compile_packet(window.packet, next_delivery, proof=window.approved)
        assert next_delivery.calls == output_calls
        assert window.local.chat.await_count == 4 + output_calls
        assert window.semantic.chat.await_count == 1
        assert len(result.claims) == texts
        assert all(set(claim.evidence_ids) == set(window.packet["items"][0]["fact"]["source_ref_ids"])
                   for claim in result.claims)
        assert "OrionTestValue" not in encoded(window.semantic.chat.await_args.args[0])
        assert "<<SECRET_" in encoded(window.semantic.chat.await_args.args[0])
        persisted = "".join(path.read_text(encoding="utf-8") for path in window.private.rglob("*.json"))
        assert "OrionTestValue" not in persisted
        assert "完整合成解释" not in persisted
    finally:
        await next_delivery.close()


@pytest.mark.asyncio
async def test_output_ninth_scan_is_terminal_without_regeneration_or_raw_response_checkpoint(window: SimpleNamespace, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_OUTPUT_MAX_CALLS_PER_UNIT", 8)
    await prepare_input(window)
    window.semantic.chat.return_value = window.response(6)
    next_delivery = window.fresh_guard()
    try:
        with pytest.raises(CompilationLimitError, match="语义输出.*privacy_window_exhausted"):
            await compile_packet(window.packet, next_delivery, proof=window.approved)
        assert next_delivery.calls == 8
        assert window.semantic.chat.await_count == 1
        assert window.local.chat.await_count == 12
        assert "完整合成解释" not in "".join(path.read_text(encoding="utf-8")
                                            for path in window.private.rglob("*.json"))
    finally:
        await next_delivery.close()


@pytest.mark.asyncio
async def test_complete_valid_output_can_require_more_than_input_call_limit(window: SimpleNamespace) -> None:
    """Replay the real pattern: valid output needs nine intact scalar scans."""
    await prepare_input(window)
    window.semantic.chat.return_value = window.response(6)
    guard = window.fresh_guard()
    try:
        result = await compile_packet(window.packet, guard, proof=window.approved)
        assert len(result.claims) == 6
        assert guard.calls == 9
        assert window.semantic.chat.await_count == 1
        assert window.local.chat.await_count == 13
        assert "完整合成解释" not in "".join(path.read_text(encoding="utf-8") for path in window.private.rglob("*.json"))
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_output_call_allowance_does_not_expand_input_window(window: SimpleNamespace, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT", 1)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_OUTPUT_MAX_CALLS_PER_UNIT", 64)
    guard = window.fresh_guard()
    try:
        with pytest.raises(PrivacyScanDeferred):
            await guard.clean(encoded({"source_excerpt": ["complete input alpha", "complete input beta"]}))
        assert guard.calls == 1
        assert window.semantic.chat.await_count == 0
        assert guard.after_response is False
    finally:
        await guard.close()


def test_output_scan_call_limit_is_versioned_in_recipe(monkeypatch):
    from src.core.project_brain.lifecycle import recipe_hash

    original = recipe_hash()
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_OUTPUT_MAX_CALLS_PER_UNIT",
                        settings.PROJECT_BRAIN_PRIVACY_OUTPUT_MAX_CALLS_PER_UNIT - 1)
    assert recipe_hash() != original


@pytest.mark.asyncio
async def test_output_work_window_exhaustion_is_terminal_without_new_semantic_request(window: SimpleNamespace,
                                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    await prepare_input(window)
    window.semantic.chat.return_value = window.response(2)
    next_delivery = window.fresh_guard()
    monkeypatch.setattr(next_delivery, "remaining_seconds", lambda: 0 if window.semantic.chat.await_count else 480)
    try:
        with pytest.raises(CompilationLimitError, match="语义输出.*privacy_window_exhausted"):
            await compile_packet(window.packet, next_delivery, proof=window.approved)
        assert next_delivery.calls == 0
        assert window.semantic.chat.await_count == 1
        assert window.local.chat.await_count == 4
    finally:
        await next_delivery.close()


@pytest.mark.asyncio
async def test_rule_only_input_needs_no_extra_delivery(window: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "rules_only")
    window.semantic.chat.return_value = window.response(2)
    guard = window.fresh_guard()
    try:
        result = await compile_packet(window.packet, guard, proof=window.approved)
        assert result.claims
        assert guard.calls == 0
        assert window.local.chat.await_count == 0
        assert window.semantic.chat.await_count == 1
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_untrusted_packet_input_also_finishes_before_semantic_generation(window: SimpleNamespace) -> None:
    window.semantic.chat.return_value = window.response(2)
    deferred_deliveries = 0
    for _ in range(20):
        guard = window.fresh_guard()
        try:
            try:
                result = await compile_packet(window.packet, guard)
            except PrivacyScanDeferred:
                deferred_deliveries += 1
                assert guard.calls <= 8
                assert window.semantic.chat.await_count == 0
                continue
            assert result.claims
            assert guard.calls <= 8
            break
        finally:
            await guard.close()
    else:
        pytest.fail("Complete synthetic input never reached its durable receipt")
    assert deferred_deliveries > 0
    assert window.semantic.chat.await_count == 1


@pytest.mark.asyncio
async def test_necessary_retry_reasoning_cannot_defer_and_reset_format_limit(window: SimpleNamespace,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    await prepare_input(window)
    invalid = formatter_result("{}")
    invalid.message.reasoning_content = "完整必要合成推理 OrionTestValue"
    window.semantic.chat.return_value = invalid
    next_delivery = window.fresh_guard()
    monkeypatch.setattr(next_delivery, "remaining_seconds", lambda: 0 if window.semantic.chat.await_count else 480)
    try:
        with pytest.raises(CompilationLimitError, match="必要字段.*privacy_window_exhausted"):
            await compile_packet(window.packet, next_delivery, proof=window.approved)
        assert window.semantic.chat.await_count == 1
        assert window.local.chat.await_count == 4
    finally:
        await next_delivery.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["extra_slot", "blank_text"])
async def test_source_bound_diagnostic_categories_never_log_slot_names_or_text(window: SimpleNamespace,
                                                                            monkeypatch: pytest.MonkeyPatch,
                                                                            case: str) -> None:
    await prepare_input(window)
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 0)
    warnings = []
    monkeypatch.setattr(logger, "warning", lambda template, *args: warnings.append(template % args))
    payload = {"title": "合成标题", "slots": {"S1": {"texts": ["完整解释"], "unknowns": []}},
               "unknowns": [], "document_path": None}
    if case == "extra_slot":
        payload["slots"]["synthetic-private-slot-name"] = {"texts": ["synthetic-private-response"], "unknowns": []}
    else:
        payload["slots"]["S1"]["texts"] = ["   "]
    window.semantic.chat.return_value = formatter_result(encoded(payload))
    next_delivery = window.fresh_guard()
    try:
        with pytest.raises(ValueError, match="schema_mismatch"):
            await compile_packet(window.packet, next_delivery, proof=window.approved)
        rendered = "\n".join(warnings)
        if case == "extra_slot":
            assert "source_bound_error=slot_set_mismatch missing_count=0 extra_count=1 duplicate_count=0" in rendered
        else:
            assert "schema_error_type=source_bound_blank_text schema_loc=slots.unknown_field.texts" in rendered
        assert "synthetic-private-slot-name" not in rendered
        assert "synthetic-private-response" not in rendered
        assert "S1" not in rendered
        assert window.semantic.chat.await_count == 1
    finally:
        await next_delivery.close()


@pytest.mark.asyncio
async def test_completed_six_call_reasoning_exhausts_retry_gate_without_reset(window: SimpleNamespace) -> None:
    await prepare_input(window)
    invalid = formatter_result("{}")
    invalid.message.reasoning_content = [f"合成必要推理{index}" for index in range(5)]
    window.semantic.chat.return_value = invalid
    next_delivery = window.fresh_guard()
    try:
        with pytest.raises(CompilationLimitError, match="semantic_retry_window_exhausted"):
            await compile_packet(window.packet, next_delivery, proof=window.approved)
        assert next_delivery.calls == 6
        assert window.semantic.chat.await_count == 1
        assert next_delivery.after_response is False
    finally:
        await next_delivery.close()


@pytest.mark.asyncio
async def test_elapsed_window_after_invalid_response_cannot_defer_format_retry(window: SimpleNamespace,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    await prepare_input(window)
    window.semantic.chat.return_value = formatter_result("{}")
    next_delivery = window.fresh_guard()
    monkeypatch.setattr(next_delivery, "remaining_seconds", lambda: 119 if window.semantic.chat.await_count else 480)
    try:
        with pytest.raises(CompilationLimitError, match="semantic_retry_window_exhausted"):
            await compile_packet(window.packet, next_delivery, proof=window.approved)
        assert window.semantic.chat.await_count == 1
    finally:
        await next_delivery.close()


@pytest.mark.asyncio
async def test_output_budget_busy_waits_in_memory_and_generates_once(window: SimpleNamespace,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    await prepare_input(window)
    window.semantic.chat.return_value = window.response(2)
    next_delivery = window.fresh_guard()
    busy = [2]

    def reserve(amount: int) -> None:
        if amount and next_delivery.after_response and busy[0]:
            busy[0] -= 1
            raise BudgetBusy()

    sleep = AsyncMock()
    monkeypatch.setattr(next_delivery, "reserve", reserve)
    monkeypatch.setattr("src.core.project_brain.compiler.asyncio.sleep", sleep)
    try:
        result = await compile_packet(window.packet, next_delivery, proof=window.approved)
        assert result.claims
        assert sleep.await_count == 2
        assert window.semantic.chat.await_count == 1
        assert next_delivery.calls == 5
        assert next_delivery.after_response is False
    finally:
        await next_delivery.close()


@pytest.mark.asyncio
async def test_next_semantic_retry_budget_busy_keeps_exact_attempt_context(window: SimpleNamespace,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    await prepare_input(window)
    window.semantic.chat.side_effect = [formatter_result("{}"), window.response(2)]
    next_delivery = window.fresh_guard()
    busy = [2]

    def reserve(amount: int) -> None:
        if amount and window.semantic.chat.await_count == 1 and busy[0]:
            busy[0] -= 1
            raise BudgetBusy()

    sleep = AsyncMock()
    monkeypatch.setattr(next_delivery, "reserve", reserve)
    monkeypatch.setattr("src.core.project_brain.compiler.asyncio.sleep", sleep)
    try:
        result = await compile_packet(window.packet, next_delivery, proof=window.approved)
        assert result.claims
        assert sleep.await_count == 2
        assert window.semantic.chat.await_count == 2
        retry = window.semantic.chat.await_args_list[1].args[0]
        assert len(retry) == 4
        assert retry[2] == {"role": "assistant", "content": "{}"}
        assert "schema_mismatch" in retry[3]["content"]
        assert next_delivery.calls == 5
    finally:
        await next_delivery.close()


@pytest.mark.asyncio
async def test_retry_reasoning_budget_busy_waits_without_dropping_required_field(window: SimpleNamespace,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    await prepare_input(window)
    invalid = formatter_result("{}")
    invalid.message.reasoning_content = "完整合成必要推理"
    window.semantic.chat.side_effect = [invalid, window.response(2)]
    next_delivery = window.fresh_guard()
    busy = [1]

    def reserve(amount: int) -> None:
        if amount and window.semantic.chat.await_count == 1 and busy[0]:
            busy[0] -= 1
            raise BudgetBusy()

    sleep = AsyncMock()
    monkeypatch.setattr(next_delivery, "reserve", reserve)
    monkeypatch.setattr("src.core.project_brain.compiler.asyncio.sleep", sleep)
    try:
        await compile_packet(window.packet, next_delivery, proof=window.approved)
        assert sleep.await_count == 1
        assert window.semantic.chat.await_count == 2
        retry = window.semantic.chat.await_args_list[1].args[0]
        assert retry[2]["reasoning_content"] == "完整合成必要推理"
        assert next_delivery.calls == 7
    finally:
        await next_delivery.close()


@pytest.mark.asyncio
async def test_endless_output_budget_busy_stops_at_work_window_without_regeneration(window: SimpleNamespace,
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    await prepare_input(window)
    window.semantic.chat.return_value = window.response(2)
    next_delivery = window.fresh_guard()
    remaining = [61.0]

    def reserve(amount: int) -> None:
        if amount and next_delivery.after_response:
            raise BudgetBusy()

    async def sleep(seconds: float) -> None:
        remaining[0] -= seconds

    monkeypatch.setattr(next_delivery, "reserve", reserve)
    monkeypatch.setattr(next_delivery, "remaining_seconds", lambda: remaining[0] if window.semantic.chat.await_count else 480)
    monkeypatch.setattr("src.core.project_brain.compiler.asyncio.sleep", sleep)
    try:
        with pytest.raises(CompilationLimitError, match="budget_wait_window_exhausted"):
            await compile_packet(window.packet, next_delivery, proof=window.approved)
        assert remaining[0] == 60
        assert window.semantic.chat.await_count == 1
        assert next_delivery.calls == 0
        assert window.local.chat.await_count == 4
    finally:
        await next_delivery.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", [True, False])
async def test_budget_busy_before_any_semantic_response_keeps_persistent_wait_contract(window: SimpleNamespace,
                                                                                   monkeypatch: pytest.MonkeyPatch,
                                                                                   prepared: bool) -> None:
    if prepared:
        await prepare_input(window)
    next_delivery = window.fresh_guard()

    def reserve(amount: int) -> None:
        if amount:
            raise BudgetBusy()

    sleep = AsyncMock()
    monkeypatch.setattr(next_delivery, "reserve", reserve)
    monkeypatch.setattr("src.core.project_brain.compiler.asyncio.sleep", sleep)
    try:
        with pytest.raises(BudgetBusy):
            await compile_packet(window.packet, next_delivery, proof=window.approved)
        assert sleep.await_count == 0
        assert window.semantic.chat.await_count == 0
        assert next_delivery.calls == 0
        assert next_delivery.after_response is False
    finally:
        await next_delivery.close()


@pytest.mark.asyncio
async def test_true_output_budget_exhaustion_is_not_waited_or_released(window: SimpleNamespace,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    await prepare_input(window)
    window.semantic.chat.return_value = window.response(2)
    next_delivery = window.fresh_guard()

    def reserve(amount: int) -> None:
        if amount and next_delivery.after_response:
            raise BudgetExhausted("synthetic known exhausted budget")

    sleep = AsyncMock()
    monkeypatch.setattr(next_delivery, "reserve", reserve)
    monkeypatch.setattr("src.core.project_brain.compiler.asyncio.sleep", sleep)
    try:
        with pytest.raises(BudgetExhausted):
            await compile_packet(window.packet, next_delivery, proof=window.approved)
        assert sleep.await_count == 0
        assert window.semantic.chat.await_count == 1
        assert next_delivery.calls == 0
    finally:
        await next_delivery.close()
