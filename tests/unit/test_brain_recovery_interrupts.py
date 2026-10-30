"""Synthetic cancellation and concurrent-index recovery checks; no paid I/O."""

from concurrent.futures import ThreadPoolExecutor
import inspect
from pathlib import Path
import secrets
import socket
from threading import Event
from uuid import uuid4

from billiard.exceptions import SoftTimeLimitExceeded
import pytest

from src.core.project_brain import recovery, recovery_privacy
from src.core.project_brain.privacy import PrivacyLedger
from src.core.project_brain.privacy_history import HistoryCandidate, PrivacyHistory
from src.core.project_brain.privacy_seal import PrivacySealStore
from src.core.project_brain.recovery_privacy import FrozenPrivacyReceipts, ReceiptParentScope, RecoveryPrivacyError
from tests.unit.test_brain_recovery_privacy import COMPONENT, SHA, environment, freeze, lookup


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    original = socket.socket.connect

    def forbidden(sock, address):
        # Windows asyncio builds its private wake-up socketpair via loopback.
        caller = inspect.currentframe().f_back
        if (caller.f_globals.get("__name__") == "socket"
                and caller.f_code.co_name in {"socketpair", "_fallback_socketpair"}
                and isinstance(address, tuple) and address[0] == "127.0.0.1"):
            return original(sock, address)
        raise AssertionError("external I/O forbidden in synthetic interrupt test")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


def _interrupt():
    raise SoftTimeLimitExceeded()


def _interrupted_operation(case, frozen, phase, monkeypatch):
    if phase == "freeze":
        monkeypatch.setattr(recovery_privacy, "_read", lambda *a, **k: _interrupt())
        return lambda: freeze(case)
    if phase == "reopen":
        monkeypatch.setattr(FrozenPrivacyReceipts, "freeze", classmethod(lambda *a, **k: _interrupt()))
        return lambda: FrozenPrivacyReceipts.reopen(frozen.manifest(), expected_scope=case["scope"],
            **{k: v for k, v in case["kwargs"].items() if k != "expected_history_digest"})
    monkeypatch.setattr(FrozenPrivacyReceipts, "assert_current", lambda *a: _interrupt())
    return lambda: lookup(frozen, case)


@pytest.mark.parametrize("phase", ["freeze", "reopen", "lookup"])
def test_soft_limit_keeps_control_flow_identity_in_each_privacy_wrapper(tmp_path, monkeypatch, phase):
    case = environment(tmp_path)
    frozen = freeze(case)
    operation = _interrupted_operation(case, frozen, phase, monkeypatch)
    with pytest.raises(SoftTimeLimitExceeded):
        operation()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["freeze", "reopen", "lookup"])
async def test_soft_limit_is_not_folded_into_source_failure_by_recovery(tmp_path, monkeypatch, phase):
    case = environment(tmp_path)
    frozen = freeze(case)
    operation = _interrupted_operation(case, frozen, phase, monkeypatch)

    async def original(*args):
        return operation()

    async def execute(original, *args):
        # Exercise the actual execute_recoverable exception boundary without DB.
        return await original(*args)

    monkeypatch.setattr(recovery, "_execute_recovery", execute)
    with pytest.raises(SoftTimeLimitExceeded):
        await recovery.execute_recoverable(original, None, None, None, None)


def test_valid_sibling_write_inside_index_read_window_disables_reuse(tmp_path, monkeypatch):
    case = environment(tmp_path)
    frozen = freeze(case)
    current = case["current"]
    sibling = PrivacyLedger(tmp_path / "child", create=False,
        seal_directory=tmp_path / "child-seal", require_index=True)
    refreshed, writing, written = Event(), Event(), Event()
    original_refresh = current.refresh
    original_assert = FrozenPrivacyReceipts.assert_current
    refreshes = checks = 0

    def write_discovery():
        assert refreshed.wait(5)
        writing.set()
        try:
            sibling.remember_policy([{"value": "ordinary", "type": "SECRET"}], [case["record"]],
                kind="deterministic_rule", scope=sibling.digest("concurrent-policy"), context=case["context"])
        finally:
            written.set()

    def refresh_with_interleave():
        nonlocal refreshes
        original_refresh()
        refreshes += 1
        if refreshes == 1:
            refreshed.set()
            assert writing.wait(5)
            # Old code allows this transaction between its two index reads.
            # Correct code holds the existing index lock, so the writer waits.
            written.wait(0.5)

    def check_after_write(self):
        nonlocal checks
        checks += 1
        if checks == 2:
            assert written.wait(5)
        return original_assert(self)

    monkeypatch.setattr(current, "refresh", refresh_with_interleave)
    monkeypatch.setattr(FrozenPrivacyReceipts, "assert_current", check_after_write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(write_discovery)
        try:
            assert lookup(frozen, case) is None
        finally:
            refreshed.set()
            future.result(timeout=5)
    original_refresh()
    assert current.entries
    assert frozen.manifest()["parent_revision"] == case["parent"]._revision()
    assert "ordinary" not in current.mask(case["record"]["text"])


@pytest.mark.parametrize("mutation", ["index_mac", "sealed", "foreign_scope"])
def test_consistent_current_snapshot_does_not_relax_authentication(tmp_path, mutation):
    import json

    case = environment(tmp_path)
    frozen = freeze(case)
    current = case["current"]
    if mutation == "index_mac":
        value = json.loads(current.index.read_text())
        value["index_hmac"] = "0" * 64
        current.index.write_text(json.dumps(value))
    elif mutation == "sealed":
        current.seal(current.revision(), "f" * 64)
    else:
        current.remember_policy([{"value": "ordinary", "type": "SECRET"}], [case["record"]],
            kind="deterministic_rule", scope=current.digest("wrong-scope"),
            context={**case["context"], "build_id": case["scope"].build_id})
    with pytest.raises(RecoveryPrivacyError):
        frozen.lookup(case["record"], current=current, history=case["history"],
            privacy_component_digest=COMPONENT)


def _sealed_history_case(tmp_path):
    """A real sealed history snapshot so the lower audit wrapper executes."""
    parent_id, ancestor_id = str(uuid4()), str(uuid4())
    project_key = secrets.token_bytes(32)
    ancestor = PrivacyLedger(tmp_path / "ancestor", seal_directory=tmp_path / "ancestor-seal", require_index=True)
    record = {"field": "source_excerpt", "text": "synthetic_audit_secret"}
    ancestor.remember_policy([{"value": record["text"], "type": "SECRET"}], [record],
        kind="deterministic_rule", scope=ancestor.digest("policy"),
        context={"build_id": ancestor_id, "unit_id": 1, "stage": "privacy"})
    ancestor.seal(ancestor.revision(), "f" * 64)
    audit = tmp_path / "retained-ancestor"
    ancestor.retain_audit(audit)
    baseline = PrivacyHistory(tmp_path, 480, parent_id, SHA, project_key, create=True,
        candidates=[HistoryCandidate(480, ancestor_id, SHA, audit, sealed_audit=True, require_index=True)])
    parent = PrivacyLedger(tmp_path / "parent", seal_directory=tmp_path / "parent-seal", require_index=True)
    return {
        "scope": ReceiptParentScope(480, parent_id, SHA, "c" * 64, COMPONENT),
        "kwargs": {"directory": tmp_path / "parent", "seal_directory": tmp_path / "parent-seal",
            "history_base": tmp_path, "project_key": project_key, "expected_history_digest": baseline.digest},
        "baseline": baseline, "parent": parent,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["freeze", "execute"])
async def test_actual_history_audit_wrapping_does_not_hide_soft_limit(tmp_path, monkeypatch, boundary):
    case = _sealed_history_case(tmp_path)
    original = PrivacySealStore.authenticate_documents
    interrupt = SoftTimeLimitExceeded()
    calls = []

    def interrupt_history_audit(self, *args, **kwargs):
        if self.root == Path("."):
            calls.append("sealed_history_audit")
            raise interrupt
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PrivacySealStore, "authenticate_documents", interrupt_history_audit)
    with pytest.raises(SoftTimeLimitExceeded) as caught:
        if boundary == "freeze":
            freeze(case)
        else:
            async def original_execute(*args):
                return freeze(case)

            async def execute(original, *args):
                return await original(*args)

            monkeypatch.setattr(recovery, "_execute_recovery", execute)
            await recovery.execute_recoverable(original_execute, None, None, None, None)
    assert caught.value is interrupt
    assert calls == ["sealed_history_audit"]


def test_damaged_real_history_audit_keeps_closed_failure(tmp_path, monkeypatch):
    from src.core.project_brain.exceptions import PrivacySealConflict

    case = _sealed_history_case(tmp_path)
    original = PrivacySealStore.authenticate_documents

    def reject_history_audit(self, *args, **kwargs):
        if self.root == Path("."):
            raise PrivacySealConflict()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PrivacySealStore, "authenticate_documents", reject_history_audit)
    with pytest.raises(RecoveryPrivacyError, match="recovery_privacy_invalid"):
        freeze(case)


@pytest.mark.parametrize("phase", ["freeze", "reopen", "lookup"])
def test_previously_wrapped_recovery_error_keeps_real_interrupt_identity(tmp_path, monkeypatch, phase):
    case = environment(tmp_path)
    frozen = freeze(case)
    interrupt = SoftTimeLimitExceeded()
    wrapped = RecoveryPrivacyError()
    wrapped.__context__ = interrupt

    def fail(*args, **kwargs):
        raise wrapped

    if phase == "freeze":
        monkeypatch.setattr(recovery_privacy, "_read", fail)
        operation = lambda: freeze(case)
    elif phase == "reopen":
        monkeypatch.setattr(FrozenPrivacyReceipts, "freeze", classmethod(fail))
        operation = lambda: FrozenPrivacyReceipts.reopen(frozen.manifest(), expected_scope=case["scope"],
            **{k: v for k, v in case["kwargs"].items() if k != "expected_history_digest"})
    else:
        monkeypatch.setattr(FrozenPrivacyReceipts, "assert_current", fail)
        operation = lambda: lookup(frozen, case)
    with pytest.raises(SoftTimeLimitExceeded) as caught:
        operation()
    assert caught.value is interrupt


@pytest.mark.parametrize("chain", ["cycle", "too_deep", "misleading_type", "normal_recovery"])
def test_exception_chain_search_is_bounded_and_fails_closed(tmp_path, monkeypatch, chain):
    case = environment(tmp_path)
    if chain == "cycle":
        error, second = ValueError(), RuntimeError()
        error.__context__, second.__cause__ = second, error
    elif chain == "too_deep":
        error = SoftTimeLimitExceeded()
        for _ in range(17):
            wrapper = ValueError()
            wrapper.__context__, error = error, wrapper
    elif chain == "misleading_type":
        # The class name and text do not establish a billiard control signal.
        error = type("SoftTimeLimitExceeded", (ValueError,), {})("SoftTimeLimitExceeded")
    else:
        error = RecoveryPrivacyError()
        error.__context__ = ValueError()

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(recovery_privacy, "_read", fail)
    with pytest.raises(RecoveryPrivacyError, match="recovery_privacy_invalid") as caught:
        freeze(case)
    if chain == "normal_recovery":
        assert caught.value is error


def test_interrupt_search_follows_real_cause_without_reading_error_payload():
    class OpaqueFailure(ValueError):
        def __str__(self):
            raise AssertionError("exception payload must not be inspected")

        def __repr__(self):
            raise AssertionError("exception payload must not be inspected")

    interrupt = SoftTimeLimitExceeded()
    error = OpaqueFailure()
    error.__cause__, error.__context__ = interrupt, error
    with pytest.raises(SoftTimeLimitExceeded) as caught:
        recovery_privacy._raise_wrapped_interrupt(error)
    assert caught.value is interrupt
