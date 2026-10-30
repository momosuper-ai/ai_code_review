"""Filesystem acceptance tests for private final HMAC-only audit receipts."""

import json
import os
import shutil
from pathlib import Path

import pytest

from scripts.brain_audit_receipt import watch_receipt

_BUILD = "3f0100c5-1502-4951-a74a-9871aab32006"


def roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Make independent synthetic worker and formal roots."""
    work, artifacts = tmp_path / "private", tmp_path / "formal"
    source = work / "work" / _BUILD / "privacy"
    source.mkdir(parents=True)
    (artifacts / "builds").mkdir(parents=True)
    (source / "key").write_bytes(b"k" * 32)
    (source / "fingerprints.json").write_text(json.dumps({"a" * 64: [12, "SECRET"]}), encoding="utf-8")
    (source / ("b" * 64 + ".json")).write_text('"completed"', encoding="utf-8")
    (source / "forbidden-context.json").write_text('"synthetic-source-must-not-copy"', encoding="utf-8")
    return work, artifacts, source


def manifest(directory: Path) -> None:
    """Write only identity metadata as a finalization signal."""
    target = directory / "repository/docs/project-brain/manifest.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"build_id": _BUILD, "source_commit": "a" * 40}), encoding="utf-8")


def test_final_receipt_copies_only_key_and_hmac_index(tmp_path):
    work, artifacts, source = roots(tmp_path)
    manifest(artifacts / "builds" / _BUILD)
    result = watch_receipt(_BUILD, work, artifacts, timeout_seconds=0.1, poll_seconds=0.05)
    destination = work / "audits" / _BUILD / "privacy"
    assert result["final_ledger_captured"] is True
    assert result["fingerprint_count"] == 1
    assert {path.name for path in destination.iterdir()} == {"key", "fingerprints.json", "audit_receipt.json"}
    assert (destination / "fingerprints.json").read_bytes() == (source / "fingerprints.json").read_bytes()
    assert "synthetic-source-must-not-copy" not in (destination / "audit_receipt.json").read_text()
    if os.name != "nt":
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in destination.iterdir())
        assert destination.stat().st_mode & 0o777 == 0o700


def test_staging_capture_survives_atomic_final_rename_and_source_cleanup(tmp_path, monkeypatch):
    work, artifacts, source = roots(tmp_path)
    staging = artifacts / "builds" / (_BUILD + ".tmp-fixture")
    manifest(staging)

    def finish(_: float) -> None:
        staging.rename(artifacts / "builds" / _BUILD)
        shutil.rmtree(source.parent)

    monkeypatch.setattr("scripts.brain_audit_receipt.time.sleep", finish)
    result = watch_receipt(_BUILD, work, artifacts, timeout_seconds=1, poll_seconds=0.05)
    assert result["final_ledger_captured"] is True
    assert result["fingerprint_count"] == 1


def test_active_only_copy_never_claims_final_after_source_cleanup(tmp_path, monkeypatch):
    work, artifacts, source = roots(tmp_path)

    def finish(_: float) -> None:
        manifest(artifacts / "builds" / _BUILD)
        shutil.rmtree(source.parent)

    monkeypatch.setattr("scripts.brain_audit_receipt.time.sleep", finish)
    result = watch_receipt(_BUILD, work, artifacts, timeout_seconds=1, poll_seconds=0.05)
    assert result["final_ledger_captured"] is False
    assert result["state"] == "final_ledger_unavailable"


def test_wrong_staging_inode_cannot_authenticate_final_ledger(tmp_path, monkeypatch):
    work, artifacts, source = roots(tmp_path)
    staging = artifacts / "builds" / (_BUILD + ".tmp-fixture")
    manifest(staging)

    def finish(_: float) -> None:
        manifest(artifacts / "builds" / _BUILD)
        shutil.rmtree(source.parent)

    monkeypatch.setattr("scripts.brain_audit_receipt.time.sleep", finish)
    result = watch_receipt(_BUILD, work, artifacts, timeout_seconds=1, poll_seconds=0.05)
    assert result["final_ledger_captured"] is False


def test_timeout_leaves_explicit_incomplete_receipt(tmp_path):
    work, artifacts, _ = roots(tmp_path)
    result = watch_receipt(_BUILD, work, artifacts, timeout_seconds=0.06, poll_seconds=0.05)
    assert result["state"] == "timeout"
    assert result["final_ledger_captured"] is False


def test_plaintext_or_invalid_index_is_rejected_before_copy(tmp_path):
    work, artifacts, source = roots(tmp_path)
    (source / "fingerprints.json").write_text(json.dumps({"synthetic-raw-value": [12, "SECRET"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="审计隐私指纹无效"):
        watch_receipt(_BUILD, work, artifacts, timeout_seconds=0.1, poll_seconds=0.05)
    assert not (work / "audits" / _BUILD / "privacy/key").exists()


def test_public_artifact_root_is_never_a_private_receipt_destination(tmp_path):
    _, artifacts, _ = roots(tmp_path)
    with pytest.raises(ValueError, match="不能写入正式产物卷"):
        watch_receipt(_BUILD, artifacts, artifacts, timeout_seconds=0.1, poll_seconds=0.05)


def version_two_index() -> dict:
    """A structurally valid fixture; read-only copying does not certify its MAC."""
    digest = "a" * 64
    call_id = "bc4a35fe-2c6f-4b4c-a4d7-a248d061df41"
    call = {"context": {"build_id": _BUILD, "unit_id": 1, "stage": "privacy"},
            "after_response": False, "scope": "c" * 64, "packet": "d" * 64,
            "records_hmac": "e" * 64, "findings_hmac": "f" * 64,
            "start": 0, "record_count": 1, "binding_hmac": "1" * 64}
    location = {"call_id": call_id, "record_hmac": "2" * 64, "field_hmac": "3" * 64,
                "field_role": "generic", "location_kind": "candidate_occurrence",
                "record_index": 0, "record_length": 20, "first_offset": 0,
                "occurrence_count": 1, "length": 12, "type": "SECRET"}
    return {"version": 2, "index_hmac": "0" * 64, "entries": {digest: [12, "SECRET"]},
            "origins": {"calls": {call_id: call},
                        "findings": {digest: {"legacy_unknown": False, "records": {"4" * 64: location}}}}}


def test_version_two_receipt_preserves_entire_private_index_and_counts_only_entries(tmp_path):
    work, artifacts, source = roots(tmp_path)
    (source / "fingerprints.json").write_text(json.dumps(version_two_index()), encoding="utf-8")
    manifest(artifacts / "builds" / _BUILD)
    before = {name: (source / name).read_bytes() for name in ("key", "fingerprints.json")}
    formal_before = {str(path.relative_to(artifacts)): path.read_bytes()
                     for path in artifacts.rglob("*") if path.is_file()}

    result = watch_receipt(_BUILD, work, artifacts, timeout_seconds=0.1, poll_seconds=0.05)

    destination = work / "audits" / _BUILD / "privacy"
    assert result["final_ledger_captured"] is True
    assert result["fingerprint_count"] == 1
    assert json.loads((destination / "fingerprints.json").read_bytes()) == version_two_index()
    assert before == {name: (source / name).read_bytes() for name in before}
    assert (destination / "fingerprints.json").read_bytes() == before["fingerprints.json"]
    assert formal_before == {str(path.relative_to(artifacts)): path.read_bytes()
                             for path in artifacts.rglob("*") if path.is_file()}
    assert "origins" not in result and "entries" not in result


@pytest.mark.parametrize("mutation", ["envelope_field", "origin_field", "finding_field", "unknown_type", "missing_finding"])
def test_version_two_malicious_metadata_is_rejected_before_private_copy(tmp_path, mutation):
    work, artifacts, source = roots(tmp_path)
    index = version_two_index()
    marker = "untrusted-context-must-never-escape"
    if mutation == "envelope_field":
        index[marker] = marker
    elif mutation == "origin_field":
        index["origins"][marker] = marker
    elif mutation == "finding_field":
        index["origins"]["findings"]["a" * 64][marker] = marker
    elif mutation == "unknown_type":
        index["entries"]["a" * 64][1] = marker
    else:
        index["origins"]["findings"] = {}
    (source / "fingerprints.json").write_text(json.dumps(index), encoding="utf-8")
    before = {name: (source / name).read_bytes() for name in ("key", "fingerprints.json")}
    with pytest.raises(ValueError, match="审计隐私指纹无效") as error:
        watch_receipt(_BUILD, work, artifacts, timeout_seconds=0.1, poll_seconds=0.05)
    assert marker not in str(error.value)
    assert not (work / "audits" / _BUILD / "privacy/key").exists()
    assert before == {name: (source / name).read_bytes() for name in before}


def test_duplicate_fingerprint_fields_are_rejected_instead_of_overwritten(tmp_path):
    work, artifacts, source = roots(tmp_path)
    digest = "a" * 64
    raw = '{"' + digest + '":[12,"SECRET"],"' + digest + '":[12,"KEY"]}'
    (source / "fingerprints.json").write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="审计隐私指纹无效"):
        watch_receipt(_BUILD, work, artifacts, timeout_seconds=0.1, poll_seconds=0.05)
    assert not (work / "audits" / _BUILD / "privacy/key").exists()
