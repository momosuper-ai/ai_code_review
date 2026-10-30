"""Brain extensions cannot replace the request that was scanned and budgeted."""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import AsyncOpenAI

from src.config import settings
from src.core.llm_client import LLMClient
from src.core.project_brain import compiler
from tests.unit.test_brain_compiler_transport import transport  # noqa: F401


CONTROLLED_FIELDS = (
    "model", "messages", "max_tokens", "max_completion_tokens", "tools",
    "tool_choice", "response_format", "stream", "n", "temperature", "top_p",
    "stop", "functions", "function_call",
)
PRIVATE_MARKER = "synthetic-untrusted-configuration-marker"


@pytest.fixture(autouse=True)
def isolated_extensions(monkeypatch):
    """Never inherit the developer machine's gateway parameters in these tests."""
    monkeypatch.setattr(settings, "LLM_EXTRA_BODY", "")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_EXTRA_BODY", "")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_EXTRA_BODY", "")
    monkeypatch.setattr(settings, "PRIVACY_CUSTOM_PATTERNS", "")


def configure(monkeypatch, role, source, value):
    field = "LLM_EXTRA_BODY" if source == "global" else (
        "PROJECT_BRAIN_PRIVACY_EXTRA_BODY" if role == "privacy"
        else "PROJECT_BRAIN_SEMANTIC_EXTRA_BODY")
    monkeypatch.setattr(settings, field, value)


@pytest.mark.parametrize("role", ["privacy", "semantic"])
@pytest.mark.parametrize("source", ["global", "brain"])
@pytest.mark.parametrize("field", CONTROLLED_FIELDS)
def test_controlled_request_fields_are_rejected_without_echoing_values(monkeypatch, role, source, field):
    configure(monkeypatch, role, source, json.dumps({field: PRIVATE_MARKER}))
    with pytest.raises(ValueError) as error:
        compiler.brain_extra_body(role)
    assert PRIVATE_MARKER not in str(error.value)
    # Even known conflicting field names need not become diagnostic payloads.
    assert field not in str(error.value)


@pytest.mark.parametrize("role", ["privacy", "semantic"])
def test_blank_brain_configuration_inherits_only_validated_global_extensions(monkeypatch, role):
    configure(monkeypatch, role, "global", '{"reasoning_effort":"none"}')
    configure(monkeypatch, role, "brain", "  ")
    assert compiler.brain_extra_body(role) == {"reasoning_effort": "none"}


@pytest.mark.parametrize("role", ["privacy", "semantic"])
def test_brain_override_replaces_global_and_does_not_modify_other_consumers(monkeypatch, role):
    global_body = '{"model":"legacy-review-override"}'
    configure(monkeypatch, role, "global", global_body)
    configure(monkeypatch, role, "brain", '{"reasoning_effort":"none"}')
    assert compiler.brain_extra_body(role) == {"reasoning_effort": "none"}
    assert settings.LLM_EXTRA_BODY == global_body
    other_role = "semantic" if role == "privacy" else "privacy"
    with pytest.raises(ValueError):
        compiler.brain_extra_body(other_role)


@pytest.mark.parametrize("role", ["privacy", "semantic"])
@pytest.mark.parametrize("source", ["global", "brain"])
@pytest.mark.parametrize("value", ["[]", "null", "true", '"scalar"',
                                   '{"reasoning_effort":"' + PRIVATE_MARKER + '"'])
def test_invalid_json_or_nonobject_extensions_fail_with_a_fixed_private_error(monkeypatch, role, source, value):
    configure(monkeypatch, role, source, value)
    with pytest.raises(ValueError) as error:
        compiler.brain_extra_body(role)
    assert PRIVATE_MARKER not in str(error.value)
    assert value not in str(error.value)


def test_empty_configuration_is_an_explicit_empty_object():
    assert compiler.brain_extra_body("privacy") == {}
    assert compiler.brain_extra_body("semantic") == {}


@pytest.mark.parametrize("source", ["global", "brain"])
@pytest.mark.parametrize("field", ["model", "messages", "max_tokens"])
def test_privacy_override_is_rejected_before_client_or_budget_creation(monkeypatch, source, field):
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    configure(monkeypatch, "privacy", source, json.dumps({field: PRIVATE_MARKER}))
    constructor = MagicMock(side_effect=AssertionError("invalid configuration reached the model client"))
    account, reserve = MagicMock(), MagicMock()
    monkeypatch.setattr(compiler, "LLMClient", constructor)
    with pytest.raises(ValueError):
        compiler.CompilationGuard(account, reserve)
    constructor.assert_not_called()
    account.assert_not_called()
    reserve.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["global", "brain"])
@pytest.mark.parametrize("field", ["model", "messages", "max_tokens"])
async def test_semantic_override_fails_before_input_scan_client_or_reservation(transport, monkeypatch, source, field):
    configure(monkeypatch, "semantic", source, json.dumps({field: PRIVATE_MARKER}))
    constructor = MagicMock(side_effect=AssertionError("invalid configuration reached the model client"))
    monkeypatch.setattr(compiler, "LLMClient", constructor)
    transport.guard.clean = AsyncMock(side_effect=AssertionError("invalid configuration started privacy scanning"))
    transport.guard.reserve = MagicMock()
    transport.guard.reserve_model_call = AsyncMock()
    with pytest.raises(ValueError):
        await compiler.compile_packet(transport.packet, transport.guard)
    transport.guard.clean.assert_not_awaited()
    constructor.assert_not_called()
    transport.guard.reserve.assert_not_called()
    transport.guard.reserve_model_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_sdk_merge_can_override_scanned_request_but_legacy_review_contract_is_unchanged(monkeypatch):
    """Prove the SDK merge boundary using a local fake transport, without I/O."""
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "synthetic-completion", "object": "chat.completion", "created": 0,
            "model": "legacy-review-override",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "{}"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })

    replacement_messages = [{"role": "user", "content": "synthetic-extra-body-message"}]
    global_body = json.dumps({"model": "legacy-review-override", "messages": replacement_messages,
                              "max_tokens": 900, "reasoning_effort": "none"})
    monkeypatch.setattr(settings, "LLM_EXTRA_BODY", global_body)
    sdk = AsyncOpenAI(api_key="synthetic-only-key", base_url="https://synthetic.invalid/v1",
                      max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr("src.core.llm_client.AsyncOpenAI", lambda **kwargs: sdk)
    client = LLMClient("review-configured-model", sdk_retries=0)
    try:
        await client.chat([{"role": "user", "content": "synthetic-original-scanned-message"}],
                          max_tokens=32, response_format=None, retry_max=0)
        assert len(captured) == 1
        assert captured[0]["model"] == "legacy-review-override"
        assert captured[0]["messages"] == replacement_messages
        assert captured[0]["max_tokens"] == 900
        assert settings.LLM_EXTRA_BODY == global_body
        # The Brain check closes this same boundary without changing review.
        for role in ("privacy", "semantic"):
            with pytest.raises(ValueError):
                compiler.brain_extra_body(role)
    finally:
        await client.aclose()
