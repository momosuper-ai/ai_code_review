"""Only synthetic, offline data exercises the private provenance transaction."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from src.core.project_brain.privacy import PrivacyLedger
from src.core.project_brain import privacy_provenance as contract
from src.core.project_brain.privacy_provenance import decode_fingerprint_index, parse_fingerprint_index
from src.core.project_brain.exceptions import PrivacyCheckpointMissing


def scan(ledger, *, value="synthetic_collision", records=None, call_id=None, **metadata):
    records = records or [{"field": "code_identifier", "text": value}]
    kwargs = {"call_id": call_id or str(uuid4()), "scope": "a" * 64, "packet": "b" * 64, **metadata}
    ledger.remember_scan([{"value": value, "type": "SECRET"}], records, **kwargs)
    return kwargs


def saved(ledger):
    return json.loads(ledger.index.read_text(encoding="utf-8"))


def test_all_matching_records_are_candidate_locations_and_no_raw_content_persists(tmp_path):
    ledger = PrivacyLedger(tmp_path)
    value = "fictive_credential"
    records = [{"field": "code_identifier", "text": value},
               {"field": "source_path", "text": f"private_source/{value}.py"},
               {"field": "untrusted_raw_field", "text": "汉字😀:" + value + " + " + value},
               {"field": "generic", "text": "not a match"}]
    context = {"build_id": str(uuid4()), "unit_id": 7, "stage": "module_summary"}
    metadata = scan(ledger, value=value, records=records, start=100, context=context, after_response=True)
    raw = ledger.index.read_text(encoding="utf-8")
    for private in (value, "untrusted_raw_field", "private_source", records[2]["text"]):
        assert private not in raw
    data = saved(ledger)
    assert data["version"] == 2
    digest = ledger.digest("value:" + value)
    finding = data["origins"]["findings"][digest]
    assert finding["legacy_unknown"] is False
    locations = sorted(finding["records"].values(), key=lambda item: item["record_index"])
    assert [item["record_index"] for item in locations] == [100, 101, 102]
    assert [item["field_role"] for item in locations] == ["code_identifier", "source_path", "generic"]
    assert [item["first_offset"] for item in locations] == [0, len("private_source/"), 4]
    assert [item["occurrence_count"] for item in locations] == [1, 1, 2]
    assert all(item["location_kind"] == "candidate_occurrence" for item in locations)
    assert data["origins"]["calls"][metadata["call_id"]]["context"] == context
    restarted = PrivacyLedger(tmp_path)
    assert restarted.entries == ledger.entries
    assert restarted.origins == ledger.origins
    assert value not in restarted.mask(records[1]["text"])


def test_short_repeated_value_has_one_bounded_location_and_overlapping_count(tmp_path):
    ledger = PrivacyLedger(tmp_path)
    scan(ledger, value="aa", records=[{"field": "generic", "text": "a" * 10000}])
    finding = next(iter(ledger.origins["findings"].values()))
    assert len(finding["records"]) == 1
    location = next(iter(finding["records"].values()))
    assert location["occurrence_count"] == 9999
    assert location["first_offset"] == 0
    assert len(ledger.index.read_bytes()) < 2000


def test_exact_repeat_is_idempotent_and_same_id_conflicts_do_not_modify_state(tmp_path):
    ledger = PrivacyLedger(tmp_path)
    metadata = scan(ledger)
    before = ledger.index.read_bytes()
    scan(ledger, **metadata)
    assert ledger.index.read_bytes() == before
    for changed in ({"packet": "c" * 64}, {"scope": "d" * 64}, {"after_response": True},
                    {"start": 1}, {"context": {"build_id": str(uuid4()), "unit_id": 1, "stage": "compile"}},
                    {"records": [{"field": "source_path", "text": "synthetic_collision"}]}):
        with pytest.raises(ValueError, match="^隐私指纹或来源检查点无效$"):
            scan(ledger, **{**metadata, **changed})
        assert ledger.index.read_bytes() == before
    with pytest.raises(ValueError):
        ledger.remember_scan([], [{"field": "code_identifier", "text": "synthetic_collision"}], **metadata)
    assert ledger.index.read_bytes() == before


def test_observed_types_across_calls_preserved_without_retyping_global_mask(tmp_path):
    ledger = PrivacyLedger(tmp_path)
    first = scan(ledger)
    ledger.remember_scan([{"value": "synthetic_collision", "type": "KEY"}],
                         [{"field": "code_identifier", "text": "synthetic_collision"}],
                         **{**first, "call_id": str(uuid4())})
    assert list(ledger.entries.values()) == [(len("synthetic_collision"), "SECRET")]
    observations = next(iter(ledger.origins["findings"].values()))["records"].values()
    assert {item["type"] for item in observations} == {"SECRET", "KEY"}
    assert "<<SECRET_" in PrivacyLedger(tmp_path).mask("synthetic_collision")


def test_legacy_read_does_not_rewrite_or_invent_origins_and_write_migrates(tmp_path):
    ledger = PrivacyLedger(tmp_path)
    digest = ledger.digest("value:old-fictive-value")
    old = json.dumps({digest: [len("old-fictive-value"), "SECRET"]}).encode()
    ledger.index.write_bytes(old)
    restored = PrivacyLedger(tmp_path)
    assert restored.index.read_bytes() == old
    assert restored.origins == {"calls": {}, "findings": {digest: {"legacy_unknown": True, "records": {}}}}
    scan(restored)
    assert saved(restored)["version"] == 2
    assert restored.origins["findings"][digest] == {"legacy_unknown": True, "records": {}}


def test_remember_without_scan_remains_explicitly_unknown_even_after_known_origin(tmp_path):
    ledger = PrivacyLedger(tmp_path)
    scan(ledger)
    ledger.remember([{"value": "synthetic_collision", "type": "SECRET"}])
    finding = next(iter(ledger.origins["findings"].values()))
    assert finding["legacy_unknown"] is True
    assert len(finding["records"]) == 1


def test_empty_findings_do_not_persist_call_or_index(tmp_path):
    ledger = PrivacyLedger(tmp_path)
    ledger.remember_scan([], [{"field": "generic", "text": "public fixture"}],
                         call_id=str(uuid4()), scope="a" * 64, packet="b" * 64)
    assert not ledger.index.exists()
    assert not ledger.entries and ledger.origins == {"calls": {}, "findings": {}}


def test_new_values_and_origins_are_not_adopted_on_failed_write(tmp_path, monkeypatch):
    ledger = PrivacyLedger(tmp_path)
    scan(ledger)
    before = ledger.index.read_bytes()
    entries, origins = copy.deepcopy((ledger.entries, ledger.origins))

    def fail(*args):
        raise OSError("fixed synthetic write failure")

    monkeypatch.setattr(ledger, "_write", fail)
    with pytest.raises(OSError):
        scan(ledger, value="second-fictive-value")
    assert ledger.index.read_bytes() == before
    assert (ledger.entries, ledger.origins) == (entries, origins)
    assert ledger.mask("second-fictive-value") == "second-fictive-value"
    assert (PrivacyLedger(tmp_path).entries, PrivacyLedger(tmp_path).origins) == (entries, origins)


def test_atomic_replace_interruption_leaves_old_index_and_no_partial_file(tmp_path, monkeypatch):
    ledger = PrivacyLedger(tmp_path)
    scan(ledger)
    before = ledger.index.read_bytes()
    entries, origins = copy.deepcopy((ledger.entries, ledger.origins))

    def interrupt(*args):
        raise OSError("fixed synthetic replace failure")

    monkeypatch.setattr(Path, "replace", interrupt)
    with pytest.raises(OSError):
        scan(ledger, value="second-fictive-value")
    assert ledger.index.read_bytes() == before
    assert (ledger.entries, ledger.origins) == (entries, origins)
    assert not list(ledger.root.glob("fingerprints.json.*"))


def test_interruption_after_replace_recovers_complete_transaction_on_restart(tmp_path, monkeypatch):
    ledger = PrivacyLedger(tmp_path)
    write = ledger._write

    def committed_then_interrupt(*args):
        write(*args)
        raise OSError("fixed synthetic post-commit interruption")

    monkeypatch.setattr(ledger, "_write", committed_then_interrupt)
    with pytest.raises(OSError):
        scan(ledger)
    assert not ledger.entries
    restored = PrivacyLedger(tmp_path)
    assert restored.entries and restored.origins["findings"]
    assert len(restored.origins["calls"]) == 1
    assert "synthetic_collision" not in restored.mask("synthetic_collision")


def test_concurrent_independent_ledgers_merge_under_one_lock(tmp_path):
    ledgers = [PrivacyLedger(tmp_path) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda pair: scan(pair[1], value=f"fictive_value_{pair[0]}"), enumerate(ledgers)))
    restored = PrivacyLedger(tmp_path)
    assert len(restored.entries) == 8
    assert len(restored.origins["calls"]) == 8
    assert sum(len(item["records"]) for item in restored.origins["findings"].values()) == 8


@pytest.mark.parametrize("change", [
    {"call_id": "untrusted-uuid"}, {"scope": "not-a-hmac"}, {"packet": "not-a-hmac"},
    {"start": True}, {"start": -1}, {"after_response": 1},
    {"context": {"build_id": str(uuid4()), "unit_id": True, "stage": "compile"}},
    {"context": {"build_id": str(uuid4()), "unit_id": 1, "stage": "untrusted-stage"}},
    {"context": {"build_id": str(uuid4()), "unit_id": 1, "stage": "compile", "raw_field": "untrusted-content"}},
])
def test_malformed_call_metadata_rejected_without_content_in_errors(tmp_path, change):
    ledger = PrivacyLedger(tmp_path)
    with pytest.raises(ValueError, match="^隐私指纹或来源检查点无效$"):
        scan(ledger, **change)
    assert not ledger.index.exists()


@pytest.mark.parametrize("items,records", [
    ([{"value": "not present", "type": "SECRET"}], [{"field": "generic", "text": "ordinary"}]),
    ([{"value": "ordinary", "type": "unexpected-kind"}], [{"field": "generic", "text": "ordinary"}]),
    ([{"value": "ordinary", "type": "SECRET", "source": "private"}], [{"field": "generic", "text": "ordinary"}]),
    ([{"value": "ordinary", "type": "SECRET"}, {"value": "ordinary", "type": "KEY"}], [{"field": "generic", "text": "ordinary"}]),
    ([{"value": "ordinary", "type": "SECRET"}], [{"field": "generic", "text": "ordinary", "source": "private"}]),
    ([{"value": "ordinary", "type": "SECRET"}], [{"field": "generic", "text": "ordinary\ud800"}]),
    ([{"value": "\ud800", "type": "SECRET"}], [{"field": "generic", "text": "ordinary"}]),
])
def test_unverifiable_or_unsafe_scan_data_is_not_remembered(tmp_path, items, records):
    ledger = PrivacyLedger(tmp_path)
    with pytest.raises(ValueError, match="^隐私指纹或来源检查点无效$"):
        ledger.remember_scan(items, records, call_id=str(uuid4()), scope="a" * 64, packet="b" * 64)
    assert not ledger.index.exists() and not ledger.entries


@pytest.mark.parametrize("target,new", [("first_offset", 1), ("field_role", "semantic_output"),
                                         ("record_hmac", "c" * 64)])
def test_shape_valid_location_tampering_fails_keyed_worker_binding(tmp_path, target, new):
    ledger = PrivacyLedger(tmp_path)
    scan(ledger, records=[{"field": "code_identifier", "text": "synthetic_collision suffix"}])
    data = saved(ledger)
    finding = next(iter(data["origins"]["findings"].values()))
    next(iter(finding["records"].values()))[target] = new
    # Structural audits have no key; worker additionally authenticates metadata.
    decode_fingerprint_index(data)
    ledger.index.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="^隐私指纹或来源检查点无效$"):
        ledger.refresh()


def test_shape_valid_call_context_tampering_fails_keyed_binding(tmp_path):
    ledger = PrivacyLedger(tmp_path)
    scan(ledger, context={"build_id": str(uuid4()), "unit_id": 4, "stage": "compile"})
    data = saved(ledger)
    next(iter(data["origins"]["calls"].values()))["context"]["unit_id"] = 5
    decode_fingerprint_index(data)
    ledger.index.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        PrivacyLedger(tmp_path)


@pytest.mark.parametrize("change", ["delete_location", "move_location", "add_location", "erase_origins", "delete_value"])
def test_index_binding_authenticates_complete_sets_not_only_surviving_rows(tmp_path, change):
    ledger = PrivacyLedger(tmp_path)
    scan(ledger, records=[{"field": "code_identifier", "text": "synthetic_collision first"},
                          {"field": "source_path", "text": "synthetic_collision second"}])
    data = saved(ledger)
    digest, finding = next(iter(data["origins"]["findings"].items()))
    origin_id, location = next(iter(finding["records"].items()))
    if change == "delete_location":
        del finding["records"][origin_id]
    elif change == "move_location":
        location["first_offset"] = 1
    elif change == "add_location":
        finding["records"]["f" * 64] = dict(location)
    elif change == "erase_origins":
        finding.update(legacy_unknown=True, records={})
        data["origins"]["calls"].clear()
    else:
        del data["entries"][digest]
        del data["origins"]["findings"][digest]
        data["origins"]["calls"].clear()
    ledger.index.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="^隐私指纹或来源检查点无效$"):
        PrivacyLedger(tmp_path)


def test_durable_order_is_index_before_receipt_and_key_before_index(tmp_path, monkeypatch):
    import os

    events = []
    fsync, replace = os.fsync, Path.replace

    def sync_file(fd):
        events.append("file_fsync")
        return fsync(fd)

    def replace_file(source, target):
        events.append("replace:" + target.name)
        return replace(source, target)

    monkeypatch.setattr(os, "fsync", sync_file)
    monkeypatch.setattr(Path, "replace", replace_file)
    monkeypatch.setattr(PrivacyLedger, "_sync_directory", staticmethod(lambda directory: events.append("directory_fsync")))
    ledger = PrivacyLedger(tmp_path)
    assert events == ["file_fsync", "directory_fsync"]
    events.clear()
    scan(ledger)
    ledger.complete("c" * 64)
    assert events == ["file_fsync", "replace:fingerprints.json", "directory_fsync",
                      "file_fsync", "replace:" + "c" * 64 + ".json", "directory_fsync"]


def test_directory_sync_failure_stops_adoption_and_receipt_but_restart_sees_whole_index(tmp_path, monkeypatch):
    ledger = PrivacyLedger(tmp_path)

    def interrupt(directory):
        raise OSError("fixed synthetic durability failure")

    monkeypatch.setattr(ledger, "_sync_directory", interrupt)
    with pytest.raises(OSError):
        scan(ledger)
    assert not ledger.entries
    assert not ledger.receipt("c" * 64)
    restored = PrivacyLedger(tmp_path)
    assert len(restored.entries) == 1 and len(restored.origins["calls"]) == 1


@pytest.mark.parametrize("mutate", [
    lambda data: data.update(version=True),
    lambda data: data.update(raw_field="untrusted-context"),
    lambda data: data["origins"].update(raw_field="untrusted-context"),
    lambda data: next(iter(data["entries"].values())).__setitem__(0, True),
    lambda data: next(iter(data["origins"]["findings"].values())).update(legacy_unknown=1),
    lambda data: next(iter(data["origins"]["calls"].values())).update(start=True),
    lambda data: next(iter(next(iter(data["origins"]["findings"].values()))["records"].values())).update(occurrence_count=True),
    lambda data: next(iter(next(iter(data["origins"]["findings"].values()))["records"].values())).update(raw_field="untrusted-content"),
])
def test_pure_decoder_rejects_unknown_or_loosely_typed_metadata(tmp_path, mutate):
    ledger = PrivacyLedger(tmp_path)
    scan(ledger)
    data = saved(ledger)
    mutate(data)
    with pytest.raises(ValueError, match="^隐私指纹或来源检查点无效$"):
        decode_fingerprint_index(data)


def test_duplicate_json_identity_rejected_before_dict_overwrite():
    with pytest.raises(ValueError, match="^隐私指纹或来源检查点无效$"):
        parse_fingerprint_index('{"version":2,"version":2,"entries":{},"origins":{"calls":{},"findings":{}}}')


def test_total_location_limit_fails_transaction_instead_of_truncating(tmp_path, monkeypatch):
    ledger = PrivacyLedger(tmp_path)
    monkeypatch.setattr(contract, "MAX_LOCATIONS", 2)
    records = [{"field": "generic", "text": f"synthetic_collision {index}"} for index in range(3)]
    with pytest.raises(ValueError):
        scan(ledger, records=records)
    assert not ledger.index.exists() and not ledger.entries


@pytest.mark.parametrize("limit", ["MAX_CALLS", "MAX_ENTRIES", "MAX_INDEX_BYTES"])
def test_global_envelope_limits_do_not_erase_previous_transaction(tmp_path, monkeypatch, limit):
    ledger = PrivacyLedger(tmp_path)
    scan(ledger)
    before = ledger.index.read_bytes()
    monkeypatch.setattr(contract, limit, len(before) + 1 if limit == "MAX_INDEX_BYTES" else 1)
    with pytest.raises(ValueError):
        scan(ledger, value="second_fictive_value")
    assert ledger.index.read_bytes() == before
    assert len(ledger.entries) == 1


@pytest.mark.parametrize("records", [
    [{"field": "generic", "text": "x"}] * 1025,
    [{"field": "generic", "text": "x" * (contract.MAX_RECORD_CHARS + 1)}],
    [{"field": "generic", "text": "汉" * contract.MAX_RECORD_CHARS}] * 3,
])
def test_input_capacity_rejects_complete_scan_without_partial_write(tmp_path, records):
    ledger = PrivacyLedger(tmp_path)
    with pytest.raises(ValueError):
        scan(ledger, value="x", records=records)
    assert not ledger.index.exists()


def test_cross_build_context_in_one_ledger_is_rejected(tmp_path):
    ledger = PrivacyLedger(tmp_path)
    scan(ledger, context={"build_id": str(uuid4()), "unit_id": 1, "stage": "privacy"})
    before = ledger.index.read_bytes()
    with pytest.raises(ValueError):
        scan(ledger, value="second_fictive_value", context={"build_id": str(uuid4()), "unit_id": 2, "stage": "compile"})
    assert ledger.index.read_bytes() == before


@pytest.mark.parametrize("checkpoint", ["fingerprints.json", "a" * 64 + ".json"])
def test_missing_key_never_reinitializes_existing_checkpoint(tmp_path, checkpoint):
    root = tmp_path / "privacy"
    root.mkdir()
    (root / checkpoint).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="^隐私指纹密钥缺失，停止构建$"):
        PrivacyLedger(tmp_path)
    assert not (root / "key").exists()


@pytest.mark.parametrize("existing_directory", [False, True])
def test_resume_missing_directory_or_key_never_creates_files(tmp_path, monkeypatch, existing_directory):
    root = tmp_path / "privacy"
    if existing_directory:
        root.mkdir()

    def no_lock(*args, **kwargs):
        pytest.fail("resuming must not acquire a potentially creating lock")

    monkeypatch.setattr("src.core.project_brain.privacy.FileLock", no_lock)
    with pytest.raises(PrivacyCheckpointMissing):
        PrivacyLedger(tmp_path, create=False)
    assert root.exists() is existing_directory
    assert not root.exists() or not list(root.iterdir())


@pytest.mark.parametrize("length", [0, 31, 33, 100_000])
def test_resume_bad_key_never_replaces_or_creates_anything(tmp_path, length):
    root = tmp_path / "privacy"
    root.mkdir()
    key = root / "key"
    key.write_bytes(b"x" * length)
    with pytest.raises(PrivacyCheckpointMissing):
        PrivacyLedger(tmp_path, create=False)
    assert key.read_bytes() == b"x" * length
    assert [path.name for path in root.iterdir()] == ["key"]


def test_extract_can_create_then_resume_reads_same_checkpoint_without_writing(tmp_path, monkeypatch):
    ledger = PrivacyLedger(tmp_path, create=True)
    scan(ledger)
    (ledger.root / "index.lock").unlink(missing_ok=True)
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in ledger.root.iterdir()}

    def no_lock(*args, **kwargs):
        pytest.fail("resuming must be read-only")

    monkeypatch.setattr("src.core.project_brain.privacy.FileLock", no_lock)
    restored = PrivacyLedger(tmp_path, create=False)
    assert restored.key == ledger.key and restored.entries == ledger.entries and restored.origins == ledger.origins
    assert {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in ledger.root.iterdir()} == before


def test_private_file_permissions_and_receipts_remain_compatible(tmp_path):
    ledger = PrivacyLedger(tmp_path)
    scan(ledger)
    ledger.complete("c" * 64)
    assert ledger.receipt("c" * 64) == "completed"
    ledger.complete("d" * 64, "split")
    assert ledger.receipt("d" * 64) == "split"
    if __import__("os").name != "nt":
        assert ledger.index.stat().st_mode & 0o077 == 0
        assert (ledger.root / "key").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("field,expected", [("code_identifier", "code_identifier"), ("field_name", "field_name"),
                                           ("source_path", "source_path"), ("sources.excerpt", "source_excerpt"),
                                           ("model_retry_reasoning", "retry_reasoning"), ("claims.text", "semantic_output"),
                                           ("untrusted_field_name", "generic")])
def test_field_roles_are_closed_and_never_retain_raw_names(field, expected):
    assert contract.field_role(field) == expected
