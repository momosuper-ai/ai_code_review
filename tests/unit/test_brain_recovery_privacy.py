"""Offline synthetic parent receipt reuse; no SDK, network or trial source."""

from dataclasses import replace
import json
from pathlib import Path
import secrets
import socket
from uuid import uuid4

import pytest

from src.core.project_brain.privacy import PrivacyLedger
from src.core.project_brain.privacy_history import HistoryCandidate, PrivacyHistory
from src.core.project_brain.privacy_provenance import canonical_json
from src.core.project_brain.recovery_privacy import (
    FrozenPrivacyReceipts, ReceiptParentScope, RecoveryPrivacyError,
)
from src.core.project_brain import recovery_privacy

SECRET = "synthetic_recovery_old_secret"
NEW_SECRET = "synthetic_recovery_later_secret"
COMPONENT = "a" * 64
SHA = "b" * 40


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline synthetic test")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def snapshot_files(base):
    return {path.relative_to(base).as_posix(): path.read_bytes() for path in base.rglob("*") if path.is_file()}


def environment(tmp_path, *, positive=False, completed=True):
    parent_id, child_id = str(uuid4()), str(uuid4())
    project_key = secrets.token_bytes(32)
    parent_dir, parent_seal = tmp_path / "parent", tmp_path / "parent-seal"
    parent = PrivacyLedger(parent_dir, seal_directory=parent_seal, require_index=True)
    baseline = PrivacyHistory(tmp_path, 480, parent_id, SHA, project_key, create=True)
    record = {"field": "source_excerpt", "text": "ordinary declaration " + SECRET if positive else "ordinary declaration"}
    if positive:
        parent.remember_scan([{"value": SECRET, "type": "SECRET"}], [record], call_id=str(uuid4()),
            scope=parent.digest("scope"), packet=parent.digest("packet"),
            context={"build_id": parent_id, "unit_id": 1, "stage": "privacy"})
    receipt = parent.digest("record:v2:" + canonical_json(record))
    parent.complete(receipt, "completed" if completed else "split")
    scope = ReceiptParentScope(480, parent_id, SHA, "c" * 64, COMPONENT)
    history = PrivacyHistory(tmp_path, 480, child_id, SHA, project_key, create=True, candidates=[
        HistoryCandidate(480, parent_id, SHA, parent_dir, require_index=True)])
    current = PrivacyLedger(tmp_path / "child", seal_directory=tmp_path / "child-seal", require_index=True)
    context = {"build_id": child_id, "unit_id": 2, "stage": "privacy"}
    history.apply([record], current, scope=current.digest("scope"), context=context)
    kwargs = dict(directory=parent_dir, seal_directory=parent_seal, history_base=tmp_path,
                  project_key=project_key, expected_history_digest=baseline.digest)
    return dict(parent=parent, baseline=baseline, scope=scope, history=history, current=current,
                record=record, receipt=receipt, context=context, kwargs=kwargs)


def freeze(case):
    return FrozenPrivacyReceipts.freeze(case["scope"], **case["kwargs"])


def lookup(frozen, case, record=None, component=COMPONENT):
    return frozen.lookup(record or case["record"], current=case["current"], history=case["history"],
                         privacy_component_digest=component)


def test_exact_text_and_semantic_field_reuse_is_read_only(tmp_path):
    case = environment(tmp_path)
    before = snapshot_files(tmp_path)
    frozen = freeze(case)
    proof = lookup(frozen, case)
    frozen.assert_current()
    assert proof.parent_build_id == case["scope"].build_id and proof.parent_receipt == case["receipt"]
    assert proof.safe_record == case["record"]
    assert snapshot_files(tmp_path) == before
    assert case["current"].receipt(case["current"].digest("record:v2:" + canonical_json(case["record"]))) is None
    assert case["current"].origins["calls"] == {}


def test_positive_receipt_reuses_across_keys_and_reapplies_current_hmac(tmp_path):
    case = environment(tmp_path, positive=True)
    original_parent = snapshot_files(tmp_path / "parent")
    original_current = case["current"].index.read_bytes()
    frozen = freeze(case)
    assert case["parent"].key != case["current"].key
    assert case["parent"].mask(SECRET) != case["current"].mask(SECRET)
    proof = lookup(frozen, case)
    assert proof is not None and SECRET not in proof.safe_record["text"]
    assert case["current"].index.read_bytes() == original_current
    assert snapshot_files(tmp_path / "parent") == original_parent
    assert case["current"].origins["calls"] == {}
    assert case["parent"].origins["calls"]
    assert SECRET not in json.dumps(frozen.manifest()) and SECRET not in repr(frozen) + repr(proof)


@pytest.mark.parametrize("change", [{"field": "semantic_output"}, {"text": "different declaration"},
                                  {"text": "ordinary declaration "}, {"field": "SOURCE_EXCERPT"}])
def test_text_and_role_are_exact_not_name_or_coarse_role_matches(tmp_path, change):
    case = environment(tmp_path)
    assert lookup(freeze(case), case, dict(case["record"], **change)) is None


def test_split_is_not_completion_and_record_receipt_cannot_be_inferred_from_scope(tmp_path):
    case = environment(tmp_path, completed=False)
    case["parent"].complete(case["parent"].digest("input:" + canonical_json(case["record"])))
    assert lookup(freeze(case), case) is None


@pytest.mark.parametrize("change", [{"state": "ready"}, {"state": "running"}, {"active_leases": 1},
                                  {"active_leases": False}, {"project_id": True}, {"recipe_hash": "bad"}])
def test_only_failed_parents_without_active_leases_are_accepted(tmp_path, change):
    case = environment(tmp_path)
    with pytest.raises(RecoveryPrivacyError):
        FrozenPrivacyReceipts.freeze(replace(case["scope"], **change), **case["kwargs"])


def test_component_change_rejects_whole_adapter_even_for_same_record(tmp_path):
    case = environment(tmp_path)
    with pytest.raises(RecoveryPrivacyError, match="recovery_privacy_component_mismatch"):
        lookup(freeze(case), case, component="d" * 64)


def test_persistent_manifest_reopens_exact_parent_without_writes(tmp_path):
    case = environment(tmp_path, positive=True)
    frozen = freeze(case)
    manifest = json.loads(json.dumps(frozen.manifest()))
    before = snapshot_files(tmp_path)
    reopened = FrozenPrivacyReceipts.reopen(manifest, expected_scope=case["scope"],
        **{key: value for key, value in case["kwargs"].items() if key != "expected_history_digest"})
    assert reopened.manifest() == manifest and lookup(reopened, case) is not None
    assert snapshot_files(tmp_path) == before
    manifest["completed_receipt_count"] += 1
    assert frozen.manifest()["completed_receipt_count"] == 1
    with pytest.raises(RecoveryPrivacyError):
        FrozenPrivacyReceipts.reopen(manifest, expected_scope=case["scope"],
            **{key: value for key, value in case["kwargs"].items() if key != "expected_history_digest"})


def test_detached_ledger_view_does_not_let_origin_validator_mutate_frozen_parent(tmp_path):
    case = environment(tmp_path, positive=True)
    frozen = freeze(case)
    view = frozen.ledger
    view.entries.clear()
    view.origins["calls"].clear()
    assert frozen.ledger.entries and frozen.ledger.origins["calls"]
    assert lookup(frozen, case) is not None


def test_large_lookup_is_authenticated_per_batch_not_per_record(tmp_path, monkeypatch):
    case = environment(tmp_path)
    frozen = freeze(case)
    monkeypatch.setattr(recovery_privacy, "MAX_REUSE_RECORDS", 2)
    original = FrozenPrivacyReceipts.assert_current
    checks = 0
    def checked(self):
        nonlocal checks
        checks += 1
        return original(self)
    monkeypatch.setattr(FrozenPrivacyReceipts, "assert_current", checked)
    results = frozen.lookup_records([dict(case["record"]) for _ in range(5)], current=case["current"],
        history=case["history"], privacy_component_digest=COMPONENT)
    assert len(results) == 5 and all(hit is not None for hit in results)
    assert checks == 6  # before/after each of three batches, not ten checks


@pytest.mark.parametrize("mutation", ["key", "index_mac", "missing_index", "legacy_index", "state_mac", "history_mac", "receipt_body"])
def test_all_parent_materials_are_required_and_authenticated(tmp_path, mutation):
    case = environment(tmp_path, positive=True)
    parent = case["parent"]
    if mutation == "key": (parent.root / "key").write_bytes(b"x" * 32)
    elif mutation == "index_mac":
        data = json.loads(parent.index.read_text())
        data["index_hmac"] = "0" * 64
        parent.index.write_text(json.dumps(data))
    elif mutation == "missing_index": parent.index.unlink()
    elif mutation == "legacy_index": parent.index.write_text("{}")
    elif mutation == "state_mac":
        data = json.loads((tmp_path / "parent-seal/state.json").read_text())
        data["binding_hmac"] = "0" * 64
        (tmp_path / "parent-seal/state.json").write_text(json.dumps(data))
    elif mutation == "history_mac":
        data = json.loads(case["baseline"].path.read_text())
        data["binding_hmac"] = "0" * 64
        case["baseline"].path.write_text(json.dumps(data))
    else: (parent.root / (case["receipt"] + ".json")).write_text('"unknown"')
    with pytest.raises(RecoveryPrivacyError) as caught:
        freeze(case)
    assert str(caught.value).startswith("recovery_privacy_") and SECRET not in str(caught.value)


@pytest.mark.parametrize("mutation", ["new_receipt", "changed_receipt", "late_discovery", "valid_old_index"])
def test_frozen_parent_revision_and_receipt_inventory_cannot_change(tmp_path, mutation):
    case = environment(tmp_path)
    frozen = freeze(case)
    old_index = case["parent"].index.read_bytes()
    if mutation == "new_receipt": case["parent"].complete("f" * 64)
    elif mutation == "changed_receipt": case["parent"].complete(case["receipt"], "split")
    else:
        case["parent"].remember([{"value": NEW_SECRET, "type": "SECRET"}])
        if mutation == "valid_old_index":
            advanced = freeze(case)
            case["parent"].index.write_bytes(old_index)
            frozen = advanced
    with pytest.raises(RecoveryPrivacyError, match="recovery_privacy_parent_changed"):
        frozen.assert_current()


def test_new_current_finding_invalidates_previously_complete_parent_input(tmp_path):
    case = environment(tmp_path)
    frozen = freeze(case)
    case["current"].remember_policy([{"value": "declaration", "type": "SECRET"}], [case["record"]],
        kind="deterministic_rule", scope=case["current"].digest("new-policy"), context=case["context"])
    assert lookup(frozen, case) is None
    assert "declaration" not in case["current"].mask(case["record"]["text"])


def test_missing_current_parent_history_cannot_turn_old_positive_into_a_cache_hit(tmp_path):
    case = environment(tmp_path, positive=True)
    missing = PrivacyHistory(tmp_path, 480, str(uuid4()), SHA, case["kwargs"]["project_key"], create=True)
    with pytest.raises(RecoveryPrivacyError, match="recovery_privacy_current_history_missing_parent"):
        freeze(case).lookup(case["record"], current=case["current"], history=missing, privacy_component_digest=COMPONENT)


def test_current_history_and_index_are_authenticated_again_on_lookup(tmp_path):
    case = environment(tmp_path)
    frozen = freeze(case)
    case["current"].index.write_text("{}")
    with pytest.raises(RecoveryPrivacyError):
        lookup(frozen, case)


def test_closed_parent_seal_must_authenticate_revision_and_cannot_go_missing(tmp_path):
    case = environment(tmp_path, positive=True)
    case["parent"].seal(case["parent"].revision(), "f" * 64)
    frozen = freeze(case)
    assert lookup(frozen, case) is not None
    (tmp_path / "parent-seal/seal.json").unlink()
    with pytest.raises(RecoveryPrivacyError):
        frozen.assert_current()


def test_oversize_receipt_fails_closed(tmp_path):
    case = environment(tmp_path)
    receipt = case["parent"].root / (case["receipt"] + ".json")
    receipt.write_bytes(b'"completed"' + b" " * 40)
    with pytest.raises(RecoveryPrivacyError):
        freeze(case)


def test_parent_receipt_symlink_fails_closed(tmp_path):
    case = environment(tmp_path)
    receipt = case["parent"].root / (case["receipt"] + ".json")
    target = tmp_path / "untrusted-marker.json"
    target.write_text('"completed"')
    receipt.unlink()
    try:
        receipt.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not permit synthetic symlinks")
    with pytest.raises(RecoveryPrivacyError):
        freeze(case)


def test_current_late_discovery_during_lookup_returns_no_reuse(tmp_path, monkeypatch):
    case = environment(tmp_path)
    frozen = freeze(case)
    original = FrozenPrivacyReceipts._current_revision
    calls = 0
    def current_revision(ledger, build_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            ledger.remember_policy([{"value": "ordinary", "type": "SECRET"}], [case["record"]],
                kind="deterministic_rule", scope=ledger.digest("late"), context=case["context"])
        return original(ledger, build_id)
    monkeypatch.setattr(FrozenPrivacyReceipts, "_current_revision", staticmethod(current_revision))
    assert lookup(frozen, case) is None


def test_strict_record_fields_cannot_hide_unknown_wrappers_or_empty_text(tmp_path):
    case = environment(tmp_path)
    frozen = freeze(case)
    for bad in ({"field": "source_excerpt", "text": "", "extra": SECRET},
                {"field": "source_excerpt", "text": "",}, {"field": False, "text": "ordinary declaration"}):
        with pytest.raises(RecoveryPrivacyError):
            lookup(frozen, case, bad)
