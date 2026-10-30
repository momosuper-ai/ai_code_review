"""The public graph never doubles as a canonical parser checkpoint."""

import hashlib
import json
import zipfile
from uuid import uuid4

import pytest

from src.core.project_brain.artifacts import ArtifactStore, assemble_cards
from src.core.project_brain.privacy import LedgerPrivacyFilter, PrivacyLedger
from src.core.project_brain.public_projection import project_public, project_value, safe_public_url
from src.core.project_brain.public_schema import PublicBundle, PublicValue, parse_manifest
from src.core.project_brain.schema import BrainManifest, Claim, Fact, FactBundle, KnowledgeCard, Relation, SourceRef


@pytest.fixture
def projection(tmp_path):
    ledger = PrivacyLedger(tmp_path / "private")
    ledger.remember([{"value": "rosecrab", "type": "SECRET"}])
    privacy = LedgerPrivacyFilter(ledger)
    files = {"opaque/first": "src/rosecrab.py", "opaque/second": "src/plain.py"}
    bundle = FactBundle(source_commit="a" * 40, source_fingerprint="b" * 64,
        files={locator: {"available": True, "lines": 3, "blob_sha": "c" * 40} for locator in files},
        modules={"module:one": "opaque/first"},
        facts=[Fact(id="fact:one", kind="function", name="rosecrab", module_id="module:one", source_ref_ids=["ref:one"],
                    extractor="python", attributes={"rosecrab": {"nested": "rosecrab"}, "normal": [True, None, 3, 1.25]})],
        relations=[Relation(id="rel:one", from_id="fact:one", type="CALLS", to_id=None, target_name="rosecrab",
                            resolution="candidate", source_ref_ids=["ref:one"], candidates=["fact:one"])],
        source_refs=[SourceRef(id="ref:one", commit_sha="a" * 40, path="opaque/first", blob_sha="c" * 40,
                              line_start=2, line_end=3, symbol_id="fact:one"),
                     SourceRef(id="ref:two", commit_sha="a" * 40, path="opaque/second", blob_sha="c" * 40,
                              line_start=1, line_end=1)], observations={"opaque/first": {"imports": {"rosecrab": "rosecrab"}}},
        issues=["rosecrab is hidden"], frameworks=["test"])
    bundle = bundle.model_copy(update={"source_identity": {"version": "source/1", "key_version": "1", "branch_id": "branch:one"},
                                      "display_paths": {locator: privacy.sanitize_text(path) for locator, path in files.items()}})
    card = KnowledgeCard(id="card:one", kind="module", title="rosecrab", module_id="module:one", source_commit=bundle.source_commit,
        claims=[Claim(text="rosecrab reads configuration", knowledge_type="AI_INFERRED", evidence_ids=["ref:one"])],
        detail_claims=[Claim(text="rosecrab detail", knowledge_type="FACT", evidence_ids=["ref:two"])],
        unknowns=["rosecrab unknown"], detail_unknowns=["rosecrab detail unknown"],
        document_path="docs/project-brain/rosecrab.md")
    manifest = BrainManifest(generator="test", build_id=str(uuid4()), project_id=480, branch="rosecrab",
        source_commit=bundle.source_commit, brain_version=1, recipe_hash="e" * 64, source_fingerprint=bundle.source_fingerprint,
        files={}, coverage={"files": 2, "note": "rosecrab"}, issues=bundle.issues)
    projected = project_public(bundle, [card], manifest, "https://fixture.invalid/group/project", privacy,
                               source_paths=files, branch_id="branch:one")
    return bundle, [card], manifest, privacy, files, projected


def test_all_public_views_are_safe_and_keep_identity(projection):
    bundle, cards, manifest, privacy, paths, (public, public_cards, public_manifest) = projection
    serialized = json.dumps([public.model_dump(), [c.model_dump() for c in public_cards], public_manifest.model_dump()])
    assert "rosecrab" not in serialized
    assert set(public.files) == set(bundle.files)
    assert set(public.observations) == set(bundle.observations)
    assert {r.id for r in public.source_refs} == {r.id for r in bundle.source_refs}
    assert public.relations[0].resolution == "candidate"
    assert public.relations[0].candidates == ["fact:one"]
    assert len(public_cards[0].detail_claims) == len(cards[0].detail_claims)
    assert len(public_cards[0].detail_unknowns) == len(cards[0].detail_unknowns)
    assert public.source_refs[0].source_url is None
    assert public.source_refs[0].path_redacted
    assert public.source_refs[1].source_url.endswith("/src/plain.py#L1-1")
    assert public.source_refs[1].commit_sha == "a" * 40
    assert public_cards[0].document_path.startswith("docs/project-brain/cards/")
    assert public_manifest.public_metadata.branch_display != manifest.branch
    assert public_manifest.public_metadata.branch_id == "branch:one"


def test_object_keys_can_collide_without_losing_children():
    value = project_value({"secret_a": {"secret_c": 1}, "secret_b": 2}, lambda text: "hidden" if text.startswith("secret") else text)
    assert len(value.entries) == 2
    assert value.entries[0].key == value.entries[1].key == "hidden"
    assert value.entries[0].id != value.entries[1].id
    assert value.entries[0].value.entries[0].value.number == 1
    assert value.entries[1].value.number == 2


def test_source_derived_numeric_secret_is_replaced_as_display_text():
    value = project_value({"credential": 943517}, lambda text: "<<SECRET_1>>" if text == "943517" else text)
    member = value.entries[0].value
    assert member.kind == "string" and member.text == "<<SECRET_1>>"
    assert member.number is None


@pytest.mark.parametrize("value", [object(), {1: "not a JSON key"}, float("nan"), b"bytes"])
def test_unknown_attribute_types_fail_instead_of_disappearing(value):
    with pytest.raises(ValueError, match="类型"):
        project_value(value, lambda text: text)


def test_public_tree_rejects_ambiguous_shape():
    with pytest.raises(ValueError):
        PublicValue(kind="null", text="unexpected")
    with pytest.raises(ValueError):
        PublicValue(kind="number", number=True)


def test_module_cards_display_names_do_not_replace_ownership(projection):
    from src.core.project_brain.schema import Compilation

    bundle, _, _, _, _, _ = projection
    compilation = Compilation(title="module", claims=[{"text": "module behavior", "evidence_ids": ["ref:one"]}], unknowns=[])
    cards = assemble_cards(bundle, [("module:one", compilation)], [compilation])
    module = next(card for card in cards if card.kind == "module")
    assert module.module_id == "module:one"
    assert module.title == bundle.display_paths["opaque/first"]
    assert module.title != "opaque/first"


def test_source_graph_with_missing_reference_fails_before_finalization(projection):
    bundle, cards, manifest, privacy, paths, _ = projection
    bundle.facts[0].source_ref_ids = ["unknown-reference"]
    with pytest.raises(ValueError, match="来源"):
        project_public(bundle, cards, manifest, "https://fixture.invalid/project", privacy, source_paths=paths)


def test_missing_private_branch_identity_is_not_guessed(projection):
    bundle, cards, manifest, privacy, paths, _ = projection
    bundle.source_identity.pop("branch_id")
    with pytest.raises(ValueError, match="分支身份"):
        project_public(bundle, cards, manifest, "https://fixture.invalid/project", privacy, source_paths=paths)


def test_finalized_zip_is_public_only_and_cannot_be_parser_cache(projection, tmp_path):
    _, _, _, _, _, (public, cards, manifest) = projection
    store = ArtifactStore(str(tmp_path / "public"))
    destination = store.finalize(manifest, public, cards, "https://fixture.invalid/group/project")
    assert isinstance(store.read_public_bundle(manifest.build_id), PublicBundle)
    with pytest.raises(ValueError, match="规范提取缓存"):
        store.read_bundle(manifest.build_id)
    parsed = store.read_manifest(manifest.build_id)
    assert parsed.schema_version == "2.0"
    assert parse_manifest(parsed.model_dump_json()) == parsed
    assert store.read_cards(manifest.build_id)[0].schema_version == "2.0"
    with zipfile.ZipFile(destination / "knowledge.zip") as archive:
        expected = {*manifest.service_files, *("repository/" + path for path in manifest.files),
                    "repository/docs/project-brain/manifest.json"}
        assert set(archive.namelist()) == expected
        assert all("rosecrab" not in name for name in archive.namelist())
        assert all(b"rosecrab" not in archive.read(name) for name in archive.namelist())
        rendered = "\n".join(archive.read(name).decode() for name in archive.namelist() if name.endswith(".md"))
        assert "路径已脱敏" in rendered
        assert "/-/blob/" in rendered  # safe source is still a real fixed-SHA link
    assert not (destination / "privacy").exists()
    assert set(store.repository(manifest.build_id)) == {*manifest.files, "docs/project-brain/manifest.json"}


def test_changed_projection_rejected_before_write(projection, tmp_path):
    _, _, _, _, _, (public, cards, manifest) = projection
    cards[0].title = "changed after projection"
    store = ArtifactStore(str(tmp_path / "public"))
    with pytest.raises(ValueError, match="投影认证"):
        store.finalize(manifest, public, cards, "https://fixture.invalid/project")
    assert not store.directory(manifest.build_id).exists()


def test_canonical_bundle_is_not_accepted_on_public_volume(projection, tmp_path):
    bundle, cards, manifest, _, _, _ = projection
    with pytest.raises(ValueError, match="独立公开投影"):
        ArtifactStore(str(tmp_path / "public")).finalize(manifest, bundle, cards, "https://fixture.invalid/project")


def test_public_graph_never_takes_legacy_unhashed_compatibility_path(projection, tmp_path):
    _, _, _, _, _, (public, cards, manifest) = projection
    store = ArtifactStore(str(tmp_path / "public"))
    directory = store.finalize(manifest, public, cards, "https://fixture.invalid/project")
    path = directory / "repository/docs/project-brain/manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["service_files"] = {}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="完整校验清单"):
        store.read_public_bundle(manifest.build_id)


@pytest.mark.parametrize("url", ["https://user:password@fixture.invalid/repo", "https://fixture.invalid/repo?token=x",
                              "https://fixture.invalid/repo#fragment", "javascript:alert(1)",
                              "https://fixture.invalid/rosecrab", "https://fixture.invalid\\@evil.invalid"])
def test_invalid_or_changed_urls_have_no_safe_replacement(projection, url):
    _, _, _, privacy, _, _ = projection
    assert safe_public_url(url, privacy) is None


def test_missing_private_paths_disables_links_without_guessing(projection):
    bundle, cards, manifest, privacy, _, _ = projection
    public, _, _ = project_public(bundle, cards, manifest, "https://fixture.invalid/group/project", privacy)
    assert all(ref.source_url is None for ref in public.source_refs)


def test_late_findings_reproject_without_changing_canonical_objects(projection):
    bundle, cards, manifest, privacy, paths, first = projection
    original = bundle.model_dump_json(), [card.model_dump_json() for card in cards]
    privacy.ledger.remember([{"value": "plain", "type": "SECRET"}])
    second = project_public(bundle, cards, manifest, "https://fixture.invalid/group/project", privacy, source_paths=paths)
    assert original == (bundle.model_dump_json(), [card.model_dump_json() for card in cards])
    assert first[0].source_refs[1].source_url is not None
    assert second[0].source_refs[1].source_url is None
    assert first[2].projection.privacy_seal != second[2].projection.privacy_seal
    assert {ref.id for ref in first[0].source_refs} == {ref.id for ref in second[0].source_refs}


def test_exact_protocol_positions_survive_same_value_secret(projection):
    bundle, cards, manifest, privacy, paths, _ = projection
    bundle.facts[0].kind = "symbol"
    bundle.facts[0].extractor = "brain.python/1"
    bundle.facts[0].attributes.update(language="python", symbol_kind="function", parameter_count=2)
    manifest.coverage.update(public_schema_version="2.0", semantic_support_status="unreviewed")
    for value in ("symbol", "python", "function", "CALLS", "files", "partial", "available", "unreviewed"):
        privacy.ledger.remember([{"value": value, "type": "SECRET"}])
    public, projected_cards, public_manifest = project_public(bundle, cards, manifest, "https://fixture.invalid/project", privacy, source_paths=paths)
    assert public.facts[0].kind == "symbol"
    assert public.facts[0].extractor == "brain.python/1"
    assert public.relations[0].type == "CALLS"
    attributes = {entry.key: entry.value for entry in public.facts[0].attributes.entries}
    assert attributes["language"].text == "python"
    assert attributes["symbol_kind"].text == "function"
    assert projected_cards[0].coverage == "partial"
    assert public_manifest.coverage["files"] == 2
    assert public_manifest.coverage["semantic_support_status"] == "unreviewed"
    assert public_manifest.projection.privacy_seal == privacy.ledger.revision()


def test_duplicate_safe_document_paths_use_distinct_opaque_fallbacks(projection):
    bundle, cards, manifest, privacy, paths, _ = projection
    cards[0].document_path = "docs/project-brain/shared.md"
    cards.append(cards[0].model_copy(update={"id": "card:two"}))
    _, public_cards, _ = project_public(bundle, cards, manifest, "https://fixture.invalid/project", privacy, source_paths=paths)
    assert len({card.document_path for card in public_cards}) == 2
    assert all(card.document_path.startswith("docs/project-brain/cards/") for card in public_cards)


def test_fixed_git_source_can_export_despite_secret_path_collision(tmp_path):
    from src.core.project_brain.artifacts import validate
    from src.core.project_brain.extractor import FactExtractor
    from src.core.project_brain.schema import Compilation
    from src.core.project_brain.snapshot import export_snapshot
    from src.core.project_brain.source_catalog import SourceCatalog
    from tests.unit.test_project_brain import git

    repository = tmp_path / "source"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    (repository / "rosecrab.py").write_text('import os\nPASSWORD = os.getenv("PASSWORD", "rosecrab")\ndef run():\n    return PASSWORD\n', encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "synthetic fixture")
    sha = git(repository, "rev-parse", "HEAD")
    snapshot = export_snapshot(repository, tmp_path / "snapshot", sha, 100)
    raw = FactExtractor(snapshot).extract()
    catalog = SourceCatalog(snapshot, raw, b"x" * 32, 480, "main")
    ledger = PrivacyLedger(tmp_path / "work")
    ledger.remember([{"value": "rosecrab", "type": "SECRET"}])
    privacy = LedgerPrivacyFilter(ledger)
    safe = catalog.safe_bundle(catalog.bundle(), privacy)
    first_refs = safe.facts[0].source_ref_ids
    compilation = Compilation(title="module", claims=[{"text": "Reads configuration with a hidden default.", "evidence_ids": first_refs}], unknowns=[])
    cards = assemble_cards(safe, [(module, compilation) for module in safe.modules], [compilation])
    coverage = validate(safe, cards, catalog.snapshot)
    manifest = BrainManifest(generator="project-brain/2.0.0", build_id=str(uuid4()), project_id=480, branch="main",
        source_commit=sha, brain_version=1, recipe_hash="f" * 64, source_fingerprint=raw.source_fingerprint,
        files={}, coverage=coverage, issues=[])
    public, projected_cards, public_manifest = project_public(safe, cards, manifest, "https://fixture.invalid/repository", privacy,
                                                            source_paths=catalog.paths)
    assert all(ref.path_redacted and ref.source_url is None for ref in public.source_refs)
    assert public.source_commit == sha
    assert [ref.blob_sha for ref in public.source_refs] == [ref.blob_sha for ref in raw.source_refs]
    destination = ArtifactStore(str(tmp_path / "artifacts")).finalize(public_manifest, public, projected_cards, "https://fixture.invalid/repository")
    with zipfile.ZipFile(destination / "knowledge.zip") as archive:
        assert all(b"rosecrab" not in archive.read(name) for name in archive.namelist())
    # Git source and canonical parser data remain unchanged, not sanitized caches.
    assert '"rosecrab"' in (repository / "rosecrab.py").read_text()
    assert raw.source_refs[0].path == "rosecrab.py"


def test_unlisted_private_staging_file_is_never_archived(projection, tmp_path, monkeypatch):
    _, _, _, _, _, (public, cards, manifest) = projection
    original_write = type(tmp_path).write_text
    injected = False

    def injecting_write(path, data, *args, **kwargs):
        nonlocal injected
        result = original_write(path, data, *args, **kwargs)
        if path.name == "cards.json" and not injected:
            injected = True
            original_write(path.parent / "private-checkpoint.json", "must not export", encoding="utf-8")
        return result

    monkeypatch.setattr(type(tmp_path), "write_text", injecting_write)
    store = ArtifactStore(str(tmp_path / "artifacts"))
    with pytest.raises(ValueError, match="非公开清单"):
        store.finalize(manifest, public, cards, "https://fixture.invalid/project")
    assert not store.directory(manifest.build_id).exists()
