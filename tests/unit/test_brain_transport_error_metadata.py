"""Transport diagnostics contain fixed categories, never exception bodies."""

from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APITimeoutError, InternalServerError

from src.core.llm_client import LLMClient, _transport_phase
from src.utils.logger import logger


@pytest.mark.parametrize("kind,phase", [
    (httpx.ConnectTimeout, "connect"), (httpx.ReadTimeout, "read"),
    (httpx.WriteTimeout, "write"), (httpx.PoolTimeout, "pool"),
])
@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_phase_from_wrapped_timeout_type(kind, phase, link):
    outer = APITimeoutError(request=httpx.Request("POST", "https://gateway.invalid/secret"))
    middle = RuntimeError("synthetic-private-cause")
    setattr(outer, link, middle)
    setattr(middle, link, kind("synthetic-private-timeout"))
    assert _transport_phase(outer) == phase


def test_chain_cycles_depth_and_text_are_not_parsed():
    first, second = RuntimeError("connect timeout"), RuntimeError("read timeout")
    first.__cause__, second.__context__ = second, first
    assert _transport_phase(first) == "unknown"
    chain = httpx.ConnectTimeout("synthetic-private-leaf")
    for _ in range(20):
        outer = RuntimeError("synthetic-private-wrapper")
        outer.__cause__ = chain
        chain = outer
    assert _transport_phase(chain) == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_metadata_logs_phase_without_sensitive_transport_data(monkeypatch, retry):
    request = httpx.Request("POST", "https://gateway.invalid/secret-path",
                            headers={"Authorization": "synthetic-private-key"})
    exc = APITimeoutError(request=request)
    exc.__cause__ = httpx.ReadTimeout("synthetic-private-body", request=request)
    messages = []
    for level in ("warning", "error"):
        monkeypatch.setattr(logger, level, lambda template, *args: messages.append(template % args))
    monkeypatch.setattr("src.core.llm_client.asyncio.sleep", AsyncMock())
    client = LLMClient("synthetic-model", sdk_retries=0, metadata_errors=True)
    client._client.chat.completions.create = AsyncMock(side_effect=exc)
    try:
        with pytest.raises(APITimeoutError):
            await client.chat([{"role": "user", "content": "synthetic-private-input"}],
                              retry_max=1 if retry else 0, max_tokens=16)
        assert client._client.chat.completions.create.await_count == (2 if retry else 1)
        assert len(messages) == (2 if retry else 1)
        assert all("transport_phase=read" in message for message in messages)
        assert all(private not in message for private in
                   ("synthetic-private", "gateway.invalid", "secret-path", "Authorization") for message in messages)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_status_error_keeps_status_without_inferring_timeout(monkeypatch):
    request = httpx.Request("POST", "https://gateway.invalid/secret-path")
    response = httpx.Response(500, request=request, text="synthetic-private-body")
    exc = InternalServerError("synthetic-private-message", response=response,
                              body={"private": "synthetic-private-body"})
    messages = []
    monkeypatch.setattr(logger, "error", lambda template, *args: messages.append(template % args))
    client = LLMClient("synthetic-model", sdk_retries=0, metadata_errors=True)
    client._client.chat.completions.create = AsyncMock(side_effect=exc)
    try:
        with pytest.raises(InternalServerError):
            await client.chat([{"role": "user", "content": "synthetic-input"}], retry_max=0, max_tokens=16)
        assert len(messages) == 1
        assert "HTTP状态=500" in messages[0] and "transport_phase=unknown" in messages[0]
        assert "synthetic-private" not in messages[0] and "gateway.invalid" not in messages[0]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_legacy_error_logging_does_not_change(monkeypatch):
    messages = []
    monkeypatch.setattr(logger, "error", lambda template, *args: messages.append(template % args))
    client = LLMClient("synthetic-model", sdk_retries=0)
    client._client.chat.completions.create = AsyncMock(side_effect=RuntimeError("legacy-example"))
    try:
        with pytest.raises(RuntimeError):
            await client.chat([{"role": "user", "content": "synthetic-input"}], retry_max=0, max_tokens=16)
        assert "legacy-example" in messages[0] and "transport_phase=" not in messages[0]
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,phase", [
    (httpx.ConnectTimeout, "connect"), (httpx.ReadTimeout, "read"),
    (httpx.WriteTimeout, "write"), (httpx.PoolTimeout, "pool"),
])
async def test_real_sdk_wrapping_preserves_phase_with_in_memory_transport(monkeypatch, kind, phase):
    from openai import AsyncOpenAI

    messages, sends = [], []
    monkeypatch.setattr(logger, "error", lambda template, *args: messages.append(template % args))

    async def fail(request):
        sends.append(True)
        raise kind("synthetic-private-body", request=request)

    client = LLMClient("synthetic-model", sdk_retries=0, metadata_errors=True)
    await client.aclose()
    client._client = AsyncOpenAI(api_key="synthetic-private-key", base_url="https://gateway.invalid/v1",
                                max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail)))
    try:
        with pytest.raises(APITimeoutError):
            await client.chat([{"role": "user", "content": "synthetic-private-input"}], retry_max=0, max_tokens=16)
        assert len(sends) == 1 and len(messages) == 1
        assert f"transport_phase={phase}" in messages[0]
        assert "synthetic-private" not in messages[0] and "gateway.invalid" not in messages[0]
    finally:
        await client.aclose()
