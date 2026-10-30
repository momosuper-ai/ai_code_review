"""Repeated authentication may reuse parsed bytes, never file timestamps.

All inputs are synthetic privacy ledgers. The count test is a performance
regression signal: each boundary still checks fresh complete file content.
"""

import os
import socket

from billiard.exceptions import SoftTimeLimitExceeded
import pytest

from src.core.project_brain import recovery_privacy
from src.core.project_brain.recovery_privacy import FrozenPrivacyReceipts, RecoveryPrivacyError
from tests.unit.test_brain_recovery_privacy import environment
from tests.unit.test_brain_recovery_interrupts import _sealed_history_case


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden in synthetic authentication test")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def _freeze(case):
    return FrozenPrivacyReceipts.freeze(case["scope"], **case["kwargs"])


def _same_metadata_write(path, content):
    """An unchanged size/mtime is deliberately not an authenticity proof."""
    before = path.stat()
    assert len(content) == before.st_size
    path.write_bytes(content)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_unchanged_parent_checks_do_not_redecode_already_authenticated_ledger(tmp_path, monkeypatch):
    """Fresh content fences should not rebuild the same cryptographic parse."""
    case = environment(tmp_path, positive=True)
    frozen = _freeze(case)
    original = recovery_privacy._checked_ledger
    decodes = []

    def checked(*args, **kwargs):
        decodes.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(recovery_privacy, "_checked_ledger", checked)
    for _ in range(3):
        frozen.assert_current()
    assert len(decodes) == 0, "unchanged parent bytes were fully re-decoded at every pre-I/O fence"


@pytest.mark.parametrize("material", ["key", "index", "state", "history", "receipt"])
def test_frozen_parent_rejects_same_size_same_mtime_content_change(tmp_path, material):
    case = environment(tmp_path, positive=True)
    frozen = _freeze(case)
    # Warm any parsed-content cache before altering one authenticated material.
    frozen.assert_current()
    parent = case["parent"]
    if material == "key":
        path = parent.root / "key"
        before = path.read_bytes()
        after = bytes([before[0] ^ 1]) + before[1:]
    elif material == "index":
        path = parent.index
        before = path.read_bytes()
        index = before.index(b'"index_hmac"')
        marker = before.index(b'"', before.index(b":", index) + 1) + 1
        after = before[:marker] + (b"1" if before[marker:marker+1] != b"1" else b"2") + before[marker+1:]
    elif material == "state":
        path = case["kwargs"]["seal_directory"] / "state.json"
        before = path.read_bytes()
        marker = next(index for index, value in enumerate(before) if 97 <= value <= 102)
        after = before[:marker] + (b"1" if before[marker:marker+1] != b"1" else b"2") + before[marker+1:]
    elif material == "history":
        path = case["baseline"].path
        before = path.read_bytes()
        index = before.index(b'"binding_hmac"')
        marker = before.index(b'"', before.index(b":", index) + 1) + 1
        after = before[:marker] + (b"1" if before[marker:marker+1] != b"1" else b"2") + before[marker+1:]
    else:
        path = parent.root / (case["receipt"] + ".json")
        before = path.read_bytes()
        after = b'"split"' + b" " * (len(before) - len(b'"split"'))
    _same_metadata_write(path, after)
    with pytest.raises(RecoveryPrivacyError):
        frozen.assert_current()


def test_frozen_parent_rejects_new_receipt_after_cache_warmup(tmp_path):
    case = environment(tmp_path, positive=True)
    frozen = _freeze(case)
    frozen.assert_current()
    case["parent"].complete("f" * 64)
    with pytest.raises(RecoveryPrivacyError, match="recovery_privacy_parent_changed"):
        frozen.assert_current()


def test_frozen_parent_rejects_valid_old_index_with_restored_size_and_mtime(tmp_path):
    case = environment(tmp_path, positive=True)
    parent = case["parent"]
    old = parent.index.read_bytes()
    record = {"field": "source_excerpt", "text": "synthetic second protected value"}
    parent.remember_policy([{"value": "second", "type": "SECRET"}], [record],
        kind="deterministic_rule", scope=parent.digest("new-policy"),
        context={"build_id": case["scope"].build_id, "unit_id": 1, "stage": "privacy"})
    frozen = _freeze(case)
    frozen.assert_current()
    current_size = parent.index.stat().st_size
    assert current_size > len(old)
    _same_metadata_write(parent.index, old + b" " * (current_size - len(old)))
    with pytest.raises(RecoveryPrivacyError, match="recovery_privacy_parent_changed"):
        frozen.assert_current()


@pytest.mark.parametrize("name", ["key", "fingerprints.json", "manifest.json", "state.json", "seal.json"])
def test_all_referenced_history_snapshot_bytes_are_fenced_after_warmup(tmp_path, name):
    case = _sealed_history_case(tmp_path)
    frozen = _freeze(case)
    frozen.assert_current()
    baseline = case["baseline"]
    snapshot = baseline.root / "snapshots" / baseline.parents[0].snapshot_id
    path = snapshot / name
    before = path.read_bytes()
    # Same length and timestamps, including for the binary build key.
    after = bytes([before[0] ^ 1]) + before[1:]
    _same_metadata_write(path, after)
    with pytest.raises(RecoveryPrivacyError):
        frozen.assert_current()


def test_history_snapshot_inventory_is_fenced_after_warmup(tmp_path):
    case = _sealed_history_case(tmp_path)
    frozen = _freeze(case)
    frozen.assert_current()
    baseline = case["baseline"]
    snapshot = baseline.root / "snapshots" / baseline.parents[0].snapshot_id
    (snapshot / "extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RecoveryPrivacyError):
        frozen.assert_current()


def test_second_content_capture_detects_a_late_receipt(tmp_path, monkeypatch):
    case = environment(tmp_path, positive=True)
    frozen = _freeze(case)
    original = recovery_privacy._content_fence
    captures = []

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        captures.append(True)
        if len(captures) == 1:
            case["parent"].complete("f" * 64)
        return result

    monkeypatch.setattr(recovery_privacy, "_content_fence", capture)
    with pytest.raises(RecoveryPrivacyError, match="recovery_privacy_parent_changed"):
        frozen.assert_current()
    assert len(captures) == 2


def test_unavailable_content_enumeration_keeps_full_authentication(tmp_path, monkeypatch):
    case = environment(tmp_path, positive=True)
    original = recovery_privacy._checked_ledger
    decodes = []

    def unsupported(*args, **kwargs):
        raise ValueError("synthetic dependency enumeration unavailable")

    def checked(*args, **kwargs):
        decodes.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(recovery_privacy, "_content_fence", unsupported)
    frozen = _freeze(case)
    assert frozen._content_fence is None
    monkeypatch.setattr(recovery_privacy, "_checked_ledger", checked)
    frozen.assert_current()
    frozen.assert_current()
    assert len(decodes) == 2


def test_parent_sealed_after_warmup_is_not_accepted_as_same_proof(tmp_path):
    case = environment(tmp_path, positive=True)
    frozen = _freeze(case)
    frozen.assert_current()
    parent = case["parent"]
    parent.seal(parent.revision(), "f" * 64)
    with pytest.raises(RecoveryPrivacyError, match="recovery_privacy_parent_changed"):
        frozen.assert_current()


@pytest.mark.parametrize("material", ["index", "history"])
def test_fast_validation_rejects_missing_material(tmp_path, material):
    case = environment(tmp_path, positive=True)
    frozen = _freeze(case)
    frozen.assert_current()
    path = case["parent"].index if material == "index" else case["baseline"].path
    path.unlink()
    with pytest.raises(RecoveryPrivacyError):
        frozen.assert_current()


@pytest.mark.parametrize("material", ["parent_key", "history_index"])
def test_fast_validation_rejects_symlink_with_identical_bytes(tmp_path, material):
    case = _sealed_history_case(tmp_path)
    frozen = _freeze(case)
    frozen.assert_current()
    if material == "parent_key":
        path = case["parent"].root / "key"
    else:
        baseline = case["baseline"]
        path = baseline.root / "snapshots" / baseline.parents[0].snapshot_id / "fingerprints.json"
    target = tmp_path / "synthetic-link-target"
    target.write_bytes(path.read_bytes())
    original = path.read_bytes()
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        path.write_bytes(original)
        pytest.skip("this Windows test process cannot create symlinks")
    with pytest.raises(RecoveryPrivacyError):
        frozen.assert_current()


def test_fast_validation_preserves_wrapped_soft_limit_identity(tmp_path, monkeypatch):
    case = environment(tmp_path, positive=True)
    frozen = _freeze(case)
    interrupt = SoftTimeLimitExceeded()
    wrapped = ValueError("synthetic wrapped cancellation")
    wrapped.__context__ = interrupt

    def stopped(*args, **kwargs):
        raise wrapped

    monkeypatch.setattr(recovery_privacy, "_read", stopped)
    with pytest.raises(SoftTimeLimitExceeded) as caught:
        frozen.assert_current()
    assert caught.value is interrupt


@pytest.mark.parametrize("material", ["index", "receipt"])
def test_freeze_rejects_aba_between_content_capture_and_full_authentication(tmp_path, monkeypatch, material):
    """The cached A bytes must not accompany a ledger authenticated from B."""
    case = environment(tmp_path, positive=True)
    parent = case["parent"]
    path = parent.index if material == "index" else parent.root / (case["receipt"] + ".json")
    original_bytes = path.read_bytes()
    original_capture = recovery_privacy._content_fence
    captures = []

    def changed_between_captures(*args, **kwargs):
        captures.append(True)
        if len(captures) == 1:
            captured = original_capture(*args, **kwargs)
            if material == "index":
                parent.remember_policy([{"value": "second", "type": "SECRET"}],
                    [{"field": "source_excerpt", "text": "synthetic second value"}],
                    kind="deterministic_rule", scope=parent.digest("between-fences"),
                    context={"build_id": case["scope"].build_id, "unit_id": 1, "stage": "privacy"})
            else:
                parent.complete(case["receipt"], "split")
            return captured
        # Full authentication read B. Return the original on-disk A bytes just
        # before the final capture, so merely comparing A == A is insufficient.
        path.write_bytes(original_bytes)
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(recovery_privacy, "_content_fence", changed_between_captures)
    with pytest.raises(RecoveryPrivacyError, match="recovery_privacy_parent_changed"):
        _freeze(case)
