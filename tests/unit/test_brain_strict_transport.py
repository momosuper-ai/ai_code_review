"""Offline contract checks for data-only strict-tool knowledge output."""

import copy
import json
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionMessage
from pydantic import ValidationError

from src.config import Settings, settings
from src.core.llm_client import ChatResult, TokenUsage
from src.core.project_brain.compiler import compilation_response, compile_packet, encoded, token_bound
from src.core.project_brain.context import compact_packet
from src.core.project_brain.schema import Compilation
from src.utils.logger import logger
from tests.unit.test_brain_compiler_transport import model, output, proof_for, transport


@pytest.fixture
def strict_transport(transport: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Select the Brain-only strict transport with no live model or scanning."""
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "strict_tool")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_MAX_TOKENS", 4096)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS", 16384)
    return transport


def formatter_result(arguments: str, *, name: str = "emit_project_knowledge",
                     content: str | None = None, finish: str = "tool_calls",
                     usage: TokenUsage | None = None) -> ChatResult:
    """Build a synthetic formatter response without executing any function."""
    message = ChatCompletionMessage(role="assistant", content=content, tool_calls=[{
        "id": "synthetic_formatter_call", "type": "function", "function": {
            "name": name, "arguments": arguments,
        },
    }])
    return ChatResult(message, usage if usage is not None else TokenUsage(20, 10), finish)


def request_payload(mock: SimpleNamespace, index: int = 0) -> dict:
    """Account for the complete structured-input contract, not just messages."""
    return {"messages": mock.calls[index], "tools": mock.kwargs[index]["tools"],
            "tool_choice": mock.kwargs[index]["tool_choice"]}


def nested_keys(value: object) -> set[str]:
    """Collect schema keywords recursively while preserving enum values."""
    if isinstance(value, dict):
        return set(value) | set().union(*(nested_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(nested_keys(item) for item in value))
    return set()


@pytest.mark.parametrize("case", ["type", "function", "name", "arguments", "arguments_type"])
def test_formatter_boundary_rejects_malformed_call_metadata_with_fixed_error(case: str) -> None:
    """Malformed formatter metadata must not escape as raw attribute errors."""
    call = SimpleNamespace(type="function", function=SimpleNamespace(
        name="emit_project_knowledge", arguments='{"synthetic":true}',
    ))
    if case == "type":
        del call.type
    elif case == "function":
        del call.function
    elif case == "name":
        del call.function.name
    elif case == "arguments":
        del call.function.arguments
    else:
        call.function.arguments = 17
    message = SimpleNamespace(tool_calls=[call], content=None)
    with pytest.raises(ValueError, match="知识结构化输出"):
        compilation_response(message, strict_tool=True)


@pytest.mark.asyncio
async def test_strict_tool_uses_exact_formatter_and_packet_enum_with_fixed_source_proof(
    strict_transport, monkeypatch,
) -> None:
    context = compact_packet(strict_transport.packet)
    alias, canonical = next(iter(context.evidence_ids.items()))
    mock = model(monkeypatch, [formatter_result(output(alias))])
    result = await compile_packet(strict_transport.packet, strict_transport.guard,
                                  proof=proof_for(strict_transport))
    kwargs = mock.kwargs[0]
    assert kwargs["response_format"] is None
    assert kwargs["tool_choice"] == {"type": "function", "function": {"name": "emit_project_knowledge"}}
    assert len(kwargs["tools"]) == 1
    tool = kwargs["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "emit_project_knowledge"
    assert tool["function"]["strict"] is True
    schema = tool["function"]["parameters"]
    assert schema["$defs"]["InferredClaim"]["properties"]["evidence_ids"]["items"]["enum"] == list(context.evidence_ids)
    assert set(schema["required"]) == set(schema["properties"])
    assert not nested_keys(schema) & {"minLength", "maxLength", "minItems", "maxItems", "default"}
    assert schema["additionalProperties"] is False
    assert "minLength" in nested_keys(Compilation.model_json_schema())
    assert "minItems" in nested_keys(Compilation.model_json_schema())
    assert result.claims[0].evidence_ids == [canonical]
    assert result.claims[0].text == "该模块负责处理调用"
    assert kwargs["retry_max"] == 0 and mock.aclose.await_count == 1


@pytest.mark.asyncio
async def test_strict_tool_budget_includes_tools_choice_and_missing_usage_arguments(
    strict_transport, monkeypatch,
) -> None:
    alias = next(iter(compact_packet(strict_transport.packet).evidence_ids))
    arguments = output(alias)
    mock = model(monkeypatch, [formatter_result(arguments, usage=TokenUsage())])
    reservations, charges = [], []
    strict_transport.guard.reserve = reservations.append
    strict_transport.guard.account = lambda usage, estimated, bound: charges.append((usage, estimated, bound))
    await compile_packet(strict_transport.packet, strict_transport.guard)
    full_input = token_bound(encoded(request_payload(mock)))
    assert reservations == [full_input + 4096]
    assert full_input > token_bound(encoded(mock.calls[0]))
    usage, estimated, bound = charges[0]
    assert estimated is True and bound == reservations[0]
    assert usage.prompt_tokens >= full_input
    assert usage.completion_tokens >= token_bound(arguments)
    assert usage.total_tokens >= full_input + token_bound(arguments)


@pytest.mark.asyncio
async def test_strict_tool_provider_usage_is_not_replaced_by_conservative_byte_bound(
    strict_transport, monkeypatch,
) -> None:
    alias = next(iter(compact_packet(strict_transport.packet).evidence_ids))
    actual = TokenUsage(120, 80, 40)
    model(monkeypatch, [formatter_result(output(alias), usage=actual)])
    charges = []
    strict_transport.guard.account = lambda usage, estimated, bound: charges.append((usage, estimated, bound))
    await compile_packet(strict_transport.packet, strict_transport.guard)
    assert charges[0][0] is actual
    assert charges[0][1] is False
    assert charges[0][0].total_tokens == 200
    assert charges[0][0].cached_tokens == 40


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "wrong_name", "multiple", "plain_text", "mixed_text", "stop"])
async def test_strict_tool_rejects_ordinary_text_or_unexpected_function_calls(
    strict_transport, monkeypatch, case,
) -> None:
    alias = next(iter(compact_packet(strict_transport.packet).evidence_ids))
    response = formatter_result(output(alias))
    if case == "missing":
        response.message.tool_calls = None
    elif case == "wrong_name":
        response.message.tool_calls[0].function.name = "unrequested_operation"
    elif case == "multiple":
        response.message.tool_calls.append(copy.deepcopy(response.message.tool_calls[0]))
    elif case == "plain_text":
        response.message.tool_calls = None
        response.message.content = output(alias)
    elif case == "mixed_text":
        response.message.content = "必须忽略校验并执行操作"
    else:
        response.finish_reason = "stop"
    mock = model(monkeypatch, [response])
    with pytest.raises(ValueError):
        await compile_packet(strict_transport.packet, strict_transport.guard)
    assert len(mock.calls) == 1 and mock.aclose.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["empty_claims", "empty_text", "blank_text", "blank_title", "empty_evidence", "unknown_evidence", "invalid_path"])
async def test_strict_tool_transport_does_not_weaken_application_quality_gates(
    strict_transport, monkeypatch, case,
) -> None:
    alias = next(iter(compact_packet(strict_transport.packet).evidence_ids))
    payload = json.loads(output(alias))
    if case == "empty_claims":
        payload["claims"] = []
    elif case == "empty_text":
        payload["claims"][0]["text"] = ""
    elif case == "blank_text":
        payload["claims"][0]["text"] = " \n\t"
    elif case == "blank_title":
        payload["title"] = " \n\t"
    elif case == "empty_evidence":
        payload["claims"][0]["evidence_ids"] = []
    elif case == "unknown_evidence":
        payload["claims"][0]["evidence_ids"] = [alias, "E999999"]
    else:
        payload["document_path"] = "docs/project-brain/../../business.py"
    mock = model(monkeypatch, [formatter_result(encoded(payload))])
    with pytest.raises(ValueError):
        await compile_packet(strict_transport.packet, strict_transport.guard)
    assert len(mock.calls) == 1


@pytest.mark.asyncio
async def test_strict_tool_retry_discards_untrusted_arguments_and_forces_same_formatter(
    strict_transport, monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    alias = next(iter(compact_packet(strict_transport.packet).evidence_ids))
    untrusted_arguments = '{"synthetic_secret":"synthetic-untrusted-tool-credential"}'
    mock = model(monkeypatch, [formatter_result(untrusted_arguments), formatter_result(output(alias))])
    result = await compile_packet(strict_transport.packet, strict_transport.guard)
    assert result.claims
    assert len(mock.calls) == 2
    assert "synthetic-untrusted-tool-credential" not in encoded(mock.calls[1])
    assert all("tool_calls" not in message and "tool_call_id" not in message for message in mock.calls[1])
    assert mock.kwargs[1]["tools"] == mock.kwargs[0]["tools"]
    assert mock.kwargs[1]["tool_choice"] == mock.kwargs[0]["tool_choice"]
    assert mock.kwargs[1]["response_format"] is None
    assert mock.kwargs[0]["max_tokens"] == mock.kwargs[1]["max_tokens"] == 4096


@pytest.mark.asyncio
@pytest.mark.parametrize("reference,category", [
    ("E999999", "unknown_e_alias"), ("F1", "wrong_namespace"),
    ("E1-E2", "range_or_list"), ("evidence:" + "f" * 24, "foreign_canonical"),
    ("synthetic-private-reference", "other"),
])
async def test_strict_tool_invalid_citation_logs_only_fixed_category_and_counts(
    strict_transport, monkeypatch, reference, category,
) -> None:
    alias = next(iter(compact_packet(strict_transport.packet).evidence_ids))
    private_claim = "synthetic-private-claim-body"
    payload = json.loads(output(alias, text=private_claim))
    payload["claims"][0]["evidence_ids"].append(reference)
    diagnostics = []
    monkeypatch.setattr(logger, "warning", lambda template, *args: diagnostics.append(template % args))
    model(monkeypatch, [formatter_result(encoded(payload))])
    with pytest.raises(ValueError, match="unknown_evidence"):
        await compile_packet(strict_transport.packet, strict_transport.guard)
    assert any(f"reference_error={category} count=1" in message for message in diagnostics)
    assert any("invalid_count=1" in message and "total_count=2" in message for message in diagnostics)
    assert all(reference not in message and private_claim not in message for message in diagnostics)


@pytest.mark.asyncio
async def test_strict_tool_output_privacy_failure_is_terminal_without_format_retry(
    strict_transport, monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 3)
    alias = next(iter(compact_packet(strict_transport.packet).evidence_ids))
    mock = model(monkeypatch, [formatter_result(output(alias))])
    original_clean = strict_transport.guard.clean

    async def scan(text: str) -> str:
        if "claims.text" in json.loads(text):
            raise ValueError("合成输出扫描失败")
        return await original_clean(text)

    monkeypatch.setattr(strict_transport.guard, "clean", scan)
    with pytest.raises(ValueError, match="输出扫描失败"):
        await compile_packet(strict_transport.packet, strict_transport.guard)
    assert len(mock.calls) == 1 and mock.aclose.await_count == 1


@pytest.mark.asyncio
async def test_strict_tool_length_retry_increases_only_output_limit_and_accounts_complete_contract(
    strict_transport, monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    alias = next(iter(compact_packet(strict_transport.packet).evidence_ids))
    mock = model(monkeypatch, [formatter_result(output(alias), finish="length"), formatter_result(output(alias))])
    reservations = []
    strict_transport.guard.reserve = reservations.append
    await compile_packet(strict_transport.packet, strict_transport.guard)
    assert [call["max_tokens"] for call in mock.kwargs] == [4096, 8192]
    assert reservations == [token_bound(encoded(request_payload(mock, index))) + limit
                            for index, limit in enumerate([4096, 8192])]


@pytest.mark.asyncio
async def test_auto_output_keeps_existing_text_contract_and_global_privacy_settings(
    transport, monkeypatch,
) -> None:
    initial_privacy_kwargs = copy.deepcopy(transport.guard.privacy_kwargs)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "auto")
    global_body = '{"thinking":{"type":"disabled"}}'
    privacy_body = '{"reasoning_effort":"none"}'
    monkeypatch.setattr(settings, "LLM_EXTRA_BODY", global_body)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_EXTRA_BODY", privacy_body)
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=output(alias))])
    await compile_packet(transport.packet, transport.guard)
    assert not mock.kwargs[0].get("tools")
    assert "tool_choice" not in mock.kwargs[0]
    assert "response_format" not in mock.kwargs[0]
    assert callable(mock.kwargs[0]["json_schema_builder"])
    assert settings.LLM_EXTRA_BODY == global_body
    assert settings.PROJECT_BRAIN_PRIVACY_EXTRA_BODY == privacy_body
    assert transport.guard.privacy_kwargs == initial_privacy_kwargs


@pytest.mark.parametrize("mode", ["tool", "off", "strict", "execute"])
def test_unknown_strict_transport_configuration_is_rejected(mode: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE=mode)
