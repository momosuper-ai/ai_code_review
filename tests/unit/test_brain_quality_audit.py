"""Independent source-oracle and artifact-audit acceptance tests."""

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from scripts.brain_quality_audit import run_audit
from src.core.project_brain.snapshot import source_lines


def snapshot_fixture(root: Path, files: dict[str, str]) -> Path:
    """Write an inert fixed-SHA source fixture, never execute its code."""
    root.mkdir(parents=True, exist_ok=True)
    metadata = {}
    for path, text in files.items():
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8", newline="\n")
        content = text.encode()
        metadata[path] = {"blob_sha": hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest(),
                          "lines": len(source_lines(text)), "available": True, "mode": "100644"}
    (root / ".brain-snapshot.json").write_text(json.dumps({"commit_sha": "a" * 40, "files": metadata}), encoding="utf-8")
    return root


def artifact_fixture(root: Path, facts: list[dict], relations: list[dict] | None = None,
                     cards: list[dict] | None = None, markdown: str = "", checksums: bool = False) -> Path:
    """Write safe synthetic formal artifacts with an authenticated manifest."""
    root.mkdir()
    (root / "facts.json").write_text(json.dumps({"source_commit": "a" * 40, "facts": facts,
        "relations": relations or [], "source_refs": []}), encoding="utf-8")
    (root / "cards.json").write_text(json.dumps(cards or []), encoding="utf-8")
    repository = root / "repository"
    document = repository / "docs/project-brain/project.md"
    document.parent.mkdir(parents=True)
    document.write_text(markdown, encoding="utf-8")
    manifest = {"source_commit": "a" * 40, "files": {"docs/project-brain/project.md": hashlib.sha256(document.read_bytes()).hexdigest()}}
    if checksums:
        for name in ("symbols.jsonl", "relations.jsonl", "source_refs.jsonl", "index.json"):
            (root / name).write_text("{}\n", encoding="utf-8")
        manifest["service_files"] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("cards.json", "facts.json", "symbols.jsonl", "relations.jsonl", "source_refs.jsonl", "index.json")}
    (repository / "docs/project-brain/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_static_fastapi_mounts_and_aliases_without_ordinary_get_false_positive(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {
        "app/main.py": "from fastapi import FastAPI\nfrom app.routes import router as api\napp = FastAPI()\napp.include_router(api, prefix='/api')\n",
        "app/routes.py": "from fastapi import APIRouter\nrouter = APIRouter(prefix='/items')\n@router.get('/')\ndef listing():\n    return client.get('/not-a-route')\n"})
    report = run_audit(snapshot)
    routes = [item for item in report["oracle"]["items"] if item["category"] == "interface"]
    assert [(item["name"], item["attributes"]["handler"]) for item in routes] == [("GET /api/items/", "listing")]
    assert report["oracle"]["source_commit"] == "a" * 40
    assert all(item["source"]["line_start"] >= 1 for item in routes)


def test_dynamic_route_is_unknown_not_in_denominator(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get(PATH)\ndef listing():\n    pass\n"})
    report = run_audit(snapshot)
    assert not [item for item in report["oracle"]["items"] if item["category"] == "interface"]
    assert report["oracle"]["unknown_counts"]["dynamic_route"] == 1


def test_independent_direct_call_does_not_upgrade_shadowed_alias(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "from helper import work as imported\ndef confirmed():\n    return imported()\ndef shadowed(imported):\n    return imported()\n", "helper.py": "def work():\n    pass\n"})
    report = run_audit(snapshot)
    calls = [item for item in report["oracle"]["items"] if item["category"] == "direct_call"]
    assert [(item["attributes"]["owner"], item["attributes"]["target"]) for item in calls] == [("confirmed", "work")]


def test_configuration_keys_do_not_export_secret_values(tmp_path):
    secret = "glpat-0123456789abcdefghijklmnop"
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": f"import os\nfrom pydantic_settings import BaseSettings\nclass Config(BaseSettings):\n    PASSWORD: str = '{secret}'\nvalue = os.getenv('APP_MODE')\n"})
    report = run_audit(snapshot)
    assert secret not in json.dumps(report)
    assert {item["name"] for item in report["oracle"]["items"] if item["category"] == "configuration"} == {"PASSWORD", "APP_MODE"}


@pytest.fixture
def public_audit_fixture(tmp_path):
    """Schema 2 files are generated from inert source, never by a real model."""
    from uuid import uuid4
    from src.core.project_brain.artifacts import ArtifactStore, assemble_cards, validate
    from src.core.project_brain.extractor import FactExtractor
    from src.core.project_brain.privacy import LedgerPrivacyFilter, PrivacyLedger
    from src.core.project_brain.public_projection import project_public
    from src.core.project_brain.schema import BrainManifest, Compilation
    from src.core.project_brain.snapshot import RepoSnapshot, source_fingerprint
    from src.core.project_brain.source_catalog import SourceCatalog

    snapshot = snapshot_fixture(tmp_path / "source", {
        "rosecrab.py": 'import os\ndef run():\n    return os.getenv("APP_MODE", "rosecrab")\n'})
    metadata = json.loads((snapshot / ".brain-snapshot.json").read_text())
    fixed = RepoSnapshot(str(snapshot), metadata["commit_sha"], metadata["files"], [], source_fingerprint(metadata["files"]))
    raw = FactExtractor(fixed).extract()
    identity_key = tmp_path / "identity.key"
    identity_key.write_bytes(b"i" * 32)
    catalog = SourceCatalog(fixed, raw, identity_key.read_bytes(), 480, "main")
    ledger = PrivacyLedger(tmp_path / "private")
    ledger.remember([{"value": "rosecrab", "type": "SECRET"}])
    privacy = LedgerPrivacyFilter(ledger)
    safe = catalog.safe_bundle(catalog.bundle(), privacy)
    compilation = Compilation(title="fixture", claims=[{"text": "APP_MODE is read by run.", "evidence_ids": safe.facts[0].source_ref_ids}], unknowns=[])
    cards = assemble_cards(safe, [(module, compilation) for module in safe.modules], [compilation])
    manifest = BrainManifest(generator="project-brain/2.0.0", build_id=str(uuid4()), project_id=480, branch="main", source_commit=fixed.commit_sha,
        brain_version=1, recipe_hash="f" * 64, source_fingerprint=fixed.fingerprint, files={}, coverage=validate(safe, cards, catalog.snapshot), issues=[])
    public, cards, manifest = project_public(safe, cards, manifest, "https://fixture.invalid/project", privacy, source_paths=catalog.paths)
    artifact = ArtifactStore(str(tmp_path / "artifact-store")).finalize(manifest, public, cards, "https://fixture.invalid/project")
    return snapshot, artifact, ledger.root, identity_key


def test_public_audit_independently_locates_redacted_paths(public_audit_fixture):
    snapshot, artifact, ledger, identity_key = public_audit_fixture
    before = {path.name: path.read_bytes() for path in ledger.iterdir()}
    report = run_audit(snapshot, artifact, privacy_ledger=ledger, source_identity_key=identity_key, max_questions=50)
    encoded = json.dumps(report)
    assert "rosecrab" not in encoded
    assert "i" * 32 not in encoded
    assert report["audit_version"] == "2.0"
    assert report["artifact_integrity"]["service_file_hashes_valid"] is True
    assert report["fact_matching"]["invalid_source_references"] == []
    assert report["fact_matching"]["categories"]["declaration"]["recall"] == 1
    assert report["fact_matching"]["categories"]["configuration"]["recall"] == 1
    assert all(item["source"]["path"].startswith("sources/s") for item in report["oracle"]["items"])
    assert all(item["source"]["path_redacted"] and "url" not in item["source"] for item in report["oracle"]["items"])
    assert report["semantic_support"]["answer_accuracy"] is None
    assert before == {path.name: path.read_bytes() for path in ledger.iterdir()}


def test_public_audit_requires_private_identity_material(public_audit_fixture):
    snapshot, artifact, ledger, identity_key = public_audit_fixture
    with pytest.raises(ValueError, match="私有来源身份密钥"):
        run_audit(snapshot, artifact, privacy_ledger=ledger)
    with pytest.raises(ValueError, match="完整隐私账本"):
        run_audit(snapshot, artifact, source_identity_key=identity_key)
    with pytest.raises(ValueError, match="身份密钥无效"):
        run_audit(snapshot, artifact, privacy_ledger=ledger, source_identity_key=b"short")


def test_public_audit_does_not_substitute_display_paths_for_private_identity(public_audit_fixture):
    snapshot, artifact, ledger, identity_key = public_audit_fixture
    report = run_audit(snapshot, artifact, privacy_ledger=ledger, source_identity_key=b"x" * 32)
    assert report["fact_matching"]["invalid_source_references"]
    assert "rosecrab" not in json.dumps(report)
    assert report["semantic_support"]["answer_accuracy"] is None


def test_public_oracle_identity_is_keyed_and_unsafe_url_is_omitted(public_audit_fixture):
    snapshot, artifact, ledger, identity_key = public_audit_fixture
    report = run_audit(snapshot, artifact, privacy_ledger=ledger, source_identity_key=identity_key,
                       project_url="https://user:credential@fixture.invalid/project")
    assert all("url" not in item["source"] for item in report["oracle"]["items"])
    declaration = next(item for item in report["oracle"]["items"] if item["category"] == "declaration")
    raw = json.dumps(["declaration", "run", "rosecrab.py", 2, 3], ensure_ascii=False)
    assert declaration["id"] != "oracle:" + hashlib.sha256(raw.encode()).hexdigest()[:24]
    repeated = run_audit(snapshot, artifact, privacy_ledger=ledger, source_identity_key=identity_key)
    assert [item["id"] for item in report["oracle"]["items"]] == [item["id"] for item in repeated["oracle"]["items"]]


def test_pydantic_model_is_not_claimed_to_be_database_table(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "from pydantic import BaseModel\nclass Request(BaseModel):\n    value: str\n"})
    report = run_audit(snapshot)
    assert any(item["category"] == "data_model" for item in report["oracle"]["items"])
    assert not any(item["category"] == "data_table" for item in report["oracle"]["items"])


def test_missing_fact_and_document_presence_are_separate_from_answer_score(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "import os\nvalue = os.getenv('APP_MODE')\n"})
    artifact = artifact_fixture(tmp_path / "artifact", [], markdown="配置 APP_MODE 在此文档中出现。")
    report = run_audit(snapshot, artifact)
    metric = report["fact_matching"]["categories"]["configuration"]
    assert metric["oracle_denominator"] == 1 and metric["matched"] == 0 and metric["recall"] == 0
    assert report["document_checks"]["questions_with_all_terms_present"] == 1
    assert report["semantic_support"]["answer_accuracy"] is None
    assert report["questions"][0]["automatic_result"]["answer_correctness"] == "requires_manual_review"


def test_hierarchical_detail_references_are_audited_without_inventing_answer_score(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "def entry():\n    pass\n"})
    card = {"source_commit": "a" * 40, "claims": [], "detail_claims": [
        {"text": "完整详情也需要有效来源", "evidence_ids": ["missing-detail-source"], "relation_ids": []}]}
    artifact = artifact_fixture(tmp_path / "artifact", [], cards=[card], checksums=True)
    report = run_audit(snapshot, artifact)
    assert report["semantic_support"]["overview_claim_count"] == 0
    assert report["semantic_support"]["detail_claim_count"] == 1
    assert report["semantic_support"]["claims_with_invalid_references"] == 1
    assert report["semantic_support"]["answer_accuracy"] is None


def test_synthesis_views_are_reported_separately_from_answer_accuracy(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "def entry():\n    pass\n"})
    def claim(view):
        return {"text": "synthetic overview", "evidence_ids": [], "relation_ids": [],
                "source_scope": {"kind": "summary_synthesis", "relationship_view": view}}
    cards = [{"claims": [claim("resolution_counts")], "detail_claims": [claim("symbol_graph"), claim(None)]}]
    artifact = artifact_fixture(tmp_path / "artifact", [], cards=cards, checksums=True)
    support = run_audit(snapshot, artifact)["semantic_support"]
    assert support["synthesis_relationship_views"] == {"symbol_graph": 1, "resolution_counts": 1, "legacy_unspecified": 1}
    assert support["answer_accuracy"] is None


def test_candidate_relation_is_not_counted_as_confirmed_call(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "from helper import work\ndef run():\n    work()\n", "helper.py": "def work():\n    pass\n"})
    facts = [{"id": "source", "kind": "symbol", "name": "run", "attributes": {"path": "main.py"}},
             {"id": "target", "kind": "symbol", "name": "work", "attributes": {"path": "helper.py"}}]
    artifact = artifact_fixture(tmp_path / "artifact", facts, [{"id": "relation", "type": "CALLS", "from_id": "source", "to_id": None, "candidates": ["target"], "resolution": "candidate"}])
    report = run_audit(snapshot, artifact)
    metric = report["fact_matching"]["categories"]["direct_call"]
    assert metric["matched"] == 0 and metric["candidate_only"] == 1


def test_snapshot_blob_mismatch_fails_without_source_disclosure(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "def original():\n    pass\n"})
    (snapshot / "main.py").write_text("glpat-0123456789abcdefghijklmnop", encoding="utf-8")
    with pytest.raises(ValueError, match="快照 blob 校验失败"):
        run_audit(snapshot)


def test_artifact_hash_mismatch_is_rejected(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "value = 1\n"})
    artifact = artifact_fixture(tmp_path / "artifact", [])
    (artifact / "repository/docs/project-brain/project.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="产物哈希校验失败"):
        run_audit(snapshot, artifact)


def test_module_rebinding_is_not_confirmed_import_call(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "from helper import work\nif FLAG:\n    work = replacement\ndef run():\n    work()\n", "helper.py": "def work():\n    pass\n"})
    report = run_audit(snapshot)
    assert not [item for item in report["oracle"]["items"] if item["category"] == "direct_call"]


def test_factual_precision_has_explicit_generated_denominator(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "import os\nvalue = os.getenv('APP_MODE')\n"})
    facts = [{"id": "correct", "kind": "config", "name": "APP_MODE", "attributes": {"path": "main.py", "usage": "environment"}},
             {"id": "wrong", "kind": "config", "name": "INVENTED", "attributes": {"path": "main.py", "usage": "environment"}}]
    artifact = artifact_fixture(tmp_path / "artifact", facts)
    report = run_audit(snapshot, artifact)
    metric = report["fact_matching"]["categories"]["configuration"]
    assert metric["oracle_denominator"] == 1 and metric["generated_denominator"] == 2
    assert metric["recall"] == 1 and metric["precision"] == 0.5
    assert metric["unexpected_examples"][0]["name"] == "INVENTED"


def test_hmac_privacy_discovery_is_read_only_and_not_in_question_denominator(tmp_path):
    private_name = "opaque-internal-credential"
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": f"from fastapi import FastAPI\napp = FastAPI()\n@app.get('/{private_name}')\ndef handler():\n    pass\n"})
    ledger = tmp_path / "private" / "privacy"
    ledger.mkdir(parents=True)
    key = b"k" * 32
    (ledger / "key").write_bytes(key)
    digest = hmac.new(key, ("value:" + private_name).encode(), hashlib.sha256).hexdigest()
    (ledger / "fingerprints.json").write_text(json.dumps({digest: [len(private_name), "SECRET"]}), encoding="utf-8")
    before = {path.name: path.read_bytes() for path in ledger.iterdir()}
    report = run_audit(snapshot, privacy_ledger=ledger)
    assert private_name not in json.dumps(report)
    assert report["oracle"]["unknown_counts"]["privacy_redacted_oracle_items"] == 1
    assert before == {path.name: path.read_bytes() for path in ledger.iterdir()}


def test_version_two_hmac_discovery_masks_oracle_without_modifying_private_ledger(tmp_path):
    private_name = "opaque-internal-credential"
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": f"from fastapi import FastAPI\napp = FastAPI()\n@app.get('/{private_name}')\ndef handler():\n    pass\n"})
    ledger = tmp_path / "private" / "privacy"
    ledger.mkdir(parents=True)
    key = b"k" * 32
    (ledger / "key").write_bytes(key)
    digest = hmac.new(key, ("value:" + private_name).encode(), hashlib.sha256).hexdigest()
    # Read-only audit validates structure and applies discoveries; it does not
    # authenticate the worker's provenance MAC or claim the origin is known.
    checkpoint = {"version": 2, "index_hmac": "0" * 64, "entries": {digest: [len(private_name), "SECRET"]},
                  "origins": {"calls": {}, "findings": {digest: {"legacy_unknown": True, "records": {}}}}}
    (ledger / "fingerprints.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    before = {path.name: path.read_bytes() for path in ledger.iterdir()}

    report = run_audit(snapshot, privacy_ledger=ledger)

    assert private_name not in json.dumps(report)
    assert digest not in json.dumps(report)
    assert report["oracle"]["unknown_counts"]["privacy_redacted_oracle_items"] == 1
    assert before == {path.name: path.read_bytes() for path in ledger.iterdir()}


@pytest.mark.parametrize("mutation", ["envelope_field", "origin_field", "finding_field", "unknown_type", "missing_finding"])
def test_version_two_invalid_metadata_fails_before_oracle_read_with_closed_error(tmp_path, mutation):
    # There is deliberately no snapshot: invalid private metadata must fail first.
    ledger = tmp_path / "private" / "privacy"
    ledger.mkdir(parents=True)
    (ledger / "key").write_bytes(b"k" * 32)
    digest = "a" * 64
    checkpoint = {"version": 2, "index_hmac": "0" * 64, "entries": {digest: [12, "SECRET"]},
                  "origins": {"calls": {}, "findings": {digest: {"legacy_unknown": True, "records": {}}}}}
    marker = "untrusted-context-must-never-escape"
    if mutation == "envelope_field":
        checkpoint[marker] = marker
    elif mutation == "origin_field":
        checkpoint["origins"][marker] = marker
    elif mutation == "finding_field":
        checkpoint["origins"]["findings"][digest][marker] = marker
    elif mutation == "unknown_type":
        checkpoint["entries"][digest][1] = marker
    else:
        checkpoint["origins"]["findings"] = {}
    (ledger / "fingerprints.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    before = {path.name: path.read_bytes() for path in ledger.iterdir()}
    with pytest.raises(ValueError, match="审计隐私指纹无效") as error:
        run_audit(tmp_path / "missing-source", privacy_ledger=ledger)
    assert marker not in str(error.value)
    assert before == {path.name: path.read_bytes() for path in ledger.iterdir()}


def test_duplicate_fingerprint_fields_fail_without_creating_ledger_files(tmp_path):
    ledger = tmp_path / "private" / "privacy"
    ledger.mkdir(parents=True)
    (ledger / "key").write_bytes(b"k" * 32)
    digest = "a" * 64
    raw = '{"' + digest + '":[12,"SECRET"],"' + digest + '":[12,"KEY"]}'
    (ledger / "fingerprints.json").write_text(raw, encoding="utf-8")
    before = {path.name: path.read_bytes() for path in ledger.iterdir()}
    with pytest.raises(ValueError, match="审计隐私指纹无效"):
        run_audit(tmp_path / "missing-source", privacy_ledger=ledger)
    assert before == {path.name: path.read_bytes() for path in ledger.iterdir()}


def test_invalid_source_reference_is_counted_independently(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "import os\nvalue = os.getenv('APP_MODE')\n"})
    artifact = artifact_fixture(tmp_path / "artifact", [])
    bundle_path = artifact / "facts.json"
    bundle = json.loads(bundle_path.read_text())
    bundle["source_refs"] = [{"id": "wrong-blob", "path": "main.py", "commit_sha": "a" * 40,
                              "blob_sha": "b" * 40, "line_start": 2, "line_end": 2}]
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    report = run_audit(snapshot, artifact)
    assert report["fact_matching"]["invalid_source_references"] == [{"id": "wrong-blob", "code": "invalid_source_ref"}]


def test_unicode_literal_does_not_shift_independent_oracle_line_number(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {".env.example": "NOTE=first\u2028second\nMODE=safe\n"})
    report = run_audit(snapshot)
    item = next(item for item in report["oracle"]["items"] if item["name"] == "MODE")
    assert item["source"]["line_start"] == 2


def test_main_guard_local_uvicorn_alias_has_matchable_exact_entrypoint(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "\n\nif __name__ == '__main__':\n    import uvicorn as server\n    server.run('app.main:app')\n"})
    report = run_audit(snapshot)
    items = [item for item in report["oracle"]["items"] if item["category"] == "entrypoint"]
    assert [(item["name"], item["source"]["line_start"]) for item in items] == [("Python 启动入口", 3), ("uvicorn.run", 5)]
    assert items[1]["attributes"]["fact_kind"] == "entrypoint"
    assert "independent_only" not in items[1]["attributes"]


def test_uvicorn_rebinding_and_function_local_calls_are_not_startup_oracle(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "import uvicorn\nuvicorn = replacement\nuvicorn.run('app:app')\ndef deferred(uvicorn):\n    uvicorn.run('app:app')\n"})
    report = run_audit(snapshot)
    assert not [item for item in report["oracle"]["items"] if item["name"] == "uvicorn.run"]


def test_sql_ddl_trigger_update_on_is_not_a_table_access(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "def install(cursor):\n    cursor.execute('CREATE TRIGGER touched BEFORE UPDATE ON users FOR EACH ROW SET NEW.flag = 1')\n"})
    report = run_audit(snapshot)
    assert not [item for item in report["oracle"]["items"] if item["category"] == "table_access"]
    assert report["oracle"]["unknown_counts"]["unsupported_sql_ddl"] == 1


def test_generic_execute_literal_is_unknown_for_unadapted_driver(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "import pymysql\ndef load(cursor):\n    cursor.execute('SELECT * FROM users')\n"})
    report = run_audit(snapshot)
    assert not [item for item in report["oracle"]["items"] if item["category"] == "table_access"]
    assert report["oracle"]["unknown_counts"]["unsupported_sql_driver"] == 1


@pytest.mark.parametrize("name", ["cards.json", "facts.json", "symbols.jsonl", "relations.jsonl", "source_refs.jsonl", "index.json"])
def test_all_service_side_artifacts_have_verified_hashes(tmp_path, name):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "value = 1\n"})
    artifact = artifact_fixture(tmp_path / "artifact", [], checksums=True)
    (artifact / name).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="服务侧产物哈希校验失败"):
        run_audit(snapshot, artifact)


def test_new_service_checksum_manifest_reports_all_six_checks(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "value = 1\n"})
    artifact = artifact_fixture(tmp_path / "artifact", [], checksums=True)
    report = run_audit(snapshot, artifact)
    assert report["artifact_integrity"]["service_file_hashes_valid"] is True
    assert report["artifact_integrity"]["service_files_checked"] == 6


def test_partial_service_checksum_manifest_is_rejected(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "value = 1\n"})
    artifact = artifact_fixture(tmp_path / "artifact", [], checksums=True)
    manifest_path = artifact / "repository/docs/project-brain/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["service_files"].pop("index.json")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="服务侧产物校验清单不完整"):
        run_audit(snapshot, artifact)


def test_malformed_empty_service_checksum_manifest_is_not_legacy(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "value = 1\n"})
    artifact = artifact_fixture(tmp_path / "artifact", [])
    manifest_path = artifact / "repository/docs/project-brain/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["service_files"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="服务侧产物校验清单不完整"):
        run_audit(snapshot, artifact)


def test_legacy_manifest_has_explicit_service_checksum_limitation(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "value = 1\n"})
    artifact = artifact_fixture(tmp_path / "artifact", [])
    report = run_audit(snapshot, artifact)
    assert report["artifact_integrity"]["service_file_hashes_valid"] is False
    assert any("服务侧" in limitation and "校验" in limitation for limitation in report["limitations"])


def test_rule_assignment_discovery_redacts_later_bare_oracle_names(tmp_path):
    private_value = "opaque_internal_name"
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": f"from fastapi import FastAPI\nPASSWORD = '{private_value}'\napp = FastAPI()\n@app.get('/{private_value}')\ndef handler():\n    pass\n"})
    report = run_audit(snapshot)
    assert private_value not in json.dumps(report)
    assert report["oracle"]["unknown_counts"]["privacy_redacted_oracle_items"] == 1


def test_explicit_sqlalchemy_text_keeps_static_dml_access(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "from sqlalchemy import text\ndef load():\n    text('SELECT * FROM users')\n    text('UPDATE accounts SET enabled = 1')\n"})
    report = run_audit(snapshot)
    items = [item for item in report["oracle"]["items"] if item["category"] == "table_access"]
    assert {(item["name"], item["attributes"]["operation"]) for item in items} == {("users", "READS_TABLE"), ("accounts", "WRITES_TABLE")}


def test_uvicorn_dynamic_application_and_docstring_guard_are_unknown(tmp_path):
    snapshot = snapshot_fixture(tmp_path / "source", {"main.py": "'''\nif __name__ == '__main__':\n    start()\n'''\nimport uvicorn\nuvicorn.run(factory())\n"})
    report = run_audit(snapshot)
    assert not [item for item in report["oracle"]["items"] if item["category"] == "entrypoint"]
    assert report["oracle"]["unknown_counts"]["dynamic_asgi_application"] == 1
