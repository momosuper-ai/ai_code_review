"""Discovery checks fence cache hits and paid requests without regenerating."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.project_brain import compiler
from src.core.project_brain.compiler import CompilationGuard, compile_packet, encoded
from src.core.project_brain.context import compact_packet
from src.core.project_brain.exceptions import BudgetBusy
from src.core.project_brain.privacy import PrivacyLedger
from src.core.project_brain.usage import CallSpec
from tests.unit.test_brain_compiler_transport import model, output, transport  # noqa: F401
from tests.unit.test_brain_privacy_scan import scanner


class StopDiscovery(Exception):
    """A terminal signal independent of syntax validation and normal deferral."""


def install_check(guard, value="synthetic_backend"):
    path = f"src/{value}.py"

    def check():
        if guard.filter.sanitize_text(path) != path:
            raise StopDiscovery("fixed_identity_conflict")

    guard.discovery_check = check


@pytest.fixture
def hybrid(monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    monkeypatch.setattr(settings, "PRIVACY_CUSTOM_PATTERNS", "")


@pytest.mark.asyncio
async def test_completed_receipt_still_checks_late_sibling_finding(tmp_path, monkeypatch, hybrid):
    mock = scanner(monkeypatch)
    text = encoded(["ordinary complete field"])
    first = CompilationGuard(lambda *args: None, lambda *args: None, PrivacyLedger(tmp_path))
    await first.clean(text)
    await first.close()
    PrivacyLedger(tmp_path).remember([{"value": "synthetic_backend", "type": "SECRET"}])
    before = len(mock.calls)
    reservations = []
    resumed = CompilationGuard(lambda *args: None, reservations.append, PrivacyLedger(tmp_path))
    install_check(resumed)
    try:
        with pytest.raises(StopDiscovery):
            await resumed.clean(text)
    finally:
        await resumed.close()
    assert len(mock.calls) == before
    assert not reservations


@pytest.mark.asyncio
async def test_discovery_is_accounted_and_persisted_before_stopping_next_scan(tmp_path, monkeypatch, hybrid):
    mock = scanner(monkeypatch, ["synthetic_backend"])
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_RECORDS_PER_PACKET", 1)
    charged = []
    ledger = PrivacyLedger(tmp_path)
    guard = CompilationGuard(lambda usage, *args: charged.append(usage.total_tokens), lambda *args: None, ledger)
    install_check(guard)
    try:
        with pytest.raises(StopDiscovery):
            await guard.clean(encoded(["synthetic_backend", "second field must not be scanned"]))
    finally:
        await guard.close()
    assert len(mock.calls) == 1
    assert charged == [110]
    assert PrivacyLedger(tmp_path).mask("synthetic_backend") != "synthetic_backend"


@pytest.mark.asyncio
async def test_known_conflict_before_first_semantic_call_creates_no_reservation(transport, monkeypatch):
    blocked = False
    original = compiler.compilation_transport

    def build_transport(*args, **kwargs):
        nonlocal blocked
        result = original(*args, **kwargs)
        blocked = True
        return result

    def check():
        if blocked:
            raise StopDiscovery("fixed_identity_conflict")

    monkeypatch.setattr(compiler, "compilation_transport", build_transport)
    transport.guard.discovery_check = check
    reserve = AsyncMock(return_value=None)
    transport.guard.reserve_model_call = reserve
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=output(alias))])
    with pytest.raises(StopDiscovery):
        await compile_packet(transport.packet, transport.guard)
    reserve.assert_not_awaited()
    assert not mock.calls
    mock.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_conflict_before_format_retry_keeps_first_call_but_never_regenerates(transport, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content="{"),
                               ChatCompletionMessage(role="assistant", content=output(alias))])
    transport.guard.discovery_check = lambda: (_ for _ in ()).throw(StopDiscovery()) if mock.calls else None
    reserve = AsyncMock(return_value=None)
    transport.guard.reserve_model_call = reserve
    with pytest.raises(StopDiscovery):
        await compile_packet(transport.packet, transport.guard)
    assert reserve.await_count == 1
    assert len(mock.calls) == 1
    mock.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_discovery_during_budget_wait_stops_next_reservation(transport, monkeypatch):
    blocked = False
    attempts = []

    def reserve(_):
        attempts.append("reservation")
        if len(attempts) == 1:
            raise BudgetBusy()

    async def wait(_):
        nonlocal blocked
        blocked = True

    def check():
        if blocked:
            raise StopDiscovery()

    transport.guard.reserve = reserve
    transport.guard.discovery_check = check
    monkeypatch.setattr(compiler.asyncio, "sleep", wait)
    with pytest.raises(StopDiscovery):
        await transport.guard.reserve_model_call(100, CallSpec("privacy", "local_model", "generic"),
                                                 after_response=True, minimum_seconds=60)
    assert attempts == ["reservation"]


@pytest.mark.asyncio
async def test_provenance_receives_actual_masked_records_and_meter_call_id(tmp_path, monkeypatch, hybrid):
    mock = scanner(monkeypatch, ["new-synthetic-value"])
    ledger = PrivacyLedger(tmp_path)
    ledger.remember([{"value": "old-synthetic-value", "type": "SECRET"}])
    observed = []
    call_ids = []
    context = {"build_id": str(uuid4()), "unit_id": 2, "stage": "compile"}

    def remember_scan(items, records, **kwargs):
        observed.append((items, records, kwargs))
        ledger.remember(items)

    monkeypatch.setattr(ledger, "remember_scan", remember_scan, raising=False)

    def reserve(call_id, *args):
        call_ids.append(call_id)
        return call_id

    meter = SimpleNamespace(reserve=reserve, settle=lambda *args: None)
    guard = CompilationGuard(lambda *args: None, lambda *args: None, ledger, call_meter=meter)
    guard.discovery_context = context
    try:
        await guard.clean(encoded(["old-synthetic-value and new-synthetic-value"]))
    finally:
        await guard.close()
    assert len(observed) == len(call_ids) == len(mock.calls) == 1
    items, records, metadata = observed[0]
    assert items == [{"value": "new-synthetic-value", "type": "SECRET"}]
    assert "old-synthetic-value" not in encoded(records)
    assert "<<SECRET_" in encoded(records)
    assert metadata["call_id"] == call_ids[0]
    assert metadata["context"] == context
    assert set(metadata) == {"call_id", "scope", "packet", "start", "context", "after_response"}
    transmitted = json.loads(mock.calls[0])
    assert all(record["text"] in transmitted[record["field"]] for record in records)
