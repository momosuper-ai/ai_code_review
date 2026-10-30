"""Known private findings survive an empty new classifier without a new call."""

import json
import secrets
from uuid import uuid4

import pytest

from src.core.project_brain.exceptions import PrivacyCheckpointMissing
from src.core.project_brain.privacy import PrivacyLedger
from src.core.project_brain.privacy_history import HistoryCandidate, PrivacyHistory

SECRET = "synthetic_prior_credential"


def source(tmp_path):
    build_id = str(uuid4())
    parent = PrivacyLedger(tmp_path / "parent")
    parent.remember_scan([{"value": SECRET, "type": "SECRET"}], [{"field": "source_excerpt", "text": SECRET}],
        call_id=str(uuid4()), scope=parent.digest("input"), packet=parent.digest("packet"),
        context={"build_id": build_id, "unit_id": 1, "stage": "compile"})
    return parent, HistoryCandidate(480, build_id, "a" * 40, tmp_path / "parent")


def freeze(tmp_path, candidate, key=None):
    child_id = str(uuid4())
    key = key or secrets.token_bytes(32)
    history = PrivacyHistory(tmp_path / "work", 480, child_id, "b" * 40, key,
                             create=True, candidates=[candidate])
    return history, key, child_id


def test_old_hmac_rekeys_actual_current_text_without_model_call(tmp_path):
    parent, candidate = source(tmp_path)
    old_index = parent.index.read_bytes()
    history, key, build = freeze(tmp_path, candidate)
    child = PrivacyLedger(tmp_path / "child")
    rows = [{"field": "source_path", "text": "app/" + SECRET + "/settings.py"}]
    history.apply(rows, child, scope=child.digest("scope"), context={"build_id": build, "unit_id": 1, "stage": "privacy"})
    assert SECRET not in child.mask(rows[0]["text"])
    assert child.origins["calls"] == {} and len(child.entries) == 1
    record = next(iter(next(iter(child.origins["findings"].values()))["policy_records"].values()))
    assert record["kind"] == "inherited_hmac_match" and record["source_build_id"] == candidate.build_id
    assert parent.index.read_bytes() == old_index
    assert SECRET not in history.path.read_text() and SECRET not in child.index.read_text()
    assert SECRET not in b"".join(p.read_bytes() for p in history.root.rglob("*") if p.is_file()).decode("utf-8", errors="ignore")
    restarted = PrivacyHistory(tmp_path / "work", 480, build, "b" * 40, key)
    child2 = PrivacyLedger(tmp_path / "child", create=False)
    before = child2.index.read_bytes()
    restarted.apply(rows, child2, scope=child2.digest("scope"))
    assert child2.index.read_bytes() == before


def test_history_does_not_invent_current_occurrences(tmp_path):
    _, candidate = source(tmp_path)
    history, _, _ = freeze(tmp_path, candidate)
    child = PrivacyLedger(tmp_path / "child")
    history.apply([{"field": "text", "text": "ordinary declaration"}], child, scope=child.digest("scope"))
    assert not child.entries and not child.index.exists()


def test_history_rejects_other_project_and_bad_mac(tmp_path):
    parent, candidate = source(tmp_path)
    wrong = HistoryCandidate(481, candidate.build_id, candidate.source_commit, candidate.directory)
    with pytest.raises(PrivacyCheckpointMissing):
        freeze(tmp_path, wrong)
    data = json.loads(parent.index.read_text())
    data["index_hmac"] = "0" * 64
    parent.index.write_text(json.dumps(data))
    with pytest.raises(PrivacyCheckpointMissing):
        freeze(tmp_path, candidate)


@pytest.mark.parametrize("fault", ["key", "fingerprints.json", "manifest.json", "missing"])
def test_snapshot_tampering_or_loss_cannot_be_empty_history(tmp_path, fault):
    _, candidate = source(tmp_path)
    history, key, build_id = freeze(tmp_path, candidate)
    directory = history.root / "snapshots" / history.parents[0].snapshot_id
    if fault == "missing":
        (directory / "fingerprints.json").unlink()
    else:
        (directory / fault).write_bytes(b"invalid")
    with pytest.raises(PrivacyCheckpointMissing):
        PrivacyHistory(tmp_path / "work", 480, build_id, "b" * 40, key)


def test_later_build_keeps_registered_snapshots_without_old_work(tmp_path):
    _, candidate = source(tmp_path)
    first, key, first_id = freeze(tmp_path, candidate)
    # Removing the original index does not remove its immutable retained copy.
    (candidate.directory / "privacy/fingerprints.json").unlink()
    second = PrivacyHistory(tmp_path / "work", 480, str(uuid4()), "c" * 40, key, create=True,
        required=[(first_id, first.digest)])
    assert len(second.parents) == 1
    child = PrivacyLedger(tmp_path / "child")
    second.apply([{"field": "text", "text": SECRET}], child, scope=child.digest("scope"))
    assert SECRET not in child.mask(SECRET)


def test_registered_manifest_missing_is_not_recreated(tmp_path):
    _, candidate = source(tmp_path)
    first, key, first_id = freeze(tmp_path, candidate)
    first.path.unlink()
    with pytest.raises(PrivacyCheckpointMissing):
        PrivacyHistory(tmp_path / "work", 480, str(uuid4()), "c" * 40, key, create=True,
                       required=[(first_id, first.digest)])


@pytest.mark.parametrize("fault", [None, "state", "seal", "valid_old_index"])
def test_sealed_history_requires_full_audit_and_matching_revision(tmp_path, fault):
    build = str(uuid4())
    ledger = PrivacyLedger(tmp_path / "sealed", seal_directory=tmp_path / "seals")
    ledger.remember([{"value": SECRET, "type": "SECRET"}])
    previous = ledger.index.read_bytes()
    ledger.remember([{"value": "second_synthetic_secret", "type": "KEY"}])
    ledger.seal(ledger.revision(), "a" * 64)
    ledger.retain_audit(tmp_path / "audit")
    if fault in {"state", "seal"}:
        (tmp_path / "audit/privacy" / (fault + ".json")).unlink()
    elif fault == "valid_old_index":
        (tmp_path / "audit/privacy/fingerprints.json").write_bytes(previous)
    candidate = HistoryCandidate(480, build, "a" * 40, tmp_path / "audit", sealed_audit=True, require_index=True)
    if fault is None:
        history, key, child = freeze(tmp_path, candidate)
        assert history.parents[0].metadata["source_kind"] == "sealed_audit"
        directory = history.root / "snapshots" / history.parents[0].snapshot_id
        assert {p.name for p in directory.iterdir()} == {"key", "fingerprints.json", "manifest.json", "state.json", "seal.json"}
        # Validation remains mandatory when reloading the frozen snapshot.
        (directory / "seal.json").unlink()
        with pytest.raises(PrivacyCheckpointMissing):
            PrivacyHistory(tmp_path / "work", 480, child, "b" * 40, key)
    else:
        with pytest.raises(PrivacyCheckpointMissing):
            freeze(tmp_path, candidate)


def test_required_index_loss_cannot_be_interpreted_as_empty_legacy(tmp_path):
    _, candidate = source(tmp_path)
    (candidate.directory / "privacy/fingerprints.json").unlink()
    strict = HistoryCandidate(candidate.project_id, candidate.build_id, candidate.source_commit,
                              candidate.directory, require_index=True)
    with pytest.raises(PrivacyCheckpointMissing):
        freeze(tmp_path, strict)


@pytest.mark.parametrize("create", [False, True])
def test_new_protocol_initial_empty_index_is_durable_and_cannot_be_recreated(tmp_path, create):
    ledger = PrivacyLedger(tmp_path / "child", require_index=True)
    assert ledger.index.is_file() and json.loads(ledger.index.read_text())["entries"] == {}
    ledger.index.unlink()
    with pytest.raises(PrivacyCheckpointMissing):
        PrivacyLedger(tmp_path / "child", create=create, require_index=True)
    with pytest.raises(PrivacyCheckpointMissing):
        ledger.refresh()
    assert not ledger.index.exists()


@pytest.mark.parametrize("mutation", ["foreign_build", "foreign_project", "index_mac", "snapshot_missing"])
def test_audit_authenticates_private_history_without_writing(tmp_path, mutation):
    from scripts.brain_quality_audit import _history_integrity

    _, candidate = source(tmp_path)
    history, key, child_id = freeze(tmp_path, candidate)
    child = PrivacyLedger(tmp_path / "child")
    history.apply([{"field": "text", "text": SECRET}], child, scope=child.digest("scope"),
                  context={"build_id": child_id, "unit_id": 1, "stage": "privacy"})
    manifest = {"generator": "project-brain/2.0.1", "project_id": 480, "build_id": child_id,
                "source_commit": "b" * 40, "coverage": {"privacy_history_digest": history.digest}}
    before = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = _history_integrity(manifest, key, key, tmp_path / "child", tmp_path / "work")
    assert result == {"baseline_authenticated": True, "parent_count": 1,
                      "inherited_occurrence_records": 1, "rule_occurrence_records": 0}
    assert before == {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    if mutation == "foreign_build":
        manifest["build_id"] = str(uuid4())
    elif mutation == "foreign_project":
        manifest["project_id"] = 481
    elif mutation == "index_mac":
        raw = json.loads(child.index.read_text())
        raw["index_hmac"] = "0" * 64
        child.index.write_text(json.dumps(raw))
    else:
        (history.root / "snapshots" / history.parents[0].snapshot_id / "key").unlink()
    with pytest.raises(ValueError, match="审计历史隐私基线无效"):
        _history_integrity(manifest, key, key, tmp_path / "child", tmp_path / "work")
