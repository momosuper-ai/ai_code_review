"""Explicit HTTP retries retain cost, privacy provenance and the paid response."""

import asyncio
import json
import logging
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.llm_client import ChatResult, LLMClient, TokenUsage
from src.core.project_brain import transport
from src.core.project_brain.compiler import compile_packet, encoded
from src.core.project_brain.exceptions import BudgetBusy, BudgetExhausted, ModelTransportFailed, ModelWorkAdmissionDenied
from src.core.project_brain.privacy import PrivacyLedger
from src.core.project_brain.privacy_provenance import canonical_json
from src.core.project_brain.transport import PreparedCall, run_model_call
from src.core.project_brain.usage import CallSpec
from src.utils.logger import logger
from tests.unit.test_brain_compiler_privacy_window import prepare_input, window  # noqa: F401
from tests.unit.test_brain_trusted_packet import checkpoint  # noqa: F401


PRIVATE = "synthetic-private-provider-text-do-not-log"


class UnreadBody(httpx.AsyncByteStream):
    """A response object alone does not prove that its body was received."""

    async def __aiter__(self):
        yield PRIVATE.encode()


def status_error(status=500, *, complete=True, retry_after=None):
    request = httpx.Request("POST", "https://gateway.invalid/private-route")
    headers = {} if retry_after is None else {"retry-after": retry_after}
    response = httpx.Response(status, request=request, headers=headers,
        **({"content": PRIVATE.encode()} if complete else {"stream": UnreadBody()}))
    return APIStatusError(PRIVATE, response=response, body={"private": PRIVATE})


class Meter:
    """In-memory double of the transaction boundary, never of model output."""

    def __init__(self, *, prior_errors=0):
        self.reservations = []
        self.closed = []
        self.settled = []
        self.errors = prior_errors

    def reserve(self, call_id, bound, spec):
        self.reservations.append((call_id, bound, spec))
        return call_id

    def close_http_error(self, call_id, bound, status):
        self.closed.append((call_id, bound, status))
        self.errors += 1
        return self.errors

    def settle(self, call_id, usage, estimated, bound, response_model):
        self.settled.append((call_id, usage, estimated, bound, response_model))


class Guard:
    def __init__(self, *, metered=True, seconds=480, calls=0):
        self.call_meter = Meter() if metered else None
        self.ledger = object()
        self.calls = calls
        self.seconds = seconds
        self.checks = 0
        self.reservations = []

    def remaining_seconds(self):
        return self.seconds

    def check_discoveries(self):
        self.checks += 1

    async def reserve_model_call(self, bound, spec, *, after_response, minimum_seconds):
        self.check_discoveries()
        call_id = str(uuid4()) if self.call_meter is not None else None
        self.reservations.append((call_id, bound, spec, after_response, minimum_seconds))
        if self.call_meter is not None:
            self.call_meter.reserve(call_id, bound, spec)
        return call_id


@pytest.fixture(autouse=True)
def retry_config(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MODEL_HTTP_RETRIES", 2)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT", 8)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_OUTPUT_MAX_CALLS_PER_UNIT", 64)


@pytest.fixture
def pauses(monkeypatch):
    values = []

    async def pause(seconds):
        values.append(seconds)

    monkeypatch.setattr(transport.asyncio, "sleep", pause)
    return values


@pytest.fixture
def private_logs(caplog):
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def spec(privacy=True):
    return CallSpec("privacy" if privacy else "semantic", "local_model" if privacy else "deepseek-v4-flash",
                    "semantic_output" if privacy else "initial")


def preparation(outcomes, *, contexts=None, on_send=None):
    prepared, sends = [], []

    def prepare():
        index = len(prepared)
        context = {"iteration": index} if contexts is None else contexts(index)

        async def send():
            sends.append(index)
            if on_send is not None:
                on_send(index)
            outcome = outcomes[min(index, len(outcomes) - 1)]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        item = PreparedCall(bound=100 + index, send=send, context=context)
        prepared.append(item)
        return item

    return prepare, prepared, sends


def failure_is_private(error):
    assert error.failure_code in {"model_http_retry_exhausted", "model_http_retry_window_exhausted"}
    assert PRIVATE not in str(error)
    assert "private-route" not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_complete_http_error_then_success_has_new_call_and_prepared_context(status, pauses, private_logs):
    guard, success = Guard(), object()
    prepare, prepared, sends = preparation([status_error(status), success])
    result, call_id, actual = await run_model_call(guard, spec(), prepare, timeout=60,
                                                  after_response=True, privacy=True)
    assert result is success and actual is prepared[1]
    assert actual.context == {"iteration": 1}
    assert sends == [0, 1] and guard.calls == 2
    ids = [row[0] for row in guard.reservations]
    assert len(set(ids)) == 2 and all(str(UUID(value)) == value for value in ids)
    assert call_id == ids[1]
    assert guard.call_meter.closed == [(ids[0], 100, status)]
    assert [row[1] for row in guard.reservations] == [100, 101]
    assert all(row[2].role == "privacy" and row[2].purpose == "semantic_output" for row in guard.reservations)
    groups = [row[2].transport_group for row in guard.reservations]
    assert len(set(groups)) == 1 and str(UUID(groups[0])) == groups[0]
    assert [row[2].transport_attempt for row in guard.reservations] == [1, 2]
    assert all(row[3] is True for row in guard.reservations)
    assert 2 <= pauses[0] <= 3 and len(pauses) == 1
    assert PRIVATE not in private_logs.text and "private-route" not in private_logs.text
    assert guard.checks >= 2


@pytest.mark.asyncio
async def test_consecutive_errors_stop_at_two_extra_attempts(pauses, private_logs):
    guard = Guard()
    prepare, prepared, sends = preparation([status_error()])
    with pytest.raises(ModelTransportFailed) as caught:
        await run_model_call(guard, spec(), prepare, timeout=60, after_response=True, privacy=True)
    failure_is_private(caught.value)
    assert len(prepared) == len(sends) == len(guard.call_meter.closed) == guard.calls == 3
    assert len({row[0] for row in guard.reservations}) == 3
    assert len(pauses) == 2 and 2 <= pauses[0] <= 3 and 5 <= pauses[1] <= 6
    assert PRIVATE not in private_logs.text


@pytest.mark.asyncio
async def test_zero_retries_still_records_closed_error_without_resending(pauses, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MODEL_HTTP_RETRIES", 0)
    guard = Guard()
    prepare, _, sends = preparation([status_error()])
    with pytest.raises(ModelTransportFailed):
        await run_model_call(guard, spec(), prepare, timeout=60, after_response=False, privacy=True)
    assert sends == [0] and len(guard.call_meter.closed) == 1 and not pauses


@pytest.mark.asyncio
async def test_no_persistent_meter_never_adds_diagnostic_request(pauses):
    guard, error = Guard(metered=False), status_error()
    prepare, _, sends = preparation([error, object()])
    with pytest.raises(APIStatusError) as caught:
        await run_model_call(guard, spec(), prepare, timeout=60, after_response=False, privacy=True)
    assert caught.value is error and sends == [0] and not pauses


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 408, 409, 501, 505])
async def test_non_retryable_status_keeps_original_unknown_call(status, pauses):
    guard, error = Guard(), status_error(status)
    prepare, _, sends = preparation([error])
    with pytest.raises(APIStatusError) as caught:
        await run_model_call(guard, spec(), prepare, timeout=60, after_response=True, privacy=True)
    assert caught.value is error and sends == [0]
    assert not guard.call_meter.closed and not pauses


@pytest.mark.asyncio
async def test_unread_http_body_is_not_a_complete_error_response(pauses):
    guard, error = Guard(), status_error(complete=False)
    prepare, _, sends = preparation([error])
    with pytest.raises(APIStatusError) as caught:
        await run_model_call(guard, spec(), prepare, timeout=60, after_response=True, privacy=True)
    assert caught.value is error and sends == [0]
    assert not guard.call_meter.closed and not pauses
    await error.response.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sdk_timeout", "connection", "timeout", "lookalike"])
async def test_ambiguous_or_lookalike_errors_never_close_or_retry(kind, pauses):
    request = httpx.Request("POST", "https://gateway.invalid/private-route")
    error = {"sdk_timeout": APITimeoutError(request=request),
             "connection": APIConnectionError(message=PRIVATE, request=request),
             "timeout": TimeoutError(PRIVATE), "lookalike": RuntimeError(PRIVATE)}[kind]
    if kind == "lookalike":
        error.status_code, error.response = 500, status_error().response
    guard = Guard()
    prepare, _, sends = preparation([error])
    with pytest.raises(type(error)) as caught:
        await run_model_call(guard, spec(), prepare, timeout=60, after_response=True, privacy=True)
    assert caught.value is error and sends == [0]
    assert not guard.call_meter.closed and not pauses


@pytest.mark.asyncio
async def test_actual_wait_for_timeout_does_not_start_a_replacement(pauses):
    guard, sends = Guard(), []

    def prepare():
        async def send():
            sends.append(True)
            await asyncio.Future()
        return PreparedCall(bound=100, send=send, context=None)

    with pytest.raises(TimeoutError):
        await run_model_call(guard, spec(), prepare, timeout=0.01, after_response=True, privacy=True)
    assert len(sends) == len(guard.reservations) == 1
    assert not guard.call_meter.closed and not pauses


@pytest.mark.asyncio
@pytest.mark.parametrize("after_response,limit", [(False, 8), (True, 64)])
async def test_privacy_call_limit_stops_before_preparing_or_sending(after_response, limit, pauses):
    guard = Guard(calls=limit)
    prepare, _, sends = preparation([object()])
    with pytest.raises(ModelWorkAdmissionDenied) as caught:
        await run_model_call(guard, spec(), prepare, timeout=60,
                             after_response=after_response, privacy=True)
    assert caught.value.reason == "call_limit" and caught.value.phase == "before_prepare"
    assert caught.value.reservation_created is False and caught.value.after_response is after_response
    assert not sends and not guard.reservations and not pauses


@pytest.mark.asyncio
async def test_work_deadline_stops_before_any_paid_request(pauses):
    guard = Guard(seconds=0)
    prepare, _, sends = preparation([object()])
    with pytest.raises(ModelWorkAdmissionDenied) as caught:
        await run_model_call(guard, spec(False), prepare, timeout=120,
                             after_response=False, privacy=False)
    assert caught.value.reason == "deadline" and caught.value.phase == "before_prepare"
    assert caught.value.reservation_created is False and caught.value.after_response is False
    assert not sends and not guard.reservations and not pauses


@pytest.mark.asyncio
async def test_budget_wait_cannot_send_after_the_work_deadline(pauses):
    guard = Guard()
    reserve = guard.reserve_model_call

    async def delayed_reservation(*args, **kwargs):
        call_id = await reserve(*args, **kwargs)
        guard.seconds = 0  # Simulate time consumed by the reservation wait.
        return call_id

    guard.reserve_model_call = delayed_reservation
    prepare, _, sends = preparation([object()])
    with pytest.raises(ModelWorkAdmissionDenied) as caught:
        await run_model_call(guard, spec(), prepare, timeout=60,
                             after_response=True, privacy=True)
    assert caught.value.reason == "deadline" and caught.value.phase == "after_reserve"
    assert caught.value.reservation_created is True and caught.value.after_response is True
    assert not sends and len(guard.reservations) == 1 and not guard.call_meter.closed


@pytest.mark.asyncio
async def test_deadline_after_closed_error_prevents_a_new_reservation(pauses):
    guard = Guard()
    prepare, _, sends = preparation([status_error()], on_send=lambda _: setattr(guard, "seconds", 0))
    with pytest.raises(ModelTransportFailed):
        await run_model_call(guard, spec(), prepare, timeout=60, after_response=True, privacy=True)
    assert sends == [0] and len(guard.reservations) == len(guard.call_meter.closed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [BudgetExhausted("closed-budget"), BudgetBusy()])
async def test_failed_initial_reservation_does_not_send_or_close(failure, pauses):
    guard = Guard()
    guard.reserve_model_call = AsyncMock(side_effect=failure)
    prepare, _, sends = preparation([object()])
    with pytest.raises(type(failure)):
        await run_model_call(guard, spec(), prepare, timeout=60, after_response=False, privacy=True)
    assert not sends and not guard.call_meter.closed and not pauses


@pytest.mark.asyncio
async def test_retry_budget_exhaustion_keeps_closed_charge_and_does_not_send(pauses):
    guard = Guard()
    reserve = guard.reserve_model_call

    async def bounded_reserve(*args, **kwargs):
        if guard.reservations:
            raise BudgetExhausted("closed-budget")
        return await reserve(*args, **kwargs)

    guard.reserve_model_call = bounded_reserve
    prepare, _, sends = preparation([status_error(), object()])
    with pytest.raises(BudgetExhausted):
        await run_model_call(guard, spec(), prepare, timeout=60, after_response=True, privacy=True)
    assert sends == [0] and len(guard.call_meter.closed) == 1


@pytest.mark.asyncio
async def test_six_persisted_unit_errors_are_a_cross_delivery_hard_limit(pauses):
    guard = Guard()
    guard.call_meter.errors = 5
    prepare, _, sends = preparation([status_error(), object()])
    with pytest.raises(ModelTransportFailed):
        await run_model_call(guard, spec(), prepare, timeout=60, after_response=True, privacy=True)
    assert sends == [0] and guard.call_meter.errors == 6 and not pauses


@pytest.mark.asyncio
@pytest.mark.parametrize("header,low,high", [("7", 7, 8), ("999", 30, 30)])
async def test_numeric_retry_after_is_bounded(header, low, high, pauses):
    guard = Guard()
    prepare, _, _ = preparation([status_error(retry_after=header), object()])
    await run_model_call(guard, spec(), prepare, timeout=60, after_response=True, privacy=True)
    assert len(pauses) == 1 and low <= pauses[0] <= high


@pytest.mark.asyncio
async def test_new_prepare_observes_late_privacy_rule_after_error(pauses):
    guard, rule = Guard(), {"text": "initial-source-display"}
    prepare, prepared, _ = preparation([status_error(), object()],
        contexts=lambda _: {"safe": rule["text"]},
        on_send=lambda index: rule.update(text="<<SECRET_1>>") if index == 0 else None)
    _, _, actual = await run_model_call(guard, spec(), prepare, timeout=60,
                                       after_response=True, privacy=True)
    assert prepared[0].context == {"safe": "initial-source-display"}
    assert actual.context == {"safe": "<<SECRET_1>>"}


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [500, 429, "read_timeout"])
async def test_real_sdk_over_mock_transport_does_not_hide_unmetered_retries(first, pauses, monkeypatch, private_logs):
    """Execute OpenAI parsing/classification while every HTTP send stays local."""
    requests, constructors = [], []

    async def handle(request):
        requests.append(request)
        if len(requests) == 1:
            if first == "read_timeout":
                raise httpx.ReadTimeout(PRIVATE, request=request)
            return httpx.Response(first, json={"error": {"message": PRIVATE, "type": "temporary"}},
                                  headers={"x-request-id": PRIVATE}, request=request)
        return httpx.Response(200, json={"id": "chatcmpl-synthetic", "object": "chat.completion",
            "created": 1, "model": "local_model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": '{"items":[]}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}}, request=request)

    def client_factory(**kwargs):
        constructors.append(kwargs["max_retries"])
        return AsyncOpenAI(base_url="https://gateway.invalid/v1", api_key="synthetic-mock-key",
            max_retries=kwargs["max_retries"],
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

    monkeypatch.setattr("src.core.llm_client.AsyncOpenAI", client_factory)
    client, guard = LLMClient(model_name="local_model", sdk_retries=0, metadata_errors=True), Guard()

    def prepare():
        async def send():
            return await client.chat([{"role": "user", "content": "Only synthetic input"}],
                max_tokens=32, response_format={"type": "json_object"}, retry_max=0, extra_body={})
        return PreparedCall(bound=1000, send=send, context=None)

    try:
        if first == "read_timeout":
            with pytest.raises(APITimeoutError):
                await run_model_call(guard, spec(), prepare, timeout=60,
                                     after_response=True, privacy=True)
            assert len(requests) == len(guard.reservations) == 1
            assert not guard.call_meter.closed and not pauses
        else:
            result, call_id, actual = await run_model_call(guard, spec(), prepare, timeout=60,
                                                         after_response=True, privacy=True)
            assert result.usage.total_tokens == 14 and actual.bound == 1000
            assert len(requests) == len(guard.reservations) == 2
            assert guard.call_meter.closed == [(guard.reservations[0][0], 1000, first)]
            assert call_id == guard.reservations[1][0]
        assert constructors == [0]
        assert PRIVATE not in private_logs.text and "synthetic-mock-key" not in private_logs.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_at", ["retry", "reservation"])
@pytest.mark.parametrize("return_hidden_value", [False, True])
async def test_actual_scan_binding_uses_latest_safe_input_after_late_finding(
        window, pauses, late_at, return_hidden_value):
    """A finding must occur in this UUID's input, not an earlier attempt's."""
    hidden = "PriorKnownCredentialForSyntheticLateDiscoveryOnly"
    new = "FreshCredentialForSyntheticScanOnly"
    records = [{"field": "source_excerpt", "text": f"complete synthetic source {hidden} and {new}"}]
    guard, meter, sent = window.fresh_guard(), Meter(), []
    guard.call_meter, guard.after_response = meter, True
    scope = guard.ledger.digest("synthetic isolated provenance scope")
    packet = guard.ledger.digest("packet:v2:" + encoded(records))

    if late_at == "reservation":
        reserve = meter.reserve

        def discover_during_reservation(*args, **kwargs):
            call_id = reserve(*args, **kwargs)
            guard.ledger.remember([{"value": hidden, "type": "SECRET"}])
            return call_id

        meter.reserve = discover_during_reservation

    async def scan(messages, **kwargs):
        data = json.loads(messages[-1]["content"])
        safe_records = [{"field": key, "text": text} for key, values in data.items() for text in values]
        sent.append(safe_records)
        if late_at == "retry" and len(sent) == 1:
            assert hidden in encoded(safe_records)
            guard.ledger.remember([{"value": hidden, "type": "SECRET"}])
            raise status_error()
        assert hidden not in encoded(safe_records) and new in encoded(safe_records)
        return ChatResult(ChatCompletionMessage(role="assistant", content=encoded({"items": [
            {"value": hidden if return_hidden_value else new, "type": "SECRET"}]})), TokenUsage(20, 4))

    window.local.chat.side_effect = scan
    try:
        if return_hidden_value:
            with pytest.raises(ValueError, match="隐私扫描引用无效"):
                await guard._scan(records, scope, 0)
            assert guard.ledger.receipt(packet) is None
            assert guard.ledger.mask(new) == new
        else:
            await guard._scan(records, scope, 0)
            assert guard.ledger.receipt(packet) == "completed"
            assert guard.ledger.mask(new) != new
            successful_id = meter.settled[-1][0]
            origin = guard.ledger.origins["calls"][successful_id]
            assert origin["records_hmac"] == guard.ledger.digest(
                "provenance:records:v1:" + canonical_json(sent[-1]))
            assert all(row[0] not in guard.ledger.origins["calls"] for row in meter.closed)
        assert len(sent) == len(meter.reservations) == (2 if late_at == "retry" else 1) + (2 if return_hidden_value else 0)
        assert len(meter.closed) == (1 if late_at == "retry" else 0)
        private = "".join(path.read_text(encoding="utf-8") for path in window.private.rglob("*.json"))
        assert hidden not in private and new not in private and PRIVATE not in private
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_http_error_body_is_never_a_finding_even_when_it_looks_like_valid_scan_json(window, pauses):
    value = "OnlySyntheticCredentialPresentInErrorBody"
    error = status_error()
    error.response = httpx.Response(500, request=error.response.request,
        json={"items": [{"value": value, "type": "SECRET"}]})
    error.body = {"items": [{"value": value, "type": "SECRET"}]}
    window.local.chat.side_effect = [error, ChatResult(
        ChatCompletionMessage(role="assistant", content='{"items":[]}'), TokenUsage(10, 2))]
    guard, meter = window.fresh_guard(), Meter()
    guard.call_meter, guard.after_response = meter, True
    records = [{"field": "source_excerpt", "text": f"complete synthetic input {value}"}]
    key = guard.ledger.digest("packet:v2:" + encoded(records))
    try:
        await guard._scan(records, guard.ledger.digest("isolated error-body scope"), 0)
        assert guard.ledger.receipt(key) == "completed"
        assert guard.ledger.mask(value) == value and not guard.ledger.origins["calls"]
        assert len(meter.closed) == len(meter.settled) == 1 and guard.calls == 2
        assert meter.closed[0][0] != meter.settled[0][0]
        private = "".join(path.read_text(encoding="utf-8") for path in window.private.rglob("*.json"))
        assert value not in private and PRIVATE not in private
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_output_http_retry_retains_one_semantic_response_and_private_checkpoint(window, pauses):
    await prepare_input(window)
    secret = "VegaOutputCredential"
    response = window.response(2)
    body = json.loads(response.message.tool_calls[0].function.arguments)
    body["slots"]["S1"]["texts"][0] += " " + secret
    response.message.tool_calls[0].function.arguments = encoded(body)
    window.semantic.chat.return_value = response
    original_scan, failed = window.local.chat.side_effect, False

    async def scan(messages, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise status_error()
        result = await original_scan(messages, **kwargs)
        if secret in messages[-1]["content"]:
            data = json.loads(result.message.content)
            data["items"].append({"value": secret, "type": "SECRET"})
            result.message.content = encoded(data)
        return result

    window.local.chat.side_effect = scan
    guard, meter = window.fresh_guard(), Meter()
    guard.call_meter = meter
    try:
        result = await compile_packet(window.packet, guard, proof=window.approved)
        assert window.semantic.chat.await_count == 1
        assert len(meter.closed) == 1 and meter.closed[0][2] == 500
        assert len(meter.settled) == 6  # One semantic + five intact output fields.
        assert guard.calls == 6  # Five successful scans and one closed HTTP error.
        assert secret not in encoded(result.model_dump())
        reopened = PrivacyLedger(window.private, create=False)
        assert reopened.mask(secret) != secret
        private = "".join(path.read_text(encoding="utf-8") for path in window.private.rglob("*.json"))
        assert secret not in private and "完整合成解释" not in private and PRIVATE not in private
        assert meter.closed[0][0] not in private  # Error body supplied no model findings.
        assert len({row[0] for row in meter.reservations}) == len(meter.reservations)
        assert all(call.kwargs["retry_max"] == 0 for call in window.local.chat.await_args_list)
        assert all(call.kwargs["retry_max"] == 0 for call in window.semantic.chat.await_args_list)
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_exhausted_output_http_retries_never_generate_a_second_semantic_response(window, pauses):
    await prepare_input(window)
    window.semantic.chat.return_value = window.response(2)
    window.local.chat.side_effect = status_error()
    guard, meter = window.fresh_guard(), Meter()
    guard.call_meter = meter
    try:
        with pytest.raises(ModelTransportFailed):
            await compile_packet(window.packet, guard, proof=window.approved)
        assert window.semantic.chat.await_count == 1
        assert len(meter.closed) == guard.calls == 3
        assert len(meter.settled) == 1  # Only the already received semantic response.
        private = "".join(path.read_text(encoding="utf-8") for path in window.private.rglob("*.json"))
        assert "完整合成解释" not in private and PRIVATE not in private
    finally:
        await guard.close()
