"""Synthetic local files verify immutable seals and late-discovery fencing."""

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from src.core.project_brain.exceptions import PrivacySealConflict
from src.core.project_brain.privacy import PrivacyLedger


VALUE = "fixture-only-credential"
CONTENT = "c" * 64


def ledger(tmp_path):
    return PrivacyLedger(tmp_path / "work", seal_directory=tmp_path / "seals")


def remember_scan(item, *, call_id=None, value=VALUE):
    item.remember_scan([{"value": value, "type": "SECRET"}], [{"field": "source_excerpt", "text": value}],
        call_id=call_id or str(uuid4()), scope="a" * 64, packet="b" * 64)


def test_revision_binds_all_entries_and_origins_and_persists_no_values(tmp_path):
    item = ledger(tmp_path)
    original = item.revision()
    remember_scan(item)
    discovered = item.revision()
    remember_scan(item)  # Same value, distinct authenticated candidate origin.
    assert len(item.entries) == 1
    assert len({original, discovered, item.revision()}) == 3
    seal = item.seal(item.revision(), CONTENT)
    assert set(seal) == {"version", "revision", "content_digest", "binding_hmac"}
    assert seal["version"] == 1 and seal["content_digest"] == CONTENT
    for path in (tmp_path / "seals").iterdir():
        assert VALUE not in path.read_text(encoding="utf-8")


def test_seal_is_idempotent_and_different_content_is_rejected(tmp_path):
    item = ledger(tmp_path)
    remember_scan(item)
    revision = item.revision()
    seal = item.seal(revision, CONTENT)
    assert item.seal(revision, CONTENT) == seal
    restarted = PrivacyLedger(tmp_path / "work", create=False, seal_directory=tmp_path / "seals")
    assert restarted.seal(revision, CONTENT) == seal
    with pytest.raises(PrivacySealConflict, match="^privacy_seal_conflict$"):
        item.seal(revision, "d" * 64)
    with pytest.raises(PrivacySealConflict):
        item.seal("e" * 64, CONTENT)


def test_stale_revision_rejects_before_closing_and_can_use_current_revision(tmp_path):
    item = ledger(tmp_path)
    stale = item.revision()
    sibling = PrivacyLedger(tmp_path / "work", create=False, seal_directory=tmp_path / "seals")
    remember_scan(sibling)
    with pytest.raises(PrivacySealConflict):
        item.seal(stale, CONTENT)
    assert not (tmp_path / "seals" / "seal.json").exists()
    assert item.seal(item.revision(), CONTENT)["content_digest"] == CONTENT


def test_every_discovery_entry_point_rejects_after_seal_without_changing_index(tmp_path):
    item = ledger(tmp_path)
    remember_scan(item)
    item.seal(item.revision(), CONTENT)
    before = item.index.read_bytes()
    for operation in (
        lambda: item.remember([{"value": VALUE, "type": "SECRET"}]),
        lambda: remember_scan(item),
        lambda: item._commit(item.entries, item.origins),
    ):
        with pytest.raises(PrivacySealConflict, match="^privacy_seal_conflict$"):
            operation()
    item.remember([])
    item.remember_scan([], [{"field": "source_excerpt", "text": VALUE}],
        call_id=str(uuid4()), scope="a" * 64, packet="b" * 64)
    assert item.index.read_bytes() == before


def test_seal_can_be_authenticated_in_memory_after_disposable_work_cleanup(tmp_path):
    item = ledger(tmp_path)
    remember_scan(item)
    revision = item.revision()
    sealed = item.seal(revision, CONTENT)
    work = (tmp_path / "work").resolve()
    assert work.is_relative_to(tmp_path.resolve()) and work != tmp_path.resolve()
    shutil.rmtree(work)
    with pytest.raises(PrivacySealConflict):
        item.remember([{"value": VALUE, "type": "SECRET"}])
    with pytest.raises(PrivacySealConflict):
        remember_scan(item)
    assert item.seal(revision, CONTENT) == sealed
    assert not work.exists()
    with pytest.raises(PrivacySealConflict):
        PrivacyLedger(work, create=False, seal_directory=tmp_path / "seals")
    assert not work.exists()


@pytest.mark.parametrize("target", ["seal.json", "state.json"])
def test_missing_or_corrupt_persistent_seal_material_is_not_open(tmp_path, target):
    item = ledger(tmp_path)
    remember_scan(item)
    item.seal(item.revision(), CONTENT)
    path = tmp_path / "seals" / target
    original = path.read_bytes()
    for invalid in (b"{}", b'{"version":1,"version":1}', b"[1]", b"x" * 5000):
        path.write_bytes(invalid)
        with pytest.raises(PrivacySealConflict):
            item.remember([{"value": VALUE, "type": "SECRET"}])
    path.write_bytes(original)
    path.unlink()
    with pytest.raises(PrivacySealConflict):
        item.seal(item.revision(), CONTENT)
    with pytest.raises(PrivacySealConflict):
        PrivacyLedger(tmp_path / "work", create=False, seal_directory=tmp_path / "seals")


def test_valid_json_tampering_and_replacement_key_cannot_authenticate_seal(tmp_path):
    item = ledger(tmp_path)
    remember_scan(item)
    revision = item.revision()
    item.seal(revision, CONTENT)
    path = tmp_path / "seals" / "seal.json"
    original = path.read_text(encoding="utf-8")
    changed = json.loads(original)
    changed["content_digest"] = "d" * 64
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(PrivacySealConflict):
        item.seal(revision, CONTENT)
    path.write_text(original, encoding="utf-8")
    (item.root / "key").write_bytes(b"z" * 32)
    with pytest.raises(PrivacySealConflict):
        item.seal(revision, CONTENT)
    with pytest.raises(PrivacySealConflict):
        PrivacyLedger(tmp_path / "work", create=False, seal_directory=tmp_path / "seals")


def test_missing_or_bad_key_while_open_fails_before_a_discovery(tmp_path):
    item = ledger(tmp_path)
    key = item.root / "key"
    key.unlink()
    with pytest.raises(PrivacySealConflict):
        remember_scan(item)
    with pytest.raises(PrivacySealConflict):
        PrivacyLedger(tmp_path / "work", create=False, seal_directory=tmp_path / "seals")
    key.write_bytes(b"bad")
    with pytest.raises(PrivacySealConflict):
        item.revision()


def test_closed_marker_without_seal_after_interruption_never_reopens(tmp_path, monkeypatch):
    item = ledger(tmp_path)
    revision = item.revision()
    write = item._seal_store._write

    def interrupted(path, value):
        if path.name == "seal.json":
            raise PrivacySealConflict()
        write(path, value)

    monkeypatch.setattr(item._seal_store, "_write", interrupted)
    with pytest.raises(PrivacySealConflict):
        item.seal(revision, CONTENT)
    assert json.loads((tmp_path / "seals" / "state.json").read_text())["status"] == "sealed"
    with pytest.raises(PrivacySealConflict):
        PrivacyLedger(tmp_path / "work", create=False, seal_directory=tmp_path / "seals")


def test_deleting_whole_seal_directory_cannot_reinitialize_an_existing_key(tmp_path):
    item = ledger(tmp_path)
    sealed_dir = (tmp_path / "seals").resolve()
    assert sealed_dir.is_relative_to(tmp_path.resolve()) and sealed_dir != tmp_path.resolve()
    shutil.rmtree(sealed_dir)
    with pytest.raises(PrivacySealConflict):
        PrivacyLedger(tmp_path / "work", seal_directory=sealed_dir)
    assert not sealed_dir.exists()


@pytest.mark.parametrize("invalid", [None, "", "a" * 63, "z" * 64, "私密" * 32, True, {"private": VALUE}])
def test_invalid_seal_arguments_have_a_fixed_error(tmp_path, invalid):
    item = ledger(tmp_path)
    revision = item.revision()
    for arguments in ((invalid, CONTENT), (revision, invalid)):
        with pytest.raises(PrivacySealConflict, match="^privacy_seal_conflict$"):
            item.seal(*arguments)


def test_discovery_and_seal_serialize_on_the_same_index_lock(tmp_path):
    for number in range(8):
        root = tmp_path / str(number)
        item = ledger(root)
        sibling = PrivacyLedger(root / "work", create=False, seal_directory=root / "seals")
        revision = item.revision()
        start = Barrier(2)

        def seal():
            start.wait()
            try:
                item.seal(revision, CONTENT)
                return "sealed"
            except PrivacySealConflict:
                return "seal_conflict"

        def discovery():
            start.wait()
            try:
                remember_scan(sibling)
                return "discovered"
            except PrivacySealConflict:
                return "discovery_conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            sealing, discovering = pool.submit(seal), pool.submit(discovery)
            result = {sealing.result(), discovering.result()}
        assert result in ({"sealed", "discovery_conflict"}, {"seal_conflict", "discovered"})


@pytest.mark.parametrize("same_content", [False, True])
def test_concurrent_sealers_are_idempotent_or_one_conflicts(tmp_path, same_content):
    item = ledger(tmp_path)
    sibling = PrivacyLedger(tmp_path / "work", create=False, seal_directory=tmp_path / "seals")
    revision = item.revision()
    start = Barrier(2)

    def seal(current, content):
        start.wait()
        try:
            return current.seal(revision, content)
        except PrivacySealConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(seal, item, CONTENT)
        second = pool.submit(seal, sibling, CONTENT if same_content else "d" * 64)
        results = [first.result(), second.result()]
    assert sum(result is not None for result in results) == (2 if same_content else 1)
    persisted = json.loads((tmp_path / "seals" / "seal.json").read_text())
    assert all(result == persisted for result in results if result is not None)


def test_key_and_first_open_anchor_initialize_in_one_locked_transaction(tmp_path):
    start = Barrier(2)

    def initialize():
        start.wait()
        return ledger(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(initialize), pool.submit(initialize)
        left, right = first.result(), second.result()
    assert left.key == right.key and left.revision() == right.revision()


def test_no_seal_directory_preserves_legacy_discoveries_and_does_not_add_files(tmp_path):
    item = PrivacyLedger(tmp_path / "work")
    remember_scan(item)
    with pytest.raises(PrivacySealConflict):
        item.seal(item.revision(), CONTENT)
    remember_scan(item, value="second-fixture")
    assert len(item.entries) == 2
    assert not (tmp_path / "seals").exists()


def test_retained_audit_has_only_private_authenticated_history_and_reopens(tmp_path):
    item = ledger(tmp_path)
    remember_scan(item)
    item.complete("a" * 64)
    revision = item.revision()
    sealed = item.seal(revision, CONTENT)
    destination = tmp_path / "audits" / "build"
    item.retain_audit(destination)
    private = destination / "privacy"
    assert {path.name for path in private.iterdir()} == {"key", "fingerprints.json", "state.json", "seal.json"}
    assert not (private / ("a" * 64 + ".json")).exists()
    for path in private.iterdir():
        assert VALUE.encode() not in path.read_bytes()
    item.retain_audit(destination)
    reopened = PrivacyLedger(destination, create=False, seal_directory=private)
    assert reopened.entries == item.entries and reopened.origins == item.origins
    assert reopened.seal(revision, CONTENT) == sealed
    item.retain_audit(destination)  # A mechanical read lock does not change history.
    work = (tmp_path / "work").resolve()
    assert work.is_relative_to(tmp_path.resolve()) and work != tmp_path.resolve()
    shutil.rmtree(work)
    assert VALUE not in reopened.mask(VALUE)
    assert reopened.seal(revision, CONTENT) == sealed


def test_retained_audit_requires_a_valid_seal_and_full_current_index(tmp_path):
    item = ledger(tmp_path)
    destination = tmp_path / "audit"
    with pytest.raises(PrivacySealConflict):
        item.retain_audit(destination)
    assert not destination.exists()
    remember_scan(item)
    item.seal(item.revision(), CONTENT)
    broken = json.loads(item.index.read_text())
    broken["index_hmac"] = "f" * 64
    item.index.write_text(json.dumps(broken))
    with pytest.raises(PrivacySealConflict):
        item.retain_audit(destination)
    assert not destination.exists()


@pytest.mark.parametrize("target", ["key", "fingerprints.json", "state.json", "seal.json", "extra"])
def test_existing_audit_mismatch_is_never_overwritten(tmp_path, target):
    item = ledger(tmp_path)
    remember_scan(item)
    item.seal(item.revision(), CONTENT)
    destination = tmp_path / "audit"
    item.retain_audit(destination)
    path = destination / "privacy" / target
    path.write_bytes(b"untrusted-audit-change")
    before = path.read_bytes()
    with pytest.raises(PrivacySealConflict):
        item.retain_audit(destination)
    assert path.read_bytes() == before


def test_audit_with_missing_index_cannot_reopen_as_empty_history(tmp_path):
    item = ledger(tmp_path)
    remember_scan(item)
    item.seal(item.revision(), CONTENT)
    destination = tmp_path / "audit"
    item.retain_audit(destination)
    (destination / "privacy" / "fingerprints.json").unlink()
    with pytest.raises(PrivacySealConflict):
        PrivacyLedger(destination, create=False, seal_directory=destination / "privacy")


def test_empty_sealed_history_retains_an_authenticated_empty_index(tmp_path):
    item = ledger(tmp_path)
    revision = item.revision()
    item.seal(revision, CONTENT)
    destination = tmp_path / "audit"
    item.retain_audit(destination)
    reopened = PrivacyLedger(destination, create=False, seal_directory=destination / "privacy")
    assert reopened.entries == {} and reopened.revision() == revision


def test_late_worker_conflict_invalidates_ready_and_fences_publication_preserving_costs(database, monkeypatch):
    from datetime import timedelta
    from unittest.mock import AsyncMock

    from src.models.brain import BrainBuild, BrainProject, BrainPublication, BrainUnit, utcnow
    from src.tasks import brain_task

    with database() as session:
        project = BrainProject(gitlab_project_id=1, project_name="fixture/project", branch="main")
        session.add(project)
        session.flush()
        build = BrainBuild(id=str(uuid4()), project_id=project.id, source_commit="a" * 40,
            recipe_hash="b" * 64, request_seq=1, brain_version=1, status="ready",
            coverage={"public_schema_version": "2.0"}, total_prompt_tokens=10, total_completion_tokens=5,
            total_cached_tokens=3, reserved_tokens=32)
        session.add(build)
        session.flush()
        project.active_build_id = build.id
        unit = BrainUnit(build_id=build.id, key="compile:0", kind="compile", status="running",
            lease_token="new-live-token", lease_until=utcnow() + timedelta(minutes=1),
            payload={"_reserved_tokens": 10, "_unknown_tokens": 7})
        publication = BrainPublication(build_id=build.id, target_branch="main", status="mr_open",
            mr_iid=8, commit_sha="d" * 40, mr_url="https://example.test/-/merge_requests/8")
        session.add_all([unit, publication])
        session.commit()
        session.expunge_all()
    monkeypatch.setattr(brain_task, "claim_unit", lambda _: (unit, build, project, "old-fenced-token"))
    monkeypatch.setattr(brain_task, "_execute", AsyncMock(side_effect=PrivacySealConflict()))
    assert brain_task.execute_brain_unit.run(unit.id) == {"status": "failed"}
    with database() as session:
        affected = session.get(BrainBuild, build.id)
        assert affected.status == "failed" and "failure_code=privacy_seal_conflict" in affected.error_message
        assert session.get(BrainProject, project.id).active_build_id is None
        failed = session.get(BrainPublication, publication.id)
        assert failed.status == "failed" and failed.error_code == "privacy_seal_conflict"
        assert failed.commit_sha == "d" * 40 and failed.mr_iid == 8
        assert (affected.total_prompt_tokens, affected.total_completion_tokens, affected.total_cached_tokens, affected.reserved_tokens) == (10, 5, 3, 32)
        assert session.get(BrainUnit, unit.id).payload["_unknown_tokens"] == 17


# Shared fixture only creates an isolated in-memory SQLite schema.
from tests.unit.test_project_brain import database  # noqa: E402, F401
