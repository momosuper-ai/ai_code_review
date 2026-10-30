"""Complete knowledge graphs must remain checksum-verifiable after export."""

import hashlib
import json
from uuid import uuid4

import pytest

from src.core.project_brain.artifacts import ArtifactStore, assemble_cards, validate
from src.core.project_brain.extractor import FactExtractor
from src.core.project_brain.schema import BrainManifest, Compilation
from tests.unit.test_project_brain import repository, snapshot  # noqa: F401


@pytest.fixture
def stored(snapshot, tmp_path):
    """Finalize a synthetic validated version, without models or GitLab."""
    bundle = FactExtractor(snapshot).extract()
    compilation = Compilation(title="模块职责", claims=[{"text": "合成模块用于静态测试。", "evidence_ids": bundle.facts[0].source_ref_ids}], unknowns=[])
    cards = assemble_cards(bundle, [(module, compilation) for module in bundle.modules], [compilation])
    manifest = BrainManifest(generator="test", build_id=str(uuid4()), project_id=1, branch="main",
        source_commit=bundle.source_commit, brain_version=1, recipe_hash="test-recipe",
        source_fingerprint=bundle.source_fingerprint, files={}, coverage=validate(bundle,cards,snapshot), issues=bundle.issues)
    store = ArtifactStore(str(tmp_path / "artifacts"))
    directory = store.finalize(manifest,bundle,cards,"https://fixture.invalid/project")
    return store,manifest.build_id,directory


def test_complete_service_graph_in_manifest_and_zip(stored):
    store,build_id,directory=stored
    manifest=store.read_manifest(build_id)
    assert set(manifest.service_files)=={"cards.json","facts.json","symbols.jsonl","relations.jsonl","source_refs.jsonl","index.json"}
    for name,digest in manifest.service_files.items():
        assert hashlib.sha256((directory/name).read_bytes()).hexdigest()==digest
    store.verify_service_files(build_id)
    assert store.read_bundle(build_id).source_commit==manifest.source_commit
    assert store.read_cards(build_id)


@pytest.mark.parametrize("name,reader",[("facts.json","read_bundle"),("cards.json","read_cards")])
def test_tampered_service_data_cannot_be_read_as_verified(stored,name,reader):
    store,build_id,directory=stored
    (directory/name).write_text("{}",encoding="utf-8")
    with pytest.raises(ValueError,match="哈希"):
        getattr(store,reader)(build_id)


def test_service_manifest_cannot_hide_missing_checksum(stored):
    store,build_id,directory=stored
    path=directory/"repository/docs/project-brain/manifest.json"
    manifest=json.loads(path.read_text(encoding="utf-8"))
    del manifest["service_files"]["facts.json"]
    path.write_text(json.dumps(manifest),encoding="utf-8")
    with pytest.raises(ValueError,match="未列入"):
        store.read_bundle(build_id)


def test_modified_export_cannot_be_downloaded_as_original(stored):
    """The DB archive checksum catches replacement independent of JSON files."""
    store, build_id, directory = stored
    archive = directory / "knowledge.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert store.export_path(build_id, digest) == archive
    archive.write_bytes(b"modified-archive")
    with pytest.raises(ValueError, match="ZIP 哈希"):
        store.export_path(build_id, digest)
