"""Policy findings are authenticated without pretending to be model calls."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from src.core.project_brain.exceptions import PrivacySealConflict
from src.core.project_brain.privacy import PrivacyLedger
from src.core.project_brain.privacy_provenance import decode_fingerprint_index
from src.config import settings
from src.core.project_brain.compiler import CompilationGuard, encoded

VALUE = "synthetic_policy_credential"


def add(ledger, *, kind="inherited_hmac_match", context=None):
    source = ({"source_build_id": "00000000-0000-0000-0000-000000000001",
               "source_commit": "a" * 40, "source_snapshot": "b" * 64, "source_finding": "e" * 64}
              if kind == "inherited_hmac_match" else None)
    ledger.remember_policy([{"value": VALUE, "type": "SECRET"}],
        [{"field": "source_excerpt", "text": "password=" + VALUE}], kind=kind,
        scope=ledger.digest("input"), context=context, source=source)


@pytest.mark.parametrize("kind", ["inherited_hmac_match", "deterministic_rule"])
def test_policy_persists_hmac_without_sdk_call_or_plaintext(tmp_path, kind):
    item = PrivacyLedger(tmp_path)
    add(item, kind=kind)
    raw = item.index.read_text()
    assert VALUE not in raw and "password=" not in raw
    data = json.loads(raw)
    assert data["version"] == 3
    entries, origins = decode_fingerprint_index(data)
    assert len(entries) == 1 and origins["calls"] == {}
    finding = next(iter(origins["findings"].values()))
    assert not finding["legacy_unknown"] and finding["records"] == {}
    assert {p["kind"] for p in finding["policy_records"].values()} == {kind}
    restarted = PrivacyLedger(tmp_path, create=False)
    assert VALUE not in restarted.mask("src/" + VALUE + "/config.py")


def test_policy_merge_and_model_discoveries_preserve_each_other(tmp_path):
    item = PrivacyLedger(tmp_path)
    add(item)
    item.remember_scan([{"value": VALUE, "type": "SECRET"}], [{"field": "source_excerpt", "text": VALUE}],
        call_id=str(uuid4()), scope=item.digest("scan"), packet=item.digest("packet"), context=None)
    add(item, kind="deterministic_rule")
    finding = next(iter(item.origins["findings"].values()))
    assert len(item.origins["calls"]) == 1
    assert len(finding["records"]) == 1 and len(finding["policy_records"]) == 2
    assert len(PrivacyLedger(tmp_path, create=False).entries) == 1


def test_policy_same_occurrence_is_idempotent(tmp_path):
    item = PrivacyLedger(tmp_path)
    add(item)
    before = item.index.read_bytes()
    add(item)
    assert item.index.read_bytes() == before


def test_project_placeholder_numbers_are_stable_across_build_ledgers(tmp_path):
    from src.core.project_brain.privacy import project_placeholder_key
    from src.core.project_brain.source_catalog import identity_key

    secret = "synthetic_project_credential"
    identity_key(tmp_path, 480, create=True)
    identity_key(tmp_path, 481, create=True)
    placeholder_key = project_placeholder_key(tmp_path, 480)
    other_project_key = project_placeholder_key(tmp_path, 481)
    first = PrivacyLedger(tmp_path / "build-one", placeholder_key=placeholder_key)
    second = PrivacyLedger(tmp_path / "build-two", placeholder_key=placeholder_key)
    other_project = PrivacyLedger(tmp_path / "build-other-project", placeholder_key=other_project_key)
    first.remember([{"value": secret, "type": "SECRET"}])
    second.remember([{"value": secret, "type": "SECRET"}])
    other_project.remember([{"value": secret, "type": "SECRET"}])
    text = "token=" + secret

    assert first.mask(text) == second.mask(text)
    assert secret not in first.mask(text)
    assert first.mask(text) != other_project.mask(text)


def test_placeholder_key_controls_numbering_and_none_preserves_legacy(tmp_path):
    secret = "synthetic_legacy_credential"
    first = PrivacyLedger(tmp_path / "first", placeholder_key=b"a" * 32)
    second = PrivacyLedger(tmp_path / "second", placeholder_key=b"b" * 32)
    legacy = PrivacyLedger(tmp_path / "legacy")
    for ledger in (first, second, legacy):
        ledger.remember([{"value": secret, "type": "SECRET"}])

    assert first.mask(secret) != second.mask(secret)
    legacy_digest = legacy.digest("value:" + secret)
    assert legacy.mask(secret) == f"<<SECRET_{int(legacy_digest[:16], 16)}>>"


def test_policy_cannot_invent_a_value_or_sdk_origin(tmp_path):
    item = PrivacyLedger(tmp_path)
    with pytest.raises(ValueError):
        item.remember_policy([{"value": VALUE, "type": "SECRET"}], [{"field": "text", "text": "ordinary"}],
            kind="deterministic_rule", scope=item.digest("scope"))
    assert not item.entries and not item.index.exists()
    with pytest.raises(ValueError):
        item.remember_policy([{"value": VALUE, "type": "SECRET"}], [{"field": "text", "text": VALUE}],
            kind="deterministic_rule", scope=item.digest("scope"), source={"call_id": str(uuid4())})


def test_policy_modified_metadata_fails_full_mac(tmp_path):
    item = PrivacyLedger(tmp_path)
    add(item)
    data = json.loads(item.index.read_text())
    finding = next(iter(data["origins"]["findings"].values()))
    next(iter(finding["policy_records"].values()))["source_snapshot"] = "c" * 64
    # Even a correctly re-bound outer index cannot bypass per-policy binding.
    data["index_hmac"] = item._index_binding(data)
    item.index.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="隐私指纹或来源检查点无效"):
        PrivacyLedger(tmp_path, create=False)


def test_policy_seal_and_four_file_audit(tmp_path):
    item = PrivacyLedger(tmp_path / "work", seal_directory=tmp_path / "seals")
    add(item)
    revision = item.revision()
    item.seal(revision, "d" * 64)
    item.retain_audit(tmp_path / "audit")
    reopened = PrivacyLedger(tmp_path / "audit", create=False, seal_directory=tmp_path / "audit/privacy")
    assert reopened.revision() == revision
    assert VALUE not in reopened.mask(VALUE)
    assert {p.name for p in (tmp_path / "audit/privacy").iterdir()} <= {
        "key", "fingerprints.json", "state.json", "seal.json", "index.lock"}
    with pytest.raises(PrivacySealConflict):
        add(item, kind="deterministic_rule")


@pytest.mark.asyncio
async def test_rule_discovery_is_shared_before_model_or_receipt_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", False)
    item = PrivacyLedger(tmp_path)
    charged = []
    guard = CompilationGuard(lambda *args: charged.append(True), lambda *args: None, item)
    guard.persist_rule_findings = True
    text = encoded({"text": 'password="' + VALUE + '"'})
    # A preexisting receipt does not erase newly applied deterministic policy.
    item.complete(item.digest("input:" + text))
    try:
        await guard.clean(text)
        assert VALUE not in PrivacyLedger(tmp_path, create=False).mask("src/" + VALUE + "/config.py")
        finding = next(iter(item.origins["findings"].values()))
        assert {r["kind"] for r in finding["policy_records"].values()} == {"deterministic_rule"}
        assert not charged and not item.origins["calls"]
    finally:
        await guard.close()
