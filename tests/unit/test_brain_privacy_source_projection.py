"""Every display field is classified; authenticated opaque syntax is metadata."""

import copy

import pytest

from src.core.project_brain.privacy_projection import bundle_privacy_records, compilation_privacy_records
from src.core.project_brain.schema import Compilation, FactBundle
from tests.unit.test_brain_privacy_projection import bundle  # noqa: F401


@pytest.fixture
def opaque_bundle(bundle):
    old = bundle.model_dump()
    path = next(iter(bundle.files))
    locator = "sources/s" + "1" * 64
    replacement = {path: locator, bundle.modules[bundle.facts[0].module_id]: "sources"}
    ids = [bundle.facts[0].id, bundle.facts[0].module_id, bundle.relations[0].id, bundle.source_refs[0].id]
    for index, identifier in enumerate(ids, 2):
        replacement[identifier] = identifier.split(":", 1)[0] + ":" + str(index) * 64
    def mapped(value):
        if isinstance(value, str):
            return replacement.get(value, value)
        if isinstance(value, list):
            return [mapped(item) for item in value]
        if isinstance(value, dict):
            return {replacement.get(key, key): mapped(item) for key, item in value.items()}
        return value
    value = mapped(old)
    value["display_paths"] = {locator: "app/synthetic-sensitive-file.py", "sources": "app"}
    value["source_refs"][0]["display_path"] = "app/synthetic-sensitive-file.py"
    value["observations"][locator]["refs"] = copy.deepcopy(value["source_refs"])
    value["source_identity"] = {"version": "opaque-v1", "key_version": 1, "project_id": 480,
        "canonical_binding": "a" * 64, "branch_id": "branch:" + "b" * 64}
    return FactBundle.model_validate(value)


def test_opaque_machine_metadata_and_all_display_paths_have_separate_roles(opaque_bundle):
    records = bundle_privacy_records(opaque_bundle)
    texts = {item["text"] for item in records}
    assert "app/synthetic-sensitive-file.py" in texts
    assert "app" in texts
    assert next(iter(opaque_bundle.files)) not in texts
    assert opaque_bundle.facts[0].id not in texts
    assert opaque_bundle.source_refs[0].id not in texts
    assert opaque_bundle.source_identity["canonical_binding"] not in texts
    assert opaque_bundle.source_identity["branch_id"] not in texts


def test_unknown_display_path_and_names_never_become_metadata(opaque_bundle):
    opaque_bundle.display_paths["sources/s" + "c" * 64] = "synthetic-private-unknown-path"
    opaque_bundle.facts[0].attributes["description"] = opaque_bundle.facts[0].id
    opaque_bundle.observations[next(iter(opaque_bundle.observations))]["facts"] = [opaque_bundle.facts[0].model_dump()]
    texts = {item["text"] for item in bundle_privacy_records(opaque_bundle)}
    assert "synthetic-private-unknown-path" in texts
    assert opaque_bundle.facts[0].id in texts


@pytest.mark.parametrize("field,value", [
    ("version", "unknown"), ("key_version", True), ("project_id", False),
    ("canonical_binding", "not-a-hash"), ("branch_id", "branch:not-a-hash"),
    ("unknown-field", "opaque-looking-source-value"),
])
def test_unrecognized_identity_wrapper_cannot_exempt_fields(opaque_bundle, field, value):
    opaque_bundle.source_identity[field] = value
    with pytest.raises(ValueError, match="隐私投影来源身份元数据无效"):
        bundle_privacy_records(opaque_bundle)


def test_display_ref_mirror_tampering_is_not_silently_ignored(opaque_bundle):
    opaque_bundle.observations[next(iter(opaque_bundle.observations))]["refs"][0]["display_path"] = "changed-source-name"
    with pytest.raises(ValueError, match="观察副本与规范事实不一致"):
        bundle_privacy_records(opaque_bundle)


def test_64_hex_evidence_is_metadata_only_in_claim_reference_position():
    evidence = "evidence:" + "d" * 64
    compilation = Compilation(title="Complete", claims=[{"text": evidence, "evidence_ids": [evidence]}], unknowns=[])
    records = compilation_privacy_records(compilation)
    assert {"field": "claims.text", "text": evidence} in records
    assert {"field": "evidence_reference", "text": evidence} not in records


def test_unknown_numeric_and_boolean_source_values_are_complete_records(bundle):
    bundle.facts[0].attributes.update(custom_numeric=12345678, custom_boolean=True, parameter_count=12345678)
    bundle.observations["app/service.py"]["facts"] = [bundle.facts[0].model_dump()]
    records = bundle_privacy_records(bundle)
    assert {"field": "attributes.custom_numeric", "text": "12345678"} in records
    assert {"field": "attributes.custom_boolean", "text": "true"} in records
    assert {"field": "attributes.parameter_count", "text": "12345678"} not in records
