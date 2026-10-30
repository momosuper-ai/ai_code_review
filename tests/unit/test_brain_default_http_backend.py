"""Exercise the SDK's actual default backend, without real socket I/O.

Do not pass an httpx client into AsyncOpenAI: that hid the production backend
identity mismatch. Only its existing send boundary is replaced with RAM data.
"""
import importlib
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
import httpx
from openai import APIStatusError, APITimeoutError, AsyncOpenAI, InternalServerError

from src.config import settings
from src.core.llm_client import _transport_phase
from src.core.project_brain import transport
from src.core.project_brain.transport import PreparedCall, run_model_call
from src.core.project_brain.usage import CallSpec
from tests.unit.test_brain_http_retry import Guard


@pytest.fixture(autouse=True)
def bounded_retries(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MODEL_HTTP_RETRIES", 2)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT", 8)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_OUTPUT_MAX_CALLS_PER_UNIT", 64)


@pytest.fixture
def pauses(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(transport.asyncio, "sleep", sleep)
    return delays


def backend_for_request(request):
    """Select from the SDK-created request, never the classifier whitelist."""
    return importlib.import_module(type(request).__module__.split(".", 1)[0])


def unread_response(backend, request):
    class Body(backend.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":'
            yield b'{"message":"synthetic-only"}}'

    return backend.Response(500, request=request, stream=Body())


@asynccontextmanager
async def default_sdk(monkeypatch, outcomes):
    # No injected client: use precisely the SDK's default concrete backend.
    client = AsyncOpenAI(base_url="https://synthetic.invalid/v1", api_key="synthetic", max_retries=0)
    sends, responses = [], []

    async def memory_send(request, **kwargs):
        index = len(sends)
        sends.append(request)
        assert index < len(outcomes), "unexpected additional SDK send"
        backend = backend_for_request(request)
        outcome = outcomes[index]
        timeout_kinds = {"connect_timeout": "ConnectTimeout", "read_timeout": "ReadTimeout",
                         "write_timeout": "WriteTimeout", "pool_timeout": "PoolTimeout"}
        if outcome in timeout_kinds:
            raise getattr(backend, timeout_kinds[outcome])("synthetic-only", request=request)
        if outcome == "closed_unread":
            response = unread_response(backend, request)
            await response.aclose()
        elif outcome == 200:
            response = backend.Response(200, request=request, json={
                "id": "synthetic-completion", "object": "chat.completion", "created": 1,
                "model": "synthetic", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "synthetic-success"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}})
        else:
            response = backend.Response(outcome, request=request,
                json={"error": {"message": "synthetic-only", "type": "synthetic"}})
        responses.append(response)
        return response

    monkeypatch.setattr(client._client, "send", memory_send)
    try:
        yield client, sends, responses
    finally:
        for response in responses:
            await response.aclose()
        await client.close()


def preparation(client):
    prepared = []

    def prepare():
        async def send():
            return await client.chat.completions.create(model="synthetic", max_tokens=2,
                messages=[{"role": "user", "content": "synthetic-only"}])

        item = PreparedCall(100 + len(prepared), send)
        prepared.append(item)
        return item

    return prepare, prepared


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 429])
async def test_default_backend_complete_error_retries_with_separate_paid_uuid(status, monkeypatch, pauses):
    guard = Guard()
    async with default_sdk(monkeypatch, [status, 200]) as (client, sends, responses):
        prepare, prepared = preparation(client)
        result, call_id, actual = await run_model_call(guard,
            CallSpec("privacy", "local_model", "semantic_output"), prepare,
            timeout=60, after_response=True, privacy=True)
        assert len(sends) == len(responses) == len(prepared) == guard.calls == 2
        assert responses[0].is_closed and responses[0].is_stream_consumed
        assert result.choices[0].message.content == "synthetic-success"
        assert actual is prepared[1]
        ids = [row[0] for row in guard.reservations]
        assert len(set(ids)) == 2 and all(str(UUID(value)) == value for value in ids)
        assert call_id == ids[1]
        assert [row[1] for row in guard.reservations] == [100, 101]
        assert guard.call_meter.closed == [(ids[0], 100, status)]
        groups = [row[2].transport_group for row in guard.reservations]
        assert len(set(groups)) == 1 and str(UUID(groups[0])) == groups[0]
        assert [row[2].transport_attempt for row in guard.reservations] == [1, 2]
        assert len(pauses) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 402, 408, 501, 505])
async def test_default_backend_nonretryable_complete_status_keeps_original_call(status, monkeypatch, pauses):
    guard = Guard()
    async with default_sdk(monkeypatch, [status]) as (client, sends, responses):
        prepare, _ = preparation(client)
        with pytest.raises(APIStatusError) as caught:
            await run_model_call(guard, CallSpec("privacy", "local_model", "semantic_output"),
                prepare, timeout=60, after_response=True, privacy=True)
        assert caught.value.response is responses[0] and caught.value.status_code == status
        assert len(sends) == len(guard.reservations) == 1
        assert not guard.call_meter.closed and not pauses


@pytest.mark.asyncio
async def test_default_backend_already_closed_unread_body_is_not_accepted(monkeypatch, pauses):
    guard = Guard()
    async with default_sdk(monkeypatch, ["closed_unread"]) as (client, sends, responses):
        prepare, _ = preparation(client)
        with pytest.raises(InternalServerError) as caught:
            await run_model_call(guard, CallSpec("privacy", "local_model", "semantic_output"),
                prepare, timeout=60, after_response=True, privacy=True)
        assert caught.value.response is responses[0]
        assert responses[0].is_closed and not responses[0].is_stream_consumed
        assert len(sends) == len(guard.reservations) == 1
        assert not guard.call_meter.closed and not pauses


@pytest.mark.asyncio
async def test_default_backend_still_open_body_cannot_be_closed_as_completed_error(monkeypatch, pauses):
    guard = Guard()
    # Normal SDK error handling consumes an open response. This explicitly
    # tests the classifier's incomplete-response boundary with the actual
    # default backend's Request/Response, without simulating that consumption.
    async with default_sdk(monkeypatch, []) as (client, sends, _):
        request = client._client.build_request("POST", "https://synthetic.invalid/v1")
        response = unread_response(backend_for_request(request), request)
        error = InternalServerError("synthetic-only", response=response, body=None)
        assert not response.is_closed and not response.is_stream_consumed

        async def send():
            raise error

        with pytest.raises(InternalServerError) as caught:
            await run_model_call(guard, CallSpec("privacy", "local_model", "semantic_output"),
                lambda: PreparedCall(100, send), timeout=60, after_response=True, privacy=True)
        assert caught.value is error and not sends
        assert len(guard.reservations) == 1 and not guard.call_meter.closed and not pauses
        await response.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["connect", "read", "write", "pool"])
async def test_default_backend_sdk_timeout_retains_ambiguous_reservation(monkeypatch, pauses, phase):
    guard = Guard()
    async with default_sdk(monkeypatch, [phase + "_timeout"]) as (client, sends, _):
        prepare, _ = preparation(client)
        with pytest.raises(APITimeoutError) as caught:
            await run_model_call(guard, CallSpec("privacy", "local_model", "semantic_output"),
                prepare, timeout=60, after_response=True, privacy=True)
        assert len(sends) == len(guard.reservations) == 1
        assert not guard.call_meter.closed and not pauses
        assert _transport_phase(caught.value) == phase


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_kind", ["default", "legacy_httpx"])
async def test_partially_consumed_closed_stream_is_not_a_complete_http_error(monkeypatch, pauses, backend_kind):
    guard = Guard()
    async with default_sdk(monkeypatch, []) as (client, sends, _):
        request = client._client.build_request("POST", "https://synthetic.invalid/v1")
        backend = backend_for_request(request)
        if backend_kind == "legacy_httpx":
            backend = httpx
            request = httpx.Request("POST", "https://synthetic.invalid/v1")
        response = unread_response(backend, request)
        iterator = response.aiter_raw()
        try:
            assert await iterator.__anext__()
            await response.aclose()
            assert response.is_closed and response.is_stream_consumed
            with pytest.raises(backend.ResponseNotRead):
                response.content
            # The SDK normally refuses an uncached body in its error factory.
            # Keep the actual response and use the minimal genuine SDK status
            # error to exercise this strict classification boundary instead.
            error = APIStatusError("synthetic-only", response=response, body=None)

            async def send():
                raise error

            with pytest.raises(APIStatusError) as caught:
                await run_model_call(guard, CallSpec("privacy", "local_model", "semantic_output"),
                    lambda: PreparedCall(100, send), timeout=60, after_response=True, privacy=True)
            assert caught.value is error and not sends
            assert len(guard.reservations) == 1 and not guard.call_meter.closed and not pauses
        finally:
            await iterator.aclose()
            await response.aclose()
