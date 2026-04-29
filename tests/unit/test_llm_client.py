"""Tests for LLMClient response_format auto-injection logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.llm_client import LLMClient


def _fake_response(content: str = '{"ok":1}', tool_calls=None):
    """构造最小可用的 OpenAI ChatCompletion 响应对象。"""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls
    choice = MagicMock(message=msg, finish_reason="stop")
    usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    usage.prompt_tokens_details = None
    return MagicMock(choices=[choice], usage=usage)


@pytest.mark.asyncio
async def test_chat_auto_injects_json_object_when_no_tools(monkeypatch):
    """默认 mode=json_object 且 tools=None 时，应自动挂上 response_format。"""
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE", "json_object")
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE_CLOUD", "")
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE_LOCAL", "")
    monkeypatch.setattr("src.config.settings.CLOUD_LLM_MODEL_NAME", "")
    monkeypatch.setattr("src.config.settings.VLLM_MODEL_NAME", "test-model")

    client = LLMClient("test-model")
    client._client.chat.completions.create = AsyncMock(return_value=_fake_response())

    await client.chat([{"role": "user", "content": "hi"}], tools=None)

    kwargs = client._client.chat.completions.create.await_args.kwargs
    assert kwargs.get("response_format") == {"type": "json_object"}


@pytest.mark.asyncio
async def test_chat_does_not_inject_when_tools_present(monkeypatch):
    """工具循环阶段必须不挂 response_format（与 tools 并存会冲突）。"""
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE", "json_schema")
    monkeypatch.setattr("src.config.settings.VLLM_MODEL_NAME", "test-model")
    monkeypatch.setattr("src.config.settings.CLOUD_LLM_MODEL_NAME", "")

    client = LLMClient("test-model")
    client._client.chat.completions.create = AsyncMock(return_value=_fake_response(tool_calls=[]))

    tool_def = {"type": "function", "function": {"name": "x", "parameters": {}}}
    await client.chat([{"role": "user", "content": "hi"}], tools=[tool_def])

    kwargs = client._client.chat.completions.create.await_args.kwargs
    assert "response_format" not in kwargs


@pytest.mark.asyncio
async def test_chat_response_format_none_disables(monkeypatch):
    """显式传 response_format=None 用于 README 等 Markdown 输出场景，应禁用注入。"""
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE", "json_object")
    monkeypatch.setattr("src.config.settings.VLLM_MODEL_NAME", "test-model")
    monkeypatch.setattr("src.config.settings.CLOUD_LLM_MODEL_NAME", "")

    client = LLMClient("test-model")
    client._client.chat.completions.create = AsyncMock(return_value=_fake_response())

    await client.chat([{"role": "user", "content": "hi"}], tools=None, response_format=None)

    kwargs = client._client.chat.completions.create.await_args.kwargs
    assert "response_format" not in kwargs


@pytest.mark.asyncio
async def test_chat_off_mode_no_injection(monkeypatch):
    """mode=off 时即便 tools=None 也不挂 response_format。"""
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE", "off")
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE_CLOUD", "")
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE_LOCAL", "")
    monkeypatch.setattr("src.config.settings.VLLM_MODEL_NAME", "test-model")
    monkeypatch.setattr("src.config.settings.CLOUD_LLM_MODEL_NAME", "")

    client = LLMClient("test-model")
    client._client.chat.completions.create = AsyncMock(return_value=_fake_response())

    await client.chat([{"role": "user", "content": "hi"}], tools=None)

    kwargs = client._client.chat.completions.create.await_args.kwargs
    assert "response_format" not in kwargs


@pytest.mark.asyncio
async def test_chat_json_schema_mode_with_builder(monkeypatch):
    """mode=json_schema + 有 builder 时应注入完整 schema。"""
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE", "json_schema")
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE_CLOUD", "")
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE_LOCAL", "")
    monkeypatch.setattr("src.config.settings.VLLM_MODEL_NAME", "test-model")
    monkeypatch.setattr("src.config.settings.CLOUD_LLM_MODEL_NAME", "")

    client = LLMClient("test-model")
    client._client.chat.completions.create = AsyncMock(return_value=_fake_response())

    test_schema = {
        "type": "json_schema",
        "json_schema": {"name": "test", "strict": True, "schema": {"type": "object"}},
    }
    await client.chat(
        [{"role": "user", "content": "hi"}], tools=None,
        json_schema_builder=lambda: test_schema,
    )

    kwargs = client._client.chat.completions.create.await_args.kwargs
    assert kwargs.get("response_format") == test_schema


@pytest.mark.asyncio
async def test_chat_cloud_override_takes_precedence(monkeypatch):
    """云端模型应使用 LLM_RESPONSE_FORMAT_MODE_CLOUD 而非全局 mode。"""
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE", "json_schema")
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE_CLOUD", "json_object")
    monkeypatch.setattr("src.config.settings.LLM_RESPONSE_FORMAT_MODE_LOCAL", "")
    monkeypatch.setattr("src.config.settings.CLOUD_LLM_MODEL_NAME", "cloud-model")
    monkeypatch.setattr("src.config.settings.VLLM_MODEL_NAME", "local-model")

    client = LLMClient("cloud-model")
    client._client.chat.completions.create = AsyncMock(return_value=_fake_response())

    await client.chat([{"role": "user", "content": "hi"}], tools=None)

    kwargs = client._client.chat.completions.create.await_args.kwargs
    # cloud override 强制用 json_object，即使全局是 json_schema
    assert kwargs.get("response_format") == {"type": "json_object"}
