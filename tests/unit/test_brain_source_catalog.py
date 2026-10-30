"""Fixed synthetic Git sources stay authentic when display text is hidden."""

import copy
from pathlib import Path

import pytest

from src.config import settings
from src.core.project_brain.compiler import packet_hash, packets
from src.core.project_brain.exceptions import PrivacyCheckpointMissing, PrivacyIdentityConflict
from src.core.project_brain.extractor import FactExtractor
from src.core.project_brain.privacy import LedgerPrivacyFilter, PrivacyLedger
from src.core.project_brain.privacy_identity import SourceIdentityGuard
from src.core.project_brain.privacy_projection import bundle_privacy_records
from src.core.project_brain.source_catalog import SourceCatalog, identity_key
from src.core.project_brain.trusted_packet import VerifiedPacket, checkpoint_digest
from tests.unit.test_project_brain import git, repository, snapshot  # noqa: F401


@pytest.fixture
def catalog_case(snapshot, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    raw = FactExtractor(snapshot).extract()
    catalog = SourceCatalog(snapshot, raw, b"c" * 32, 480, "main")
    ledger = PrivacyLedger(tmp_path / "private")
    ledger.remember([{"value": "service", "type": "SECRET"}])
    return raw, catalog, LedgerPrivacyFilter(ledger)


def test_credential_equal_path_word_has_safe_display_and_canonical_identity(catalog_case):
    raw, catalog, privacy = catalog_case
    with pytest.raises(PrivacyIdentityConflict):
        SourceIdentityGuard(raw, privacy).check()
    source = catalog.bundle()
    before = copy.deepcopy(source)
    safe = catalog.safe_bundle(source, privacy)
    catalog.authenticate(safe)
    assert source == before
    assert safe.files == source.files and safe.modules == source.modules
    assert [r.id for r in safe.source_refs] == [r.id for r in source.source_refs]
    for before_ref, ref in zip(source.source_refs, safe.source_refs, strict=True):
        assert {k: v for k, v in before_ref.model_dump().items() if k != "display_path"} == {
            k: v for k, v in ref.model_dump().items() if k != "display_path"}
        assert ref.display_path == safe.display_paths[ref.path]
        assert "service" not in ref.display_path
    records = bundle_privacy_records(safe)
    assert all("service" not in record["text"] for record in records)
    assert safe.facts and safe.relations and safe.source_refs


def test_safe_bundle_observation_mirrors_equal_complete_primary_data(catalog_case):
    _, catalog, privacy = catalog_case
    safe = catalog.safe_bundle(catalog.bundle(), privacy)
    canonical = {"facts": {entry.id: entry.model_dump() for entry in safe.facts},
                 "refs": {entry.id: entry.model_dump() for entry in safe.source_refs},
                 "relations": {entry.id: entry.model_dump() for entry in safe.relations}}
    for observation in safe.observations.values():
        for field, index in canonical.items():
            assert all(entry == index[entry["id"]] for entry in observation[field])
    bundle_privacy_records(safe)


def test_safe_packets_use_real_snapshot_resolution_and_prove_exact_checkpoint(catalog_case):
    _, catalog, privacy = catalog_case
    safe = catalog.safe_bundle(catalog.bundle(), privacy)
    recipe = "offline-fixed-recipe"
    chunks = packets(safe, catalog.snapshot)
    entries = [{"hash": packet_hash(safe, recipe, packet), "packet_digest": checkpoint_digest(packet)}
               for packet in chunks]
    finalized = {"source_commit": safe.source_commit, "recipe_hash": recipe,
                 "bundle_hash": checkpoint_digest(safe.model_dump()), "packets": entries}
    for packet, planned in zip(chunks, entries, strict=True):
        proof = VerifiedPacket.from_checkpoint(packet, safe.model_dump(), source_commit=safe.source_commit,
            recipe_hash=recipe, finalize=finalized, privacy_states=["completed"], finalize_state="completed",
            planned_hash=planned["hash"])
        proof.validate(packet)
        projected = catalog.project_packet(packet, privacy)
        assert {e["id"] for item in projected["items"] for e in item["evidence"]} == {
            e["id"] for item in packet["items"] for e in item["evidence"]}


@pytest.mark.parametrize("change", ["path", "blob", "module", "relation", "attribute_path", "call_arities"])
def test_catalog_rejects_changed_machine_source_binding(catalog_case, change):
    _, catalog, _ = catalog_case
    source = catalog.bundle()
    if change == "path": source.source_refs[0].path = "sources/s" + "d" * 64
    elif change == "blob": source.source_refs[0].blob_sha = "f" * 40
    elif change == "module": source.facts[0].module_id = "module:" + "d" * 64
    elif change == "relation": source.relations[0].resolution = "candidate"
    elif change == "attribute_path": source.facts[0].attributes["path"] = "sources/s" + "d" * 64
    else: source.facts[0].attributes["call_arities"] = {"evidence:" + "f" * 64: 12}
    with pytest.raises(ValueError, match="source_binding_invalid"):
        catalog.safe_bundle(source, object())


def test_late_finding_equal_to_hash_does_not_rewrite_machine_attributes(catalog_case):
    _, catalog, privacy = catalog_case
    source = catalog.bundle()
    selected = next(f for f in source.facts if "path" in f.attributes)
    path = selected.attributes["path"]
    privacy.ledger.remember([{"value": path, "type": "SECRET"}])
    safe = catalog.safe_bundle(source, privacy)
    assert next(f for f in safe.facts if f.id == selected.id).attributes["path"] == path
    catalog.authenticate(safe)


def test_identity_material_requires_existing_key_and_is_stable(tmp_path):
    with pytest.raises(PrivacyCheckpointMissing):
        identity_key(tmp_path, 480, create=False)
    value = identity_key(tmp_path, 480, create=True)
    assert len(value) == 32
    assert identity_key(tmp_path, 480, create=False) == value
    assert identity_key(tmp_path, 481, create=True) != value
    assert (tmp_path / "source-identities" / "480" / "1" / "identity.key").is_file()


@pytest.mark.parametrize("damage", ["key_missing", "anchor_missing", "short_key", "anchor_changed"])
def test_existing_identity_cannot_be_silently_replaced(tmp_path, damage):
    identity_key(tmp_path, 480, create=True)
    root = tmp_path / "source-identities" / "480" / "1"
    if damage == "key_missing": (root / "identity.key").unlink()
    elif damage == "anchor_missing": (root / "identity.json").unlink()
    elif damage == "short_key": (root / "identity.key").write_bytes(b"short")
    else: (root / "identity.json").write_text('{"version":true}', encoding="utf-8")
    for create in (False, True):
        with pytest.raises(PrivacyCheckpointMissing):
            identity_key(tmp_path, 480, create=create)


def test_checkpoint_json_sort_roundtrip_preserves_authenticated_identity(catalog_case):
    import json
    from src.core.project_brain.schema import FactBundle
    _, catalog, privacy = catalog_case
    safe = catalog.safe_bundle(catalog.bundle(), privacy)
    mac = catalog.checkpoint_mac(safe)
    restored = FactBundle.model_validate(json.loads(json.dumps(safe.model_dump(), sort_keys=True)), strict=True)
    catalog.authenticate_checkpoint(restored, mac)


def test_source_values_equal_to_paths_or_ids_are_not_rewritten_as_identities(snapshot):
    raw = FactExtractor(snapshot).extract()
    java_fact = next(fact for fact in raw.facts if fact.attributes.get("language") == "java")
    java_fact.attributes["custom_description"] = java_fact.id
    old_id = java_fact.id
    for observation in raw.observations.values():
        observation["facts"] = [java_fact.model_dump() if entry["id"] == old_id else entry
                                for entry in observation.get("facts", [])]
    catalog = SourceCatalog(snapshot, raw, b"c" * 32, 480, "main")
    bound = next(fact for fact in catalog.bundle().facts if fact.attributes.get("custom_description") == old_id)
    assert bound.id != old_id
    assert bound.attributes["language"] == "java"
    assert bound.attributes["custom_description"] == old_id


def test_unknown_scalar_credential_masks_but_protocol_scalar_stays_exact(snapshot, tmp_path):
    raw = FactExtractor(snapshot).extract()
    selected = next(fact for fact in raw.facts if fact.kind == "symbol")
    selected.attributes.update(custom_numeric=12345678, custom_boolean=True, parameter_count=12345678)
    for observation in raw.observations.values():
        observation["facts"] = [selected.model_dump() if entry["id"] == selected.id else entry
                                for entry in observation.get("facts", [])]
    catalog = SourceCatalog(snapshot, raw, b"c" * 32, 480, "main")
    ledger = PrivacyLedger(tmp_path / "scalars")
    ledger.remember([{"value": "12345678", "type": "SECRET"}, {"value": "true", "type": "SECRET"}])
    safe = catalog.safe_bundle(catalog.bundle(), LedgerPrivacyFilter(ledger))
    fact = next(fact for fact in safe.facts if "custom_numeric" in fact.attributes)
    assert isinstance(fact.attributes["custom_numeric"], str) and "12345678" not in fact.attributes["custom_numeric"]
    assert isinstance(fact.attributes["custom_boolean"], str) and "true" not in fact.attributes["custom_boolean"]
    assert type(fact.attributes["parameter_count"]) is int and fact.attributes["parameter_count"] == 12345678
    catalog.authenticate(safe)
    bundle_privacy_records(safe)


def test_catalog_does_not_accept_display_path_as_file_locator(catalog_case):
    _, catalog, _ = catalog_case
    with pytest.raises(ValueError, match="source_locator_invalid"):
        catalog.snapshot.resolve_source("app/service.py")
    locator = next(k for k, v in catalog.paths.items() if v == "app/service.py")
    assert catalog.snapshot.resolve_source(locator).read_text(encoding="utf-8").startswith("from sqlalchemy")
