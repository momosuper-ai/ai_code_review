"""Explicit, billed HTTP recovery; ambiguous responses never enter this path."""

import asyncio
import random
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable
from uuid import uuid4

from openai import APIStatusError

from src.config import settings
from src.core.http_transport import complete_response
from src.core.project_brain.exceptions import ModelTransportFailed, ModelWorkAdmissionDenied
from src.core.project_brain.timing import segment
from src.core.project_brain.usage import CallSpec, HTTP_ERROR_UNIT_LIMIT
from src.utils.logger import logger

RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class PreparedCall:
    """Attempt-local safe input stays in RAM, bound to its actual response."""

    bound: int
    send: Callable[[], Awaitable[Any]]
    context: Any = None


def completed_http_error(exc: Exception) -> int | None:
    """Never infer closure from exception text, a status attribute or a stream."""
    if not isinstance(exc, APIStatusError):
        return None
    response = exc.response
    if (not complete_response(response) or type(exc.status_code) is not int
            or type(response.status_code) is not int
            or response.status_code != exc.status_code
            or exc.status_code not in RETRYABLE_HTTP_STATUS):
        return None
    return exc.status_code


def _delay(exc: APIStatusError, retry: int) -> float:
    delay = (2.0 if retry == 1 else min(20.0, 5.0 * 2 ** (retry - 2))) + random.uniform(0, 1)
    # Only bounded numeric seconds; never echo or persist arbitrary headers.
    value = exc.response.headers.get("retry-after", "")
    if len(value) <= 8 and value.isascii() and value.isdecimal():
        delay = max(delay, min(30, int(value)))
    return delay


def _admit_work(guard, *, timeout: float, after_response: bool, privacy: bool,
                attempt: int, phase: str) -> None:
    """Separate local exhaustion from a genuine HTTP recovery already underway."""
    if guard.ledger is None and attempt == 1:
        return
    call_limit = (settings.PROJECT_BRAIN_PRIVACY_OUTPUT_MAX_CALLS_PER_UNIT if after_response
                  else settings.PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT)
    remaining = guard.remaining_seconds()
    reason = ("deadline" if remaining < timeout else
              "call_limit" if privacy and guard.calls >= call_limit else None)
    if reason is None:
        return
    if attempt > 1:
        # A closed HTTP failure already happened. Its bounded recovery must
        # remain terminal, never become a fresh resumable input delivery.
        raise ModelTransportFailed("model_http_retry_window_exhausted")
    error = ModelWorkAdmissionDenied(reason, phase, after_response=after_response,
                                     remaining_seconds=max(0, int(remaining)),
                                     required_seconds=int(timeout))
    logger.warning("知识模型工作准入拒绝：role=%s phase=%s reason=%s after_response=%s reservation_created=%s remaining_seconds=%d required_seconds=%d",
                   "privacy" if privacy else "semantic", error.phase, error.reason,
                   error.after_response, error.reservation_created, max(0, int(remaining)), int(timeout))
    raise error


async def run_model_call(guard, spec: CallSpec, prepare: Callable[[], PreparedCall], *,
                         timeout: float, after_response: bool, privacy: bool = False):
    """Retry complete HTTP errors in this RAM context with fresh durable calls.

    The caller validates and settles the *successful* result. Error bodies are
    not model outputs. No caller can retry without durable per-send accounting.
    """
    group = str(uuid4())
    retries = settings.PROJECT_BRAIN_MODEL_HTTP_RETRIES
    for attempt in range(1, retries + 2):
        guard.check_discoveries()
        _admit_work(guard, timeout=timeout, after_response=after_response, privacy=privacy,
                    attempt=attempt, phase="before_prepare")
        request = prepare()
        # Preparation can authenticate/project a large input. Re-check before
        # acquiring a durable reservation, rather than stranding a known-unsent
        # request when that work consumed the remaining window.
        _admit_work(guard, timeout=timeout, after_response=after_response, privacy=privacy,
                    attempt=attempt, phase="before_reserve")
        current_spec = replace(spec, transport_group=group, transport_attempt=attempt)
        call_id = await guard.reserve_model_call(request.bound, current_spec,
            after_response=after_response or attempt > 1, minimum_seconds=timeout)
        guard.check_discoveries()
        # Once a reservation exists, no caller may convert this outcome into
        # zero usage or a safe continuation: its durable charge stays intact.
        _admit_work(guard, timeout=timeout, after_response=after_response, privacy=privacy,
                    attempt=attempt, phase="after_reserve")
        # Input or rule changes while waiting for a budget must not cause a
        # stale request to be sent. prepare()'s send adapter reapplies the
        # latest filter under the already reserved conservative upper bound.
        if privacy:
            guard.calls += 1
        try:
            with segment("model_call"):
                result = await asyncio.wait_for(request.send(), timeout=timeout)
            return result, call_id, request
        except Exception as exc:
            status = completed_http_error(exc)
            if status is None or guard.call_meter is None or call_id is None:
                raise
            closed = guard.call_meter.close_http_error(call_id, request.bound, status)
            logger.warning("知识模型HTTP恢复：role=%s attempt=%d status=%d closed_errors=%d",
                           spec.role, attempt, status, closed)
            if attempt > retries or closed >= HTTP_ERROR_UNIT_LIMIT:
                raise ModelTransportFailed("model_http_retry_exhausted") from None
            delay = _delay(exc, attempt)
            call_limit = (settings.PROJECT_BRAIN_PRIVACY_OUTPUT_MAX_CALLS_PER_UNIT if after_response
                          else settings.PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT)
            if guard.remaining_seconds() < delay + timeout or (privacy and guard.calls >= call_limit):
                raise ModelTransportFailed("model_http_retry_window_exhausted") from None
            await asyncio.sleep(delay)
    raise AssertionError("unreachable transport attempt")
