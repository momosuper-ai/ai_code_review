"""Offline regression for syntax errors inside knowledge formatter arguments."""

import copy
import json
import re
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.llm_client import TokenUsage
from src.core.project_brain.compiler import compile_packet, encoded, token_bound
from src.core.project_brain.context import compact_packet
from src.utils.logger import logger
from tests.unit.test_brain_compiler_transport import model, output, transport
from tests.unit.test_brain_source_bound import bound_output, expected_item_refs
from tests.unit.test_brain_strict_transport import formatter_result, request_payload


@pytest.fixture(params=["source_bound", "strict_tool"])
def json_transport(transport: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, request) -> SimpleNamespace:
    """Exercise both function transports entirely through an in-memory model."""
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", request.param)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_MAX_TOKENS", 4096)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS", 16384)
    transport.mode = request.param
    return transport


def valid_payload(fixture: SimpleNamespace, *, text: str = "完整声明职责") -> dict:
    if fixture.mode == "source_bound":
        return bound_output(fixture.packet, text=text)
    alias = next(iter(compact_packet(fixture.packet).evidence_ids))
    return json.loads(output(alias, text=text))


def broken_arguments(payload: dict, *, case: str = "invalid_escape") -> str:
    """Retain a complete-looking payload while breaking JSON, not its schema."""
    original = encoded(payload)
    title = json.dumps(payload["title"], ensure_ascii=False)
    bad_title = {
        "invalid_escape": '"synthetic-private-title\\q"',
        "unescaped_quote": '"synthetic-private-title"quoted""',
        "control_character": '"synthetic-private-title\nline"',
    }
    if case == "trailing_comma":
        return original[:-1] + ",}"
    if case == "markdown_fence":
        return "```json\n" + original + "\n```"
    return original.replace(title, bad_title[case], 1)


def capture_diagnostics(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    messages: list[str] = []
    for level in ("warning", "info", "error"):
        monkeypatch.setattr(logger, level, lambda template, *args: messages.append(template % args))
    return messages


@pytest.mark.asyncio
async def test_invalid_json_retry_preserves_accounting_privacy_and_complete_output(
    json_transport, monkeypatch,
) -> None:
    """A syntax retry pays for both calls and scans all returned knowledge."""
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    reasoning_secret = "synthetic-private-reasoning-credential-Z"
    output_secret = "synthetic-private-output-credential-Q"
    reasoning = "完整必要推理。" * 900 + reasoning_secret
    narrative = '完整职责：含 "引号"、反斜杠\\与换行\n' + "重要行为与限制。" * 800 + output_secret
    payload = valid_payload(json_transport, text=narrative)
    payload["unknowns"] = ["完整未知项：" + output_secret]
    first = formatter_result(broken_arguments(payload), usage=TokenUsage(101, 51))
    first.message = ChatCompletionMessage(**first.message.model_dump(exclude_none=True),
                                          reasoning_content=reasoning)
    mock = model(monkeypatch, [first, formatter_result(encoded(payload), usage=TokenUsage(211, 71))])
    diagnostics = capture_diagnostics(monkeypatch)
    reservations, charges, scans = [], [], []
    json_transport.guard.reserve = reservations.append
    json_transport.guard.account = lambda usage, estimated, bound: charges.append((usage, estimated, bound))
    original_clean = json_transport.guard.clean

    async def scan(text: str) -> str:
        record = json.loads(text)
        scans.append(copy.deepcopy(record))
        if "model_retry_reasoning" in record:
            json_transport.guard.filter._dynamic_values[reasoning_secret] = "SECRET"
        if "claims.text" in record:
            json_transport.guard.filter._dynamic_values[output_secret] = "SECRET"
        return await original_clean(text)

    monkeypatch.setattr(json_transport.guard, "clean", scan)
    result = await compile_packet(json_transport.packet, json_transport.guard)
    assert len(mock.calls) == 2 and mock.aclose.await_count == 1
    assert [kwargs["max_tokens"] for kwargs in mock.kwargs] == [4096, 4096]
    assert all(kwargs["retry_max"] == 0 for kwargs in mock.kwargs)
    assert reservations == [token_bound(encoded(request_payload(mock, index))) + 4096 for index in range(2)]
    assert [charge[0].total_tokens for charge in charges] == [152, 282]
    assert [charge[1] for charge in charges] == [False, False]
    assert [charge[2] for charge in charges] == reservations
    assert scans[0] == json_transport.packet
    assert [record["model_retry_reasoning"] for record in scans if "model_retry_reasoning" in record] == [reasoning]
    semantic_scans = [record for record in scans if "claims.text" in record]
    assert len(semantic_scans) == 1
    assert all(text == narrative for text in semantic_scans[0]["claims.text"])
    assert semantic_scans[0]["unknowns"] == payload["unknowns"]
    assert reasoning_secret not in encoded(mock.calls[1])
    assert "完整必要推理。" * 900 in mock.calls[1][2]["reasoning_content"]
    assert "<<SECRET_" in mock.calls[1][2]["reasoning_content"]
    assert "synthetic-private-title" not in encoded(mock.calls[1])
    assert all("tool_calls" not in message and "tool_call_id" not in message for message in mock.calls[1])
    assert "invalid_json" in mock.calls[1][-1]["content"]
    assert "转义" in mock.calls[1][-1]["content"] and "尾随逗号" in mock.calls[1][-1]["content"]
    assert "必须填满Schema规定的slots全集" not in mock.calls[1][-1]["content"]
    assert output_secret not in result.model_dump_json() and "<<SECRET_" in result.model_dump_json()
    assert all("重要行为与限制。" * 800 in claim.text for claim in result.claims)
    if json_transport.mode == "source_bound":
        assert len(result.claims) == len(json_transport.packet["items"])
        for claim, item in zip(result.claims, json_transport.packet["items"], strict=True):
            assert set(claim.evidence_ids) == expected_item_refs(item)
            assert set(claim.source_scope.source_ref_ids) == expected_item_refs(item)
    else:
        assert len(result.claims) == 1 and result.claims[0].source_scope is None
    assert any("failure_code=invalid_json" in message for message in diagnostics)
    assert all(secret not in message for secret in (reasoning_secret, output_secret, "synthetic-private-title")
               for message in diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["invalid_escape", "unescaped_quote", "control_character", "trailing_comma", "markdown_fence"])
async def test_two_invalid_json_results_fail_closed_without_formal_result(
    json_transport, monkeypatch, case,
) -> None:
    """Do not repair syntax locally, drop fields, or publish a partial result."""
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    payload = valid_payload(json_transport, text="synthetic-private-claim-body")
    arguments = broken_arguments(payload, case=case)
    mock = model(monkeypatch, [formatter_result(arguments), formatter_result(arguments)])
    diagnostics = capture_diagnostics(monkeypatch)
    reservations, charges, scans = [], [], []
    json_transport.guard.reserve = reservations.append
    json_transport.guard.account = lambda usage, estimated, bound: charges.append((usage, estimated, bound))
    original_clean = json_transport.guard.clean

    async def scan(text: str) -> str:
        scans.append(json.loads(text))
        return await original_clean(text)

    monkeypatch.setattr(json_transport.guard, "clean", scan)
    with pytest.raises(ValueError, match=r"失败：invalid_json$") as captured:
        await compile_packet(json_transport.packet, json_transport.guard)
    assert str(captured.value) == "语义编译结构或证据校验失败：invalid_json"
    assert len(mock.calls) == len(reservations) == len(charges) == 2
    assert [charge[0].total_tokens for charge in charges] == [30, 30]
    assert [charge[2] for charge in charges] == reservations
    assert mock.aclose.await_count == 1
    assert scans == [json_transport.packet]
    assert sum("failure_code=invalid_json" in message for message in diagnostics) == 2
    assert not any("source_bound_error=" in message for message in diagnostics)
    assert all("synthetic-private-title" not in message and "synthetic-private-claim-body" not in message
               for message in diagnostics)
    if json_transport.mode == "source_bound":
        syntax_logs = [message for message in diagnostics if "json_error_kind=" in message]
        assert len(syntax_logs) == 2
        assert all(re.search(r"json_error_kind=[a-z_]+ line=\d+ column=\d+ offset=\d+$", message)
                   for message in syntax_logs)
    else:
        assert any("schema_error_type=json_invalid" in message for message in diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["contract", "invalid_path"])
async def test_valid_json_contract_and_path_errors_keep_distinct_categories(
    json_transport, monkeypatch, case,
) -> None:
    """Syntax diagnostics must not conceal existing provenance or path gates."""
    payload = valid_payload(json_transport)
    if case == "invalid_path":
        payload["document_path"] = "docs/project-brain/../../business.py"
        expected = "invalid_document_path"
    elif json_transport.mode == "source_bound":
        del payload["slots"][next(iter(payload["slots"]))]
        expected = "schema_mismatch"
    else:
        payload["claims"][0]["evidence_ids"] = ["E999999"]
        expected = "unknown_evidence"
    mock = model(monkeypatch, [formatter_result(encoded(payload))])
    diagnostics = capture_diagnostics(monkeypatch)
    with pytest.raises(ValueError, match=expected):
        await compile_packet(json_transport.packet, json_transport.guard)
    assert len(mock.calls) == 1
    assert any(f"failure_code={expected}" in message for message in diagnostics)
    assert not any("failure_code=invalid_json" in message or "json_error_kind=" in message for message in diagnostics)
    if case == "contract" and json_transport.mode == "source_bound":
        assert any("source_bound_error=slot_set_mismatch missing_count=1" in message for message in diagnostics)
    elif case == "contract":
        assert any("reference_error=unknown_e_alias count=1" in message for message in diagnostics)


@pytest.mark.asyncio
async def test_duplicate_json_fields_still_fail_source_bound_contract(transport, monkeypatch) -> None:
    """JSON syntax classification cannot weaken the duplicate identity gate."""
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    payload = bound_output(transport.packet)
    arguments = encoded(payload)[:-1] + ',"synthetic-private-duplicate-key":"synthetic-private-value",' \
        '"synthetic-private-duplicate-key":"synthetic-private-value"}'
    mock = model(monkeypatch, [formatter_result(arguments)])
    diagnostics = capture_diagnostics(monkeypatch)
    with pytest.raises(ValueError, match="schema_mismatch"):
        await compile_packet(transport.packet, transport.guard)
    assert len(mock.calls) == 1
    assert any("source_bound_error=duplicate_json_keys" in message and "duplicate_count=1" in message
               for message in diagnostics)
    assert not any("failure_code=invalid_json" in message for message in diagnostics)
    assert all("synthetic-private-duplicate-key" not in message and "synthetic-private-value" not in message
               for message in diagnostics)
