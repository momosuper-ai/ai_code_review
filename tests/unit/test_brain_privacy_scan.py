"""Regressions for bounded, restartable, fail-closed Brain privacy scans."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.llm_client import ChatResult, TokenUsage
from src.core.project_brain.compiler import CompilationLimitError, CompilationGuard, encoded


@pytest.fixture(autouse=True)
def hybrid(monkeypatch: pytest.MonkeyPatch) -> None:
    """All model calls in this file are deterministic substitutes."""
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")


def scanner(monkeypatch: pytest.MonkeyPatch, values: list[str] | None = None) -> SimpleNamespace:
    """Detect complete fixture values, never invoke a network service."""
    calls = []

    async def chat(messages: list[dict], **kwargs: object) -> ChatResult:
        calls.append(messages[-1]["content"])
        items = [{"value": value, "type": "SECRET"} for value in values or [] if value in calls[-1]]
        return ChatResult(ChatCompletionMessage(role="assistant", content=encoded({"items": items})), TokenUsage(100, 10))

    mock = SimpleNamespace(chat=chat, aclose=AsyncMock(), calls=calls)
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", lambda *args, **kwargs: mock)
    return mock


@pytest.mark.asyncio
async def test_empty_structures_do_not_call_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = scanner(monkeypatch)
    guard = CompilationGuard(lambda *args: None, lambda *args: None)
    for value in ([], {}, {"imports": [], "globals": {}, "calls": [], "count": 0}):
        assert json.loads(await guard.clean(encoded(value))) == value
    assert not mock.calls


@pytest.mark.asyncio
async def test_oversized_scalar_is_rejected_instead_of_cutting_its_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "演示口令甲乙丙丁戊己庚辛壬癸松鼠蓝帆零零壹"
    mock = scanner(monkeypatch, [value])
    guard = CompilationGuard(lambda *args: None, lambda *args: None)
    with pytest.raises(CompilationLimitError, match="单个字段"):
        await guard.clean("。" * 12000 + value)
    assert not mock.calls


@pytest.mark.asyncio
async def test_large_complete_field_is_scanned_alone_and_tail_secret_is_removed(monkeypatch) -> None:
    secret = "合成尾端口令松鼠蓝帆甲乙丙丁零零壹"
    large = "完整合成推理。" * 600 + secret
    mock = scanner(monkeypatch, [secret])
    guard = CompilationGuard(lambda *args: None, lambda *args: None)
    safe = json.loads(await guard.clean(encoded({"model_retry_reasoning": ["first", large, "last"]})))
    assert secret not in encoded(safe)
    matching = [json.loads(call) for call in mock.calls if secret in call]
    assert matching == [{"model_retry_reasoning": [large]}]
    assert any("first" in call for call in mock.calls)
    assert any("last" in call for call in mock.calls)
    assert "完整合成推理。" * 600 in safe["model_retry_reasoning"][1]


@pytest.mark.asyncio
async def test_large_field_requires_actual_model_context_before_reservation(monkeypatch) -> None:
    mock = scanner(monkeypatch)
    reservations = []
    monkeypatch.setattr(settings, "LLM_MAX_CONTEXT_TOKENS", 12000)
    monkeypatch.setattr(settings, "LLM_CONTEXT_THRESHOLD", 0.9)
    guard = CompilationGuard(lambda *args: None, reservations.append)
    with pytest.raises(CompilationLimitError, match="模型上下文"):
        await guard.clean(encoded(["中" * 2600]))
    assert not mock.calls and not reservations


@pytest.mark.asyncio
async def test_mask_expansion_cannot_exceed_complete_field_limit(monkeypatch) -> None:
    mock = scanner(monkeypatch)
    reservations = []
    guard = CompilationGuard(lambda *args: None, reservations.append)
    # A very short prior discovery can make its stable placeholders larger
    # than the original. Enforce capacity on the actual model input too.
    guard.filter._dynamic_values["a"] = "SECRET"
    with pytest.raises(CompilationLimitError, match="单个字段"):
        await guard.clean(encoded(["a" * 3000]))
    assert not mock.calls and not reservations


@pytest.mark.asyncio
async def test_large_single_field_output_length_never_accepts_parseable_partial_json(monkeypatch) -> None:
    mock = scanner(monkeypatch)
    charges = []

    async def truncated(messages, **kwargs):
        mock.calls.append(messages[-1]["content"])
        return ChatResult(ChatCompletionMessage(role="assistant", content='{"items":[]}'),
                          TokenUsage(100, 4096), finish_reason="length")

    mock.chat = truncated
    guard = CompilationGuard(lambda usage, *args: charges.append(usage.total_tokens), lambda *args: None)
    with pytest.raises(CompilationLimitError, match="单个字段的输出仍被截断"):
        await guard.clean(encoded(["中" * 2600]))
    assert len(mock.calls) == 1 and charges == [4196]


def test_privacy_field_limit_must_cover_normal_packet_limit() -> None:
    from pydantic import ValidationError
    from src.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, PROJECT_BRAIN_PRIVACY_PACKET_MAX_TOKENS=6000,
                 PROJECT_BRAIN_PRIVACY_MAX_FIELD_TOKENS=5000)


@pytest.mark.asyncio
async def test_field_rejection_diagnostics_do_not_expose_names_or_values(monkeypatch) -> None:
    from src.utils.logger import logger

    field_name = "synthetic-private-field-name"
    value = "private-example-value-" * 1800
    diagnostics = []
    monkeypatch.setattr(logger, "warning", lambda template, *args: diagnostics.append(template % args))
    mock = scanner(monkeypatch)
    guard = CompilationGuard(lambda *args: None, lambda *args: None)
    with pytest.raises(ValueError, match="单个字段"):
        await guard.clean(encoded({field_name: value}))
    assert not mock.calls
    assert len(diagnostics) == 1
    assert "phase=privacy role=generic" in diagnostics[0]
    assert field_name not in diagnostics[0] and "private-example-value" not in diagnostics[0]
    assert "input_bound=" in diagnostics[0] and "field_cap=32000" in diagnostics[0]


@pytest.mark.asyncio
async def test_truncated_batch_splits_locally_and_accounts_failed_call(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = scanner(monkeypatch)
    usage = []
    original = mock.chat
    count = 0

    async def chat(messages: list[dict], **kwargs: object) -> object:
        nonlocal count
        count += 1
        if count == 1:
            return SimpleNamespace(message=ChatCompletionMessage(role="assistant", content='{"items":['),
                                   usage=TokenUsage(100, 2048), finish_reason="length")
        return await original(messages, **kwargs)

    mock.chat = chat
    guard = CompilationGuard(lambda tokens, *args: usage.append(tokens.total_tokens), lambda *args: None)
    assert json.loads(await guard.clean('["first","second"]')) == ["first", "second"]
    assert count == 3
    assert usage[0] == 2148 and len(usage) == 3


@pytest.mark.asyncio
async def test_restart_reuses_safe_cache_and_late_secret_cleans_prior_result(tmp_path, monkeypatch) -> None:
    from src.core.project_brain.privacy import PrivacyLedger

    secret = "演示口令甲乙丙丁戊己庚辛壬癸松鼠蓝帆零零壹"
    mock = scanner(monkeypatch)
    first = CompilationGuard(lambda *args: None, lambda *args: None, PrivacyLedger(tmp_path))
    prior = encoded({"note": secret})
    assert secret in await first.clean(prior)
    first_calls = len(mock.calls)
    await first.close()

    # A later unit discovers the same value; only fingerprints survive restart.
    mock = scanner(monkeypatch, [secret])
    later = CompilationGuard(lambda *args: None, lambda *args: None, PrivacyLedger(tmp_path))
    assert secret not in await later.clean(encoded({"credential": secret}))
    await later.close()
    resumed = CompilationGuard(lambda *args: None, lambda *args: None, PrivacyLedger(tmp_path))
    calls_before = len(mock.calls)
    assert secret not in await resumed.clean(prior)
    assert len(mock.calls) == calls_before and first_calls == 1
    await resumed.close()
    assert all(secret not in path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))


@pytest.mark.asyncio
async def test_privacy_protocol_override_does_not_change_global_semantic_parameters(monkeypatch) -> None:
    from src.core.llm_client import LLMClient

    global_parameters = '{"thinking":{"type":"disabled"}}'
    override = {"chat_template_kwargs": {"enable_thinking": False}}
    monkeypatch.setattr(settings, "LLM_EXTRA_BODY", global_parameters)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_EXTRA_BODY", json.dumps(override))
    guard = CompilationGuard(lambda *args: None, lambda *args: None)
    seen = []

    async def create(**kwargs: object) -> object:
        seen.append(kwargs.get("extra_body"))
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason="stop",
            message=ChatCompletionMessage(role="assistant", content='{"items":[]}'))])

    monkeypatch.setattr(guard.local._client.chat.completions, "create", create)
    await guard.clean('["ordinary"]')
    await guard.local.chat([{"role": "user", "content": "ordinary"}])
    await guard.local.chat([{"role": "user", "content": "ordinary"}], extra_body=None)
    assert seen == [override, json.loads(global_parameters), None]
    assert settings.LLM_EXTRA_BODY == global_parameters
    await guard.close()


@pytest.mark.asyncio
async def test_brain_transport_failures_do_not_retry_outside_reserved_budget(monkeypatch) -> None:
    import httpx
    from openai import APIConnectionError

    guard = CompilationGuard(lambda *args: None, lambda *args: None)
    calls = 0

    async def fail(**kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise APIConnectionError(message="synthetic", request=httpx.Request("POST", "https://fixture.invalid"))

    monkeypatch.setattr(guard.local._client.chat.completions, "create", fail)
    assert guard.local._client.max_retries == 0
    with pytest.raises(APIConnectionError):
        await guard.clean('["ordinary"]')
    assert calls == 1
    await guard.close()


def test_invalid_privacy_protocol_config_is_rejected() -> None:
    from pydantic import ValidationError
    from src.config import Settings

    for invalid in ("{invalid", "[]", "null"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, PROJECT_BRAIN_PRIVACY_EXTRA_BODY=invalid)


@pytest.mark.asyncio
async def test_dedup_preserves_semantic_field_context_across_packet_wrappers(tmp_path, monkeypatch) -> None:
    from src.core.project_brain.privacy import PrivacyLedger

    secret = "演示口令甲乙丙丁戊己庚辛壬癸松鼠蓝帆零零壹"
    mock = scanner(monkeypatch)
    first = CompilationGuard(lambda *args: None, lambda *args: None, PrivacyLedger(tmp_path))
    await first.clean(encoded({"attributes": {"description": secret}}))
    await first.close()
    mock = scanner(monkeypatch, [secret])
    second = CompilationGuard(lambda *args: None, lambda *args: None, PrivacyLedger(tmp_path))
    await second.clean(encoded({"items": [{"fact": {"attributes": {"description": secret}}}]}))
    assert all(secret not in call for call in mock.calls)
    # Changing the semantic field must cause a new classification.
    assert secret not in await second.clean(encoded({"attributes": {"credential": secret}}))
    assert any(secret in call for call in mock.calls)
    await second.close()


@pytest.mark.asyncio
async def test_finished_subpacket_is_not_recalled_after_deferred_work(tmp_path, monkeypatch) -> None:
    from src.core.project_brain.privacy import PrivacyLedger, PrivacyScanDeferred

    mock = scanner(monkeypatch)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT", 1)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_PACKET_MAX_TOKENS", 2048)
    text = encoded(["第一项" * 80, "第二项" * 80, "第三项" * 80])
    guard = CompilationGuard(lambda *args: None, lambda *args: None, PrivacyLedger(tmp_path))
    with pytest.raises(PrivacyScanDeferred):
        await guard.clean(text)
    assert len(mock.calls) == 1
    await guard.close()
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT", 8)
    resumed = CompilationGuard(lambda *args: None, lambda *args: None, PrivacyLedger(tmp_path))
    assert await resumed.clean(text) == text
    assert len(mock.calls) == 3
    await resumed.close()


@pytest.mark.asyncio
async def test_resume_after_truncated_parent_does_not_skip_unscanned_child(tmp_path, monkeypatch) -> None:
    from src.core.project_brain.privacy import PrivacyLedger, PrivacyScanDeferred

    secret = "桃花冬雨松鼠蓝帆甲乙丙丁戊己庚辛壬癸零零壹"
    mock = scanner(monkeypatch, [secret])
    original = mock.chat
    count = 0

    async def chat(messages: list[dict], **kwargs: object) -> object:
        nonlocal count
        count += 1
        if count == 1:
            return ChatResult(ChatCompletionMessage(role="assistant", content='{"items":[]}'), TokenUsage(100, 2048), "length")
        return await original(messages, **kwargs)

    mock.chat = chat
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT", 2)
    text = encoded(["ordinary", secret])
    guard = CompilationGuard(lambda *args: None, lambda *args: None, PrivacyLedger(tmp_path))
    with pytest.raises(PrivacyScanDeferred):
        await guard.clean(text)
    await guard.close()
    resumed = CompilationGuard(lambda *args: None, lambda *args: None, PrivacyLedger(tmp_path))
    assert secret not in await resumed.clean(text)
    assert count == 3
    await resumed.close()
