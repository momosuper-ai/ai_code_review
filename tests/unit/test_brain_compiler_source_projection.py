"""Offline source/display separation; no gateway, trial source or DB access."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import settings
from src.core.privacy_filter import PrivacyFilter
from src.core.project_brain.compiler import (
    CompilationLimitError, ProjectedMessageFilter, compile_packet, compilation_transport, encoded,
    packets, project_compilation_texts, project_packet_texts, sanitize_structure,
)
from src.core.project_brain.context import compact_packet
from src.core.project_brain.schema import Compilation
from src.core.project_brain.source_bound import SourceBoundPlan
from tests.unit.test_brain_compiler_transport import model, transport  # noqa: F401
from tests.unit.test_brain_strict_transport import formatter_result


def fixture_projection(case):
    displays = {ref.path: ref.path for ref in case.bundle.source_refs}
    case.guard.packet_projection = lambda packet, privacy: project_packet_texts(packet, privacy, displays)
    return displays


def test_typed_packet_masks_names_and_excerpts_but_not_source_bindings(transport):
    packet = copy.deepcopy(transport.packet)
    original = copy.deepcopy(packet)
    displays = {ref.path: "private-name/file.py" for ref in transport.bundle.source_refs}
    privacy = transport.guard.filter
    privacy._dynamic_values.update({"private-name": "SECRET", packet["module_id"]: "SECRET",
                                    packet["items"][0]["evidence"][0]["blob_sha"]: "SECRET"})
    packet["items"][0]["fact"]["name"] = "private-name"
    packet["items"][0]["evidence"][0]["excerpt"] = "credential = 'private-name'"
    safe = project_packet_texts(packet, privacy, displays)
    assert safe["module_id"] == packet["module_id"]
    assert safe["items"][0]["fact"]["id"] == packet["items"][0]["fact"]["id"]
    for entry, clean in zip(packet["items"], safe["items"], strict=True):
        assert entry["relations"] == clean["relations"]
        for ref, clean_ref in zip(entry["evidence"], clean["evidence"], strict=True):
            assert {k: v for k, v in clean_ref.items() if k not in {"display_path", "excerpt"}} == {
                k: v for k, v in ref.items() if k not in {"display_path", "excerpt"}}
            assert "private-name" not in clean_ref["display_path"]
    assert "private-name" not in safe["items"][0]["fact"]["name"]
    assert "private-name" not in safe["items"][0]["evidence"][0]["excerpt"]
    assert original["items"][0]["fact"]["name"] != packet["items"][0]["fact"]["name"]


def test_redacted_equal_display_paths_keep_distinct_source_groups(transport):
    displays = {ref.path: "hidden.py" for ref in transport.bundle.source_refs}
    safe = project_packet_texts(transport.packet, transport.guard.filter, displays)
    wire = compact_packet(safe).wire
    assert len(wire["sources"]) == 2
    assert {entry["path"] for entry in wire["sources"].values()} == {"hidden.py"}
    assert len({entry["blob_sha"] for entry in wire["sources"].values()}) == 2


def test_unknown_property_redaction_collision_is_rejected_without_overwrite(transport):
    privacy = transport.guard.filter
    privacy._dynamic_values.update({"credential-one": "SECRET", "credential-two": "SECRET"})
    # Dynamic values usually get distinct placeholders. A filter can also
    # deliberately map two values to one marker; never assume uniqueness.
    class Collision:
        def sanitize_text(self, value):
            return "hidden" if value in {"credential-one", "credential-two"} else value
    with pytest.raises(ValueError, match="隐私投影字段名冲突"):
        sanitize_structure({"credential-one": 1, "credential-two": 2}, Collision())
    packet = copy.deepcopy(transport.packet)
    packet["items"][0]["fact"]["attributes"].update({"credential-one": 1, "credential-two": 2})
    with pytest.raises(ValueError, match="隐私投影字段名冲突"):
        project_packet_texts(packet, Collision(), {ref.path: ref.path for ref in transport.bundle.source_refs})


@pytest.mark.parametrize("value", [{"opaque-looking-secret": 1}, {"evidence:" + "a" * 64: "credential-value"}, {"evidence:" + "a" * 64: True}])
def test_reserved_call_metadata_cannot_hide_source_values(transport, value):
    packet = copy.deepcopy(transport.packet)
    packet["items"][0]["fact"]["attributes"]["call_arities"] = value
    with pytest.raises(ValueError, match="调用元数据无效"):
        project_packet_texts(packet, transport.guard.filter, {ref.path: ref.path for ref in transport.bundle.source_refs})


@pytest.mark.parametrize("value,literal", [(12345678, "12345678"), (True, "true"), (False, "false"), (12.75, "12.75")])
def test_unknown_scalar_lexical_secret_masks_without_rewriting_machine_counts(transport, value, literal):
    packet = copy.deepcopy(transport.packet)
    attrs = packet["items"][0]["fact"]["attributes"]
    attrs["custom_scalar"] = {"credential": value}
    attrs["parameter_count"] = value if type(value) in {int, bool} else 4
    transport.guard.filter._dynamic_values[literal] = "SECRET"
    safe = project_packet_texts(packet, transport.guard.filter, {ref.path: ref.path for ref in transport.bundle.source_refs})
    projected = safe["items"][0]["fact"]["attributes"]
    assert isinstance(projected["custom_scalar"]["credential"], str)
    assert literal not in projected["custom_scalar"]["credential"]
    assert projected["parameter_count"] == attrs["parameter_count"]


def test_leaf_plan_canonical_digest_independent_of_redacted_text(transport):
    packet = copy.deepcopy(transport.packet)
    canonical = hashlib.sha256(encoded(packet).encode()).hexdigest()
    displays = fixture_projection(transport)
    transport.guard.filter._dynamic_values[packet["items"][0]["fact"]["name"]] = "SECRET"
    safe = project_packet_texts(packet, transport.guard.filter, displays)
    plan = SourceBoundPlan.from_packet(safe, compact_packet(safe), canonical_digest=canonical)
    assert {scope.packet_digest for scope in plan.scopes.values()} == {canonical}
    with pytest.raises(ValueError, match="规范摘要无效"):
        SourceBoundPlan.from_packet(safe, compact_packet(safe), canonical_digest="not-a-digest")


def test_compilation_projection_retains_all_evidence_scope_and_unknowns(transport):
    plan = SourceBoundPlan.from_packet(transport.packet, compact_packet(transport.packet))
    first = next(iter(plan.scopes.values()))
    compilation = Compilation(title="credential-name", claims=[{"text": "credential-name behavior",
        "evidence_ids": first.source_ref_ids, "source_scope": first}], unknowns=["credential-name unknown"],
        document_path="docs/project-brain/credential-name.md")
    transport.guard.filter._dynamic_values.update({"credential-name": "SECRET", first.id: "SECRET",
                                                  first.packet_digest: "SECRET"})
    safe = project_compilation_texts(compilation.model_dump(), transport.guard.filter)
    assert safe["claims"][0]["source_scope"] == first.model_dump()
    assert safe["claims"][0]["evidence_ids"] == compilation.claims[0].evidence_ids
    assert len(safe["claims"]) == 1 and len(safe["unknowns"]) == 1
    assert "credential-name" not in safe["title"] + safe["claims"][0]["text"] + safe["unknowns"][0]
    assert safe["document_path"] is None


def test_transport_refreshes_late_findings_without_corrupting_hashes(transport, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    fixture_projection(transport)
    packet = transport.packet
    digest = hashlib.sha256(encoded(packet).encode()).hexdigest()
    safe = transport.guard.project_packet(packet)
    messages, _, _ = compilation_transport(safe, compact_packet(safe), canonical_digest=digest)
    name = packet["items"][0]["fact"]["name"]
    transport.guard.filter._dynamic_values[name] = "SECRET"
    blob = packet["items"][0]["evidence"][0]["blob_sha"]
    transport.guard.filter._dynamic_values[blob] = "SECRET"
    sender = ProjectedMessageFilter(transport.guard, packet, digest, None)
    final = sender.sanitize_messages(messages)
    assert name not in final[1]["content"]
    assert blob in final[1]["content"]
    assert messages != final


def test_expanded_projection_after_reservation_cannot_exceed_budget(transport, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    fixture_projection(transport)
    packet = transport.packet
    digest = hashlib.sha256(encoded(packet).encode()).hexdigest()
    safe = transport.guard.project_packet(packet)
    messages, kwargs, _ = compilation_transport(safe, compact_packet(safe), canonical_digest=digest)
    sender = ProjectedMessageFilter(transport.guard, packet, digest, None)
    sender.bind_reservation(1, 4096, kwargs)
    with pytest.raises(CompilationLimitError, match="超出已预留"):
        sender.sanitize_messages(messages)


def test_retry_wrapper_unknown_scalars_are_masked_by_sender(transport, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    fixture_projection(transport)
    digest = hashlib.sha256(encoded(transport.packet).encode()).hexdigest()
    safe = transport.guard.project_packet(transport.packet)
    messages, _, _ = compilation_transport(safe, compact_packet(safe), canonical_digest=digest)
    messages.append({"role": "assistant", "content": "{}", "reasoning": {"credential": 12345678, "flag": True}})
    transport.guard.filter._dynamic_values.update({"12345678": "SECRET", "true": "SECRET"})
    result = ProjectedMessageFilter(transport.guard, transport.packet, digest, None).sanitize_messages(messages)
    assert isinstance(result[-1]["reasoning"]["credential"], str)
    assert "12345678" not in result[-1]["reasoning"]["credential"]
    assert isinstance(result[-1]["reasoning"]["flag"], str)
    assert "true" not in result[-1]["reasoning"]["flag"]


@pytest.mark.asyncio
async def test_required_structured_reasoning_is_classified_as_complete_untrusted_data(transport, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    fixture_projection(transport)
    scans = []
    clean = transport.guard.clean
    async def record_clean(text):
        payload = json.loads(text)
        scans.append(payload)
        if "model_retry_reasoning.credential" in payload:
            transport.guard.filter._dynamic_values.update({"12345678": "SECRET", "true": "SECRET"})
        return await clean(text)
    monkeypatch.setattr(transport.guard, "clean", record_clean)
    plan = SourceBoundPlan.from_packet(transport.packet, compact_packet(transport.packet))
    good = {"title": "Safe responsibilities", "slots": {slot: {"texts": ["Complete duties"], "unknowns": []}
            for slot in plan.scopes}, "unknowns": [], "document_path": None}
    first = formatter_result("{invalid")
    first.message.model_extra["reasoning"] = {"credential": 12345678, "flag": True}
    mock = model(monkeypatch, [first, formatter_result(encoded(good))])
    await compile_packet(transport.packet, transport.guard)
    assert len(mock.calls) == 2
    scan = next(payload for payload in scans if "model_retry_reasoning.credential" in payload)
    assert scan["model_retry_reasoning.credential"] == ["12345678"]
    assert scan["model_retry_reasoning.flag"] == ["true"]
    assert {"credential", "flag"} <= set(scan["field_name"])
    returned = mock.calls[1][2]["reasoning"]
    assert isinstance(returned["credential"], str) and "12345678" not in returned["credential"]
    assert isinstance(returned["flag"], str) and "true" not in returned["flag"]


@pytest.mark.asyncio
async def test_complete_mocked_compilation_keeps_canonical_leaf_scope(transport, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    fixture_projection(transport)
    packet = transport.packet
    digest = hashlib.sha256(encoded(packet).encode()).hexdigest()
    plan = SourceBoundPlan.from_packet(packet, compact_packet(packet), canonical_digest=digest)
    transport.guard.filter._dynamic_values[packet["items"][0]["fact"]["name"]] = "SECRET"
    output = {"title": "Safe responsibilities", "slots": {slot: {"texts": ["Complete duties"], "unknowns": []}
              for slot in plan.scopes}, "unknowns": [], "document_path": None}
    mock = model(monkeypatch, [formatter_result(encoded(output))])
    result = await compile_packet(packet, transport.guard)
    assert len(mock.calls) == 1
    assert all(claim.source_scope.packet_digest == digest for claim in result.claims)
    assert {rid for claim in result.claims for rid in claim.evidence_ids} == {
        rid for scope in plan.scopes.values() for rid in scope.source_ref_ids}


def test_packets_use_snapshot_resolver_instead_of_display_paths(transport):
    mapping = {ref.path: Path(transport.snapshot.path) / ref.path for ref in transport.bundle.source_refs}
    calls = []
    def resolve(path):
        calls.append(path)
        return mapping[path]
    snapshot = SimpleNamespace(path="intentionally-not-a-source-directory", resolve_source=resolve)
    result = packets(transport.bundle, snapshot)
    assert result and set(calls) == set(mapping)
