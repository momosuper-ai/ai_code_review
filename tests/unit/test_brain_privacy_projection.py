"""Privacy projections retain every free field and reuse equal parser roles."""

import json

import pytest

from src.core.project_brain.privacy_projection import (
    bundle_privacy_records,
    card_privacy_records,
    compilation_privacy_records,
    project_records,
)
from src.core.project_brain.schema import (
    Compilation,
    Claim,
    Fact,
    FactBundle,
    InferredClaim,
    InferenceScope,
    KnowledgeCard,
    Relation,
    SourceRef,
    stable_id,
)


@pytest.fixture
def bundle() -> FactBundle:
    """Build one genuine-shaped declaration and its observation mirror."""
    path, name, signature = "app/service.py", "lookup", "lookup(session)"
    fid = stable_id("symbol", path, name, "function", signature)
    mid = stable_id("module", "app")
    eid = stable_id("evidence", path, "1", "2", fid)
    fact = Fact(id=fid, kind="symbol", name=name, module_id=mid,
                source_ref_ids=[eid], extractor="brain.python/1", attributes={
                    "path": path, "language": "python", "symbol_kind": "function",
                    "signature": signature, "assigned_names": ["lookup", "session"],
                    "shadowed_names": ["lookup", "session"],
                })
    relation = Relation(id=stable_id("relation", fid, "CALLS", name), from_id=fid,
                        type="CALLS", to_id=None, target_name=name, resolution="unresolved",
                        source_ref_ids=[eid])
    ref = SourceRef(id=eid, commit_sha="a" * 40, path=path, blob_sha="b" * 40,
                    line_start=1, line_end=2, symbol_id=fid)
    observation = {
        "module": mid, "facts": [fact.model_dump()], "refs": [ref.model_dump()],
        "relations": [relation.model_dump()], "imports": {"lookup": "lookup"},
        "globals": ["lookup"], "java_imports": {}, "routers": {}, "mounts": [],
        "apps": [], "calls": [[path, fid, name, eid]], "accesses": [],
        "routes": [], "issues": [], "frameworks": [],
    }
    return FactBundle(source_commit="a" * 40, files={path: {
        "blob_sha": "b" * 40, "mode": "100644", "lines": 2, "size": 36,
        "available": True,
    }}, modules={mid: "app"}, facts=[fact], relations=[relation], source_refs=[ref],
        source_fingerprint="c" * 64, observations={path: observation})


def test_equal_parser_identifiers_have_one_complete_record(bundle: FactBundle) -> None:
    records = bundle_privacy_records(bundle)
    assert [r for r in records if r["text"] == "lookup"] == [
        {"field": "code_identifier", "text": "lookup"},
    ]
    assert [r for r in records if r["text"] == "session"] == [
        {"field": "code_identifier", "text": "session"},
    ]
    assert {"field": "source_signature", "text": "lookup(session)"} in records


def test_credentials_and_descriptions_never_share_identifier_receipt(bundle: FactBundle) -> None:
    bundle.facts[0].attributes.update(credential="lookup", description="lookup")
    bundle.observations["app/service.py"]["facts"] = [bundle.facts[0].model_dump()]
    fields = {r["field"] for r in bundle_privacy_records(bundle) if r["text"] == "lookup"}
    assert fields == {"code_identifier", "attributes.credential", "attributes.description"}


def test_signature_literals_remain_intact(bundle: FactBundle) -> None:
    signature = 'lookup(password="synthetic secret crossing no chunks")'
    bundle.facts[0].attributes["signature"] = signature
    bundle.observations["app/service.py"]["facts"] = [bundle.facts[0].model_dump()]
    assert {"field": "source_signature", "text": signature} in bundle_privacy_records(bundle)


def test_non_identifier_in_identifier_slot_is_not_reclassified(bundle: FactBundle) -> None:
    value = 'password="synthetic full credential"'
    bundle.facts[0].attributes["assigned_names"] = [value]
    bundle.observations["app/service.py"]["facts"] = [bundle.facts[0].model_dump()]
    records = bundle_privacy_records(bundle)
    assert {"field": "attributes.assigned_names", "text": value} in records
    assert {"field": "code_identifier", "text": value} not in records


def test_machine_metadata_removed_only_in_controlled_positions(bundle: FactBundle) -> None:
    fid = bundle.facts[0].id
    bundle.facts[0].attributes["description"] = fid
    bundle.facts[0].attributes["credential"] = "python"
    bundle.observations["app/service.py"]["facts"] = [bundle.facts[0].model_dump()]
    records = bundle_privacy_records(bundle)
    assert {"field": "attributes.description", "text": fid} in records
    assert {"field": "attributes.credential", "text": "python"} in records
    assert not any(r["text"] == "a" * 40 for r in records)
    assert not any(r["text"] == "b" * 40 for r in records)
    assert not any(r["text"] == "function" for r in records)


def test_unknown_enum_is_new_free_text(bundle: FactBundle) -> None:
    bundle.facts[0].attributes["language"] = "synthetic credential"
    bundle.observations["app/service.py"]["facts"] = [bundle.facts[0].model_dump()]
    assert {"field": "attributes.language", "text": "synthetic credential"} in bundle_privacy_records(bundle)


def test_all_paths_and_module_names_are_classified(bundle: FactBundle) -> None:
    bundle.files["binary/synthetic-sensitive-name.bin"] = {
        "blob_sha": "d" * 40, "mode": "100644", "size": 0, "lines": 0, "available": False,
    }
    bundle.modules[stable_id("module", "synthetic-sensitive-module")] = "synthetic-sensitive-module"
    records = bundle_privacy_records(bundle)
    assert {"field": "source_path", "text": "app/service.py"} in records
    assert {"field": "source_path", "text": "binary/synthetic-sensitive-name.bin"} in records
    assert {"field": "source_path", "text": "synthetic-sensitive-module"} in records


def test_unknown_field_keys_and_values_are_classified(bundle: FactBundle) -> None:
    bundle.facts[0].attributes["synthetic-secret-key"] = {"credential": "synthetic-secret-value"}
    bundle.observations["app/service.py"]["facts"] = [bundle.facts[0].model_dump()]
    records = bundle_privacy_records(bundle)
    assert {"field": "field_name", "text": "synthetic-secret-key"} in records
    assert {"field": "field_name", "text": "credential"} in records
    assert any(r["text"] == "synthetic-secret-value" for r in records)


def test_parser_role_is_bound_to_fact_kind(bundle: FactBundle) -> None:
    bundle.facts[0].attributes["namespace"] = "lookup"
    bundle.observations["app/service.py"]["facts"] = [bundle.facts[0].model_dump()]
    assert {"field": "attributes.namespace", "text": "lookup"} in bundle_privacy_records(bundle)


@pytest.mark.parametrize("collection", ["facts", "refs", "relations"])
def test_observation_cannot_claim_modified_free_text_is_canonical(bundle: FactBundle, collection: str) -> None:
    key = {"facts": "name", "refs": "path", "relations": "target_name"}[collection]
    bundle.observations["app/service.py"][collection][0][key] = "synthetic-hidden-secret"
    with pytest.raises(ValueError, match="副本"):
        bundle_privacy_records(bundle)


def test_unknown_observation_payload_does_not_become_machine_metadata(bundle: FactBundle) -> None:
    bundle.observations["app/service.py"]["new_extractor_data"] = {
        "credential": "synthetic credential", "trusted": True,
    }
    records = bundle_privacy_records(bundle)
    assert {"field": "field_name", "text": "new_extractor_data"} in records
    assert any(r["field"].endswith("credential") and r["text"] == "synthetic credential" for r in records)


def test_issues_cannot_reuse_identifier_context(bundle: FactBundle) -> None:
    bundle.issues = ["lookup"]
    assert {"field": "source_issue", "text": "lookup"} in bundle_privacy_records(bundle)


def test_compilation_scans_narrative_and_path_but_not_generated_references() -> None:
    eid = stable_id("evidence", "sample", "1", "2", "")
    value = Compilation(title="synthetic title", claims=[InferredClaim(
        text="synthetic credential narrative", evidence_ids=[eid],
    )], unknowns=["synthetic unknown"], document_path="docs/project-brain/synthetic-name.md")
    records = compilation_privacy_records(value)
    assert {"field": "title", "text": value.title} in records
    assert {"field": "claims.text", "text": value.claims[0].text} in records
    assert {"field": "unknowns", "text": value.unknowns[0]} in records
    assert {"field": "document_path", "text": value.document_path} in records
    assert not any(r["text"] == eid for r in records)


def test_records_projection_keeps_roles_stable_and_deduplicates() -> None:
    records = [{"field": "credential", "text": "value"},
               {"field": "description", "text": "value"},
               {"field": "credential", "text": "value"}]
    projected = project_records(records)
    assert projected == {"credential": ["value"], "description": ["value"]}
    assert json.loads(json.dumps(projected)) == projected


def test_future_compilation_schema_fields_default_to_complete_scanning() -> None:
    class FutureCompilation(Compilation):
        credential: str
        details: dict[str, str]

    value = FutureCompilation(title="title", claims=[InferredClaim(
        text="explanation", evidence_ids=[stable_id("evidence", "sample")],
    )], unknowns=[], credential="synthetic new credential",
        details={"synthetic secret field": "synthetic full future value"})
    records = compilation_privacy_records(value)
    assert {"field": "field_name", "text": "credential"} in records
    assert {"field": "credential", "text": "synthetic new credential"} in records
    assert {"field": "field_name", "text": "synthetic secret field"} in records
    assert any(r["text"] == "synthetic full future value" for r in records)


def test_future_claim_schema_fields_and_hash_shaped_free_text_are_scanned() -> None:
    class FutureClaim(InferredClaim):
        explanation: str
        details: dict[str, str]

    hash_shaped_value = stable_id("evidence", "synthetic free literal")
    claim = FutureClaim(text="explanation", evidence_ids=[stable_id("evidence", "sample")],
                        explanation=hash_shaped_value,
                        details={"credential": "synthetic complete nested credential"})
    value = Compilation(title="title", claims=[claim], unknowns=[])
    records = compilation_privacy_records(value)
    assert {"field": "field_name", "text": "explanation"} in records
    assert {"field": "claims.explanation", "text": hash_shaped_value} in records
    assert {"field": "field_name", "text": "credential"} in records
    assert any(r["text"] == "synthetic complete nested credential" for r in records)


def test_only_evidence_ids_in_known_claim_field_can_skip_classification() -> None:
    non_evidence_id = stable_id("symbol", "synthetic wrong-kind reference")
    value = Compilation(title="title", claims=[InferredClaim(
        text="explanation", evidence_ids=[non_evidence_id, "synthetic free reference"],
    )], unknowns=[])
    records = compilation_privacy_records(value)
    assert {"field": "evidence_reference", "text": non_evidence_id} in records
    assert {"field": "evidence_reference", "text": "synthetic free reference"} in records


def test_card_narratives_reuse_compilation_roles_and_future_claim_fields_scan() -> None:
    class FutureClaim(Claim):
        credential: str

    card = KnowledgeCard(id=stable_id("module", "app"), kind="module", title="title",
                         module_id=stable_id("module", "app"), source_commit="a" * 40,
                         claims=[FutureClaim(text="explanation", knowledge_type="AI_INFERRED",
                            evidence_ids=[stable_id("evidence", "sample")], credential="synthetic new value")],
                         unknowns=["unknown"], document_path="docs/project-brain/module.md")
    records = card_privacy_records(card)
    assert {"field": "claims.text", "text": "explanation"} in records
    assert {"field": "title", "text": "title"} in records
    assert {"field": "unknowns", "text": "unknown"} in records
    assert {"field": "field_name", "text": "credential"} in records
    assert {"field": "claims.credential", "text": "synthetic new value"} in records


@pytest.mark.parametrize("view", [None, "symbol_graph", "resolution_counts"])
def test_service_relationship_view_is_metadata_but_prose_keeps_scanning(view):
    ref = stable_id("evidence", "sample")
    scope = InferenceScope(id=stable_id("scope", "sample"), kind="summary_synthesis",
        packet_digest="a" * 64, fact_id=None, relation_ids=[], fragment_index=0, fragment_total=1,
        source_ref_ids=[ref], module_ids=[stable_id("module", "sample")],
        child_ids=[stable_id("summary-input", "sample")], relationship_view=view)
    value = Compilation(title="title", claims=[InferredClaim(text="resolution_counts", evidence_ids=[ref],
                        source_scope=scope)], unknowns=[])
    records = compilation_privacy_records(value)
    assert {"field": "claims.text", "text": "resolution_counts"} in records
    assert not any("relationship_view" in record["field"] or record["text"] == "relationship_view" for record in records)
    value.claims[0].source_scope = scope.model_copy(update={"relationship_view": {"synthetic credential": "must scan"}})
    records = compilation_privacy_records(value)
    assert {"field": "field_name", "text": "relationship_view"} in records
    assert any(record["text"] == "must scan" for record in records)
