"""Real scanner call sites must distinguish work admission from HTTP failures."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import settings
from src.core.project_brain.compiler import CompilationGuard, CompilationLimitError, compile_packet, encoded
from src.core.project_brain.exceptions import ModelTransportFailed, ModelWorkAdmissionDenied
from src.core.project_brain.privacy import PrivacyLedger, PrivacyScanDeferred
from src.core.project_brain.transport import run_model_call
from tests.unit.test_brain_compiler_privacy_window import prepare_input, window  # noqa: F401
from tests.unit.test_brain_http_retry import Guard, Meter, preparation, spec, status_error
from tests.unit.test_brain_strict_transport import formatter_result
from tests.unit.test_brain_trusted_packet import checkpoint  # noqa: F401


@pytest.mark.asyncio
async def test_input_authentication_crosses_admission_deadline_without_http_failure(tmp_path, monkeypatch):
    """The scanner passed its check; one more authentication consumes its margin."""
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    monkeypatch.setattr(settings, "PRIVACY_FILTER_LEVEL", "minimal")
    monkeypatch.setattr(settings, "PRIVACY_CUSTOM_PATTERNS", "")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_CALL_TIMEOUT", 60)
    local = SimpleNamespace(chat=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", lambda *args, **kwargs: local)
    reservations = []
    guard = CompilationGuard(lambda *args: None, reservations.append, PrivacyLedger(tmp_path / "private"))
    reads = []

    def remaining():
        reads.append(True)
        return 60.5 if len(reads) == 1 else 59.5

    monkeypatch.setattr(guard, "remaining_seconds", remaining)
    try:
        with pytest.raises(PrivacyScanDeferred):
            await guard.clean(encoded({"source_excerpt": "complete synthetic input"}))
        assert len(reads) >= 2
        assert reservations == []
        assert local.chat.await_count == 0
        assert guard.calls == 0
        assert guard.after_response is False
    finally:
        await guard.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_prepare", "before_reserve", "after_reserve"])
async def test_work_admission_phase_preserves_unspent_or_reserved_boundary(phase):
    """No SDK send occurs; an existing reservation is never fabricated away."""
    guard = Guard(seconds=0 if phase == "before_prepare" else 480)
    original_prepare, prepared, sends = preparation([object()])

    def prepare():
        result = original_prepare()
        if phase == "before_reserve":
            guard.seconds = 0
        return result

    if phase == "after_reserve":
        original_reserve = guard.reserve_model_call

        async def reserve(*args, **kwargs):
            call_id = await original_reserve(*args, **kwargs)
            guard.seconds = 0
            return call_id

        guard.reserve_model_call = reserve
    with pytest.raises(ModelWorkAdmissionDenied) as caught:
        await run_model_call(guard, spec(), prepare, timeout=60, after_response=True, privacy=True)
    failure = caught.value
    assert failure.failure_code == "model_work_window_exhausted"
    assert failure.reason == "deadline" and failure.phase == phase and failure.after_response is True
    assert failure.reservation_created is (phase == "after_reserve")
    assert len(prepared) == (phase != "before_prepare")
    assert len(guard.reservations) == (phase == "after_reserve")
    assert sends == [] and guard.calls == 0
    assert guard.call_meter.closed == [] and guard.call_meter.settled == []


@pytest.mark.asyncio
async def test_local_privacy_call_limit_is_distinct_from_http_recovery(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT", 8)
    guard = Guard(calls=8)
    prepare, prepared, sends = preparation([object()])
    with pytest.raises(ModelWorkAdmissionDenied) as caught:
        await run_model_call(guard, spec(), prepare, timeout=60, after_response=False, privacy=True)
    assert caught.value.reason == "call_limit"
    assert caught.value.phase == "before_prepare" and caught.value.reservation_created is False
    assert prepared == sends == guard.reservations == []


@pytest.mark.asyncio
async def test_actual_closed_http_recovery_window_remains_terminal():
    guard = Guard()
    prepare, prepared, sends = preparation([status_error()], on_send=lambda _: setattr(guard, "seconds", 0))
    with pytest.raises(ModelTransportFailed) as caught:
        await run_model_call(guard, spec(), prepare, timeout=60, after_response=True, privacy=True)
    assert caught.value.failure_code == "model_http_retry_window_exhausted"
    assert len(prepared) == len(sends) == len(guard.reservations) == len(guard.call_meter.closed) == 1
    assert guard.call_meter.settled == []


@pytest.mark.parametrize("reason,phase,after_response", [
    ("synthetic-private-diagnostic", "before_prepare", False),
    ([], "before_prepare", False),
    ("deadline", "synthetic-private-diagnostic", False),
    ("deadline", "before_prepare", 1),
])
def test_admission_diagnostic_rejects_unbounded_fields(reason, phase, after_response):
    with pytest.raises(ValueError) as caught:
        ModelWorkAdmissionDenied(reason, phase, after_response=after_response)
    assert "synthetic-private-diagnostic" not in str(caught.value)


@pytest.mark.asyncio
async def test_paid_output_authentication_crosses_deadline_without_http_misclassification(window, monkeypatch):
    """The same boundary after paid output stays terminal without regeneration."""
    await prepare_input(window)
    window.semantic.chat.return_value = window.response(2)
    guard = window.fresh_guard()
    output_reads = []

    def remaining():
        if not guard.after_response:
            return 480
        output_reads.append(True)
        return 60.5 if len(output_reads) == 1 else 59.5

    monkeypatch.setattr(guard, "remaining_seconds", remaining)
    try:
        with pytest.raises(CompilationLimitError, match="语义输出.*privacy_window_exhausted"):
            await compile_packet(window.packet, guard, proof=window.approved)
        assert len(output_reads) >= 2
        assert window.semantic.chat.await_count == 1
        assert window.local.chat.await_count == 4
        assert guard.calls == 0
        persisted = "".join(path.read_text(encoding="utf-8") for path in window.private.rglob("*.json"))
        assert "完整合成解释" not in persisted
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_initial_semantic_admission_can_defer_without_a_new_request(window, monkeypatch):
    """Authentication crossing the semantic threshold has not consumed output."""
    await prepare_input(window)
    guard = window.fresh_guard()
    guard.call_meter = Meter()
    reads = []

    def remaining():
        reads.append(True)
        return 120.5 if len(reads) == 1 else 119.5

    monkeypatch.setattr(guard, "remaining_seconds", remaining)
    try:
        with pytest.raises(PrivacyScanDeferred):
            await compile_packet(window.packet, guard, proof=window.approved)
        assert len(reads) >= 2
        assert window.semantic.chat.await_count == 0
        assert window.local.chat.await_count == 4
        assert guard.call_meter.reservations == [] and guard.call_meter.settled == []
        assert guard.calls == 0
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_paid_semantic_format_retry_admission_cannot_reset_by_deferring(window, monkeypatch):
    """A later delivery must not turn an exhausted paid retry into attempt one."""
    await prepare_input(window)
    window.semantic.chat.return_value = formatter_result("{}")
    guard = window.fresh_guard()
    guard.call_meter = Meter()
    retry_reads = []

    def remaining():
        if not window.semantic.chat.await_count:
            return 480
        retry_reads.append(True)
        return 120.5 if len(retry_reads) == 1 else 119.5

    monkeypatch.setattr(guard, "remaining_seconds", remaining)
    try:
        with pytest.raises(CompilationLimitError, match="semantic_retry_window_exhausted"):
            await compile_packet(window.packet, guard, proof=window.approved)
        assert len(retry_reads) >= 2
        assert window.semantic.chat.await_count == 1
        assert window.local.chat.await_count == 4
        assert len(guard.call_meter.reservations) == len(guard.call_meter.settled) == 1
        assert guard.call_meter.closed == []
        assert guard.calls == 0
    finally:
        await guard.close()
