"""Real privacy guard plus authenticated synthetic recovery parents, offline."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import inspect
import socket

from openai.types.chat import ChatCompletionMessage
import pytest

from src.config import settings
from src.core.llm_client import ChatResult, TokenUsage
from src.core.project_brain.compiler import CompilationGuard, encoded
from src.core.project_brain import recovery
from src.core.project_brain.recovery_privacy import FrozenPrivacyReceipts, RecoveryPrivacyError
from tests.unit.test_brain_recovery_privacy import COMPONENT, SECRET, environment


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    original_connect = socket.socket.connect
    def forbidden(sock, address):
        # Windows asyncio creates its internal socketpair through socket.py's
        # loopback fallback. Permit that exact stdlib caller, never SDK/DB I/O.
        caller = inspect.currentframe().f_back
        if (caller.f_globals.get("__name__") == "socket"
                and caller.f_code.co_name in {"socketpair", "_fallback_socketpair"}
                and isinstance(address, tuple) and address[0] == "127.0.0.1"):
            return original_connect(sock, address)
        raise AssertionError("real network forbidden in recovery guard test")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def guard_case(tmp_path, monkeypatch, *, positive=False):
    case = environment(tmp_path, positive=positive)
    # A plain guard input has the semantic role 'value', with no object-key
    # records. Its parent receipt is created by the same real ledger protocol.
    record = {"field": "value", "text": case["record"]["text"]}
    receipt = case["parent"].digest("record:v2:" + encoded(record))
    case["parent"].complete(receipt)
    frozen = FrozenPrivacyReceipts.freeze(case["scope"], **case["kwargs"])
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    monkeypatch.setattr(settings, "PRIVACY_FILTER_LEVEL", "minimal")
    monkeypatch.setattr(settings, "PRIVACY_CUSTOM_PATTERNS", "")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_RECORDS_PER_PACKET", 32)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT", 8)
    local = SimpleNamespace(chat=AsyncMock(side_effect=AssertionError("cached input must never call a model")),
                            aclose=AsyncMock())
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", lambda *args, **kwargs: local)
    accounts, reservations = [], []
    guard = CompilationGuard(lambda *args, **kwargs: accounts.append((args, kwargs)), reservations.append,
        case["current"], discovery_context=case["context"])
    guard.privacy_history = case["history"]
    guard.persist_rule_findings = True
    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr("src.core.project_brain.lifecycle.SyncSession", lambda: session)
    body = {"components": {"privacy": COMPONENT}}
    monkeypatch.setattr(recovery, "load_recovery", lambda *args: (body, [frozen]))
    child = SimpleNamespace(id=case["history"].scope["build_id"])
    project = SimpleNamespace(gitlab_project_id=480)
    return SimpleNamespace(case=case, frozen=frozen, guard=guard, local=local, accounts=accounts,
        reservations=reservations, child=child, project=project, store=SimpleNamespace(), record=record)


def bind(test):
    return recovery.bind_guard(test.guard, test.child, test.project, test.store)


def allow_empty_synthetic_scan(test):
    test.local.chat.side_effect = None
    test.local.chat.return_value = ChatResult(ChatCompletionMessage(role="assistant", content='{"items":[]}'),
                                             TokenUsage(20, 5), "stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("positive", [False, True])
async def test_completed_parent_input_skips_model_without_copying_calls_or_child_receipts(tmp_path, monkeypatch, positive):
    test = guard_case(tmp_path, monkeypatch, positive=positive)
    guard = bind(test)
    child_before = test.case["current"].index.read_bytes()
    parent_before = test.case["parent"].index.read_bytes()
    original_parent_calls = dict(test.case["parent"].origins["calls"])
    try:
        safe = await guard.clean(test.record["text"])
        assert test.local.chat.await_count == 0 and not test.accounts
        assert all(amount == 0 for amount in test.reservations)  # lease-only fence, no paid reservation
        assert guard.calls == 0 and guard.recovery_receipt_hits == 1
        assert guard.cache_hits == 1
        assert test.case["current"].origins["calls"] == {}
        assert test.case["parent"].origins["calls"] == original_parent_calls
        assert test.case["parent"].index.read_bytes() == parent_before
        assert test.case["current"].index.read_bytes() == child_before
        assert not any(len(path.stem) == 64 for path in test.case["current"].root.glob("*.json"))
        assert guard.ledger.external == set()  # lease-local view was restored
        if positive:
            assert SECRET not in safe and "<<SECRET_" in safe
        else:
            assert safe == test.record["text"]
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_new_text_uses_real_guard_scan_path_with_only_a_synthetic_local_client(tmp_path, monkeypatch):
    test = guard_case(tmp_path, monkeypatch)
    allow_empty_synthetic_scan(test)
    guard = bind(test)
    new_text = "independently new synthetic declaration"
    try:
        assert await guard.clean(new_text) == new_text
        assert test.local.chat.await_count == 1 and guard.calls == 1
        assert len(test.accounts) == 1 and test.reservations
        assert guard.recovery_receipt_hits == 0
        current_key = test.case["current"].digest("record:v2:" + encoded({"field": "value", "text": new_text}))
        assert test.case["current"].receipt(current_key) == "completed"
        assert test.case["current"].origins["calls"] == {}  # empty finding, no invented old call
        assert test.case["parent"].receipt(test.case["parent"].digest("record:v2:" + encoded({"field": "value", "text": new_text}))) is None
    finally:
        await guard.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_discovery_check", [False, True])
async def test_parent_mutation_blocks_even_when_source_identity_replaces_discovery_callback(tmp_path, monkeypatch, replace_discovery_check):
    test = guard_case(tmp_path, monkeypatch)
    guard = bind(test)
    if replace_discovery_check:
        # Mirrors the catalog's independent source-identity callback. The
        # ledger view's refresh must retain the parent fence on its own.
        guard.discovery_check = lambda: None
    test.case["parent"].complete("f" * 64)
    try:
        with pytest.raises(RecoveryPrivacyError, match="recovery_privacy_parent_changed"):
            await guard.clean(test.record["text"])
        assert test.local.chat.await_count == 0 and not test.accounts and not test.reservations
        assert test.case["current"].origins["calls"] == {}
    finally:
        await guard.close()


def test_ledger_view_drops_external_markers_on_current_revision_change(tmp_path, monkeypatch):
    test = guard_case(tmp_path, monkeypatch)
    view = recovery._LedgerView(test.case["current"], [test.frozen])
    key = view.digest("record:v2:" + encoded(test.record))
    view.external = {key}
    view.external_revision = view.ledger._revision()
    view.refresh()
    assert view.receipt(key) == "completed"
    view.ledger.remember_policy([{"value": "ordinary", "type": "SECRET"}], [test.record],
        kind="deterministic_rule", scope=view.digest("new-rule"), context=test.case["context"])
    view.refresh()
    assert view.external == set() and view.receipt(key) is None
    assert test.case["current"].origins["calls"] == {}


@pytest.mark.asyncio
async def test_late_current_discovery_between_lookup_and_scan_invalidates_external_hit(tmp_path, monkeypatch):
    test = guard_case(tmp_path, monkeypatch)
    original = test.guard.clean
    allow_empty_synthetic_scan(test)
    async def discover_before_original(text):
        test.case["current"].remember_policy([{"value": "ordinary", "type": "SECRET"}], [test.record],
            kind="deterministic_rule", scope=test.case["current"].digest("late-rule"), context=test.case["context"])
        return await original(text)
    test.guard.clean = discover_before_original
    guard = bind(test)
    try:
        safe = await guard.clean(test.record["text"])
        assert test.local.chat.await_count == 1 and guard.calls == 1
        assert "ordinary" not in safe
        assert guard.ledger.external == set()
        assert test.case["current"].origins["calls"] == {}
        assert test.case["parent"].mask("ordinary") == "ordinary"
    finally:
        await guard.close()
