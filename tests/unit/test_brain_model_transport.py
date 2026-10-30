"""Model accounting crosses the real privacy and semantic transport seam."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.llm_client import ChatResult, TokenUsage
from src.core.project_brain.compiler import CompilationGuard, compile_packet
from tests.unit.test_brain_compiler_transport import transport  # noqa: F401


class Meter:
    def __init__(self):
        self.reservations, self.settlements = [], []

    def reserve(self, call_id, bound, spec):
        self.reservations.append((call_id, bound, spec))
        return call_id

    def settle(self, call_id, usage, estimated, bound, response_model):
        self.settlements.append((call_id, usage, estimated, bound, response_model))


@pytest.mark.asyncio
async def test_privacy_transport_retains_role_model_and_real_usage(monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    monkeypatch.setattr(settings, "VLLM_MODEL_NAME", "privacy-fixture")
    meter = Meter()
    guard = CompilationGuard(lambda *args: pytest.fail("metered usage must not double-account"),
                             lambda *args: None, call_meter=meter)
    guard.local = SimpleNamespace(chat=AsyncMock(return_value=ChatResult(
        ChatCompletionMessage(role="assistant", content='{"items":[]}'), TokenUsage(31, 4))), aclose=AsyncMock())
    await guard.clean('{"source_excerpt":["def run(): return 1"]}')
    assert len(meter.reservations) == len(meter.settlements) == 1
    call_id, bound, spec = meter.reservations[0]
    assert spec.role == "privacy" and spec.requested_model == "privacy-fixture"
    # The unknown envelope key is scanned too; its mixed role must not be
    # relabeled to bypass classification as a trusted excerpt-only packet.
    assert spec.purpose == "mixed"
    assert meter.settlements[0][:4] == (call_id, TokenUsage(31, 4), False, bound)
    await guard.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning_field", ["reasoning", "reasoning_content", "thinking"])
async def test_privacy_missing_usage_counts_complete_reasoning(monkeypatch, reasoning_field):
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    meter = Meter()
    guard = CompilationGuard(lambda *args: pytest.fail("metered usage must not double-account"),
                             lambda *args: None, call_meter=meter)
    reasoning = "完整合成扫描推理。" * 100
    message = ChatCompletionMessage(role="assistant", content='{"items":[]}', **{reasoning_field: reasoning})
    guard.local = SimpleNamespace(chat=AsyncMock(return_value=ChatResult(message, TokenUsage())), aclose=AsyncMock())
    await guard.clean('{"source_excerpt":["def run(): return 1"]}')
    assert len(meter.settlements) == 1
    _, usage, estimated, _, _ = meter.settlements[0]
    assert estimated
    # Missing supplier usage must not release the reasoning output as if only
    # the short JSON answer had consumed the reservation. Byte accounting is
    # conservative and includes the complete response without storing it.
    assert usage.completion_tokens >= len(reasoning.encode("utf-8"))
    assert usage.prompt_tokens > 0
    await guard.close()


@pytest.mark.asyncio
async def test_semantic_format_retry_has_separate_accounting_without_body_storage(transport, monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", False)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "auto")
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    meter = Meter()
    guard = CompilationGuard(lambda *args: pytest.fail("metered usage must not double-account"),
                             lambda *args: None, call_meter=meter)
    evidence = transport.packet["items"][0]["evidence"][0]["id"]
    responses = [ChatResult(ChatCompletionMessage(role="assistant", content="{broken"), TokenUsage(17, 3)),
                 ChatResult(ChatCompletionMessage(role="assistant", content=
                    '{"title":"职责","claims":[{"text":"职责解释","evidence_ids":["' + evidence +
                    '"]}],"unknowns":[],"document_path":null}'), TokenUsage(19, 7))]
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", lambda **kwargs:
                        SimpleNamespace(chat=AsyncMock(side_effect=responses), set_privacy_filter=lambda _: None,
                                        aclose=AsyncMock()))
    result = await compile_packet(transport.packet, guard)
    assert result.claims
    assert len(meter.reservations) == len(meter.settlements) == 2
    assert [entry[2].attempt for entry in meter.reservations] == [1, 2]
    assert [entry[2].purpose for entry in meter.reservations] == ["initial", "format_retry"]
    assert all(entry[2].role == "semantic" for entry in meter.reservations)
    assert len({entry[0] for entry in meter.reservations}) == 2
    assert sum(entry[1].total_tokens for entry in meter.settlements) == 46
    assert all(set(vars(entry[2])) == {"role", "requested_model", "purpose", "attempt", "transport_group", "transport_attempt"}
               for entry in meter.reservations)
