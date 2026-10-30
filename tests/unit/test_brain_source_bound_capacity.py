"""Fixed explanation capacity splits inputs without dropping canonical facts."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletionMessage
from pydantic import ValidationError

from src.config import Settings, settings
from src.core.project_brain.compiler import CompilationLimitError, compile_packet, encoded, packets, packet_wire, token_bound
from src.core.project_brain.context import compact_packet, read_rows
from src.core.project_brain.extractor import FactExtractor
from src.core.project_brain.lifecycle import recipe_hash
from src.core.project_brain.schema import Compilation, Relation, stable_id
from src.core.project_brain.source_bound import SourceBoundPlan
from src.core.project_brain.summary import next_summary_tasks
from src.core.project_brain.snapshot import export_snapshot
from src.models.brain import BrainUnit
from tests.unit.test_brain_compiler_transport import model, output, transport
from tests.unit.test_brain_source_bound import bound_output
from tests.unit.test_brain_strict_transport import formatter_result
from tests.unit.test_project_brain import git


@pytest.mark.parametrize("limit", [0, 65])
def test_slot_capacity_configuration_rejects_out_of_range(limit: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, PROJECT_BRAIN_MAX_EXPLANATION_SLOTS=limit)


def test_slot_capacity_has_conservative_default_and_invalidates_recipe(monkeypatch) -> None:
    assert Settings(_env_file=None).PROJECT_BRAIN_MAX_EXPLANATION_SLOTS == 24
    original = recipe_hash()
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_EXPLANATION_SLOTS", 23)
    assert recipe_hash() != original


@pytest.mark.parametrize("limit,expected_packets", [(1, 2), (2, 1)])
def test_exact_slot_boundary_splits_whole_source_items(transport, monkeypatch, limit, expected_packets) -> None:
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_EXPLANATION_SLOTS", limit)
    results = packets(transport.bundle, transport.snapshot)
    assert len(results) == expected_packets
    assert all(0 < len(packet["items"]) <= limit for packet in results)
    assert {item["fact"]["id"] for packet in results for item in packet["items"]} == {
        fact.id for fact in transport.bundle.facts}
    assert {relation["id"] for packet in results for item in packet["items"] for relation in item["relations"]} == {
        relation.id for relation in transport.bundle.relations}
    assert {ref["id"] for packet in results for item in packet["items"] for ref in item["evidence"]} == {
        ref.id for ref in transport.bundle.source_refs}


@pytest.mark.asyncio
async def test_direct_compile_accepts_exact_capacity(transport, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_EXPLANATION_SLOTS", len(transport.packet["items"]))
    mock = model(monkeypatch, [formatter_result(encoded(bound_output(transport.packet)))])
    result = await compile_packet(transport.packet, transport.guard)
    assert len(result.claims) == len(transport.packet["items"])
    assert len(mock.calls) == 1


@pytest.mark.asyncio
async def test_direct_compile_capacity_plus_one_stops_before_privacy_or_model(transport, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_EXPLANATION_SLOTS", len(transport.packet["items"]) - 1)
    clean = AsyncMock(side_effect=AssertionError("oversized packets must not enter scanning"))
    monkeypatch.setattr(transport.guard, "clean", clean)
    mock = model(monkeypatch, [])
    with pytest.raises(CompilationLimitError, match="槽位上限"):
        await compile_packet(transport.packet, transport.guard)
    clean.assert_not_called()
    assert not mock.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "strict_tool"])
async def test_other_protocols_ignore_slot_capacity(transport, monkeypatch, mode) -> None:
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", mode)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_EXPLANATION_SLOTS", 1)
    results = packets(transport.bundle, transport.snapshot)
    assert len(results) == 1 and len(results[0]["items"]) == 2
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    response = formatter_result(output(alias)) if mode == "strict_tool" else ChatCompletionMessage(
        role="assistant", content=output(alias))
    mock = model(monkeypatch, [response])
    result = await compile_packet(transport.packet, transport.guard)
    assert result.claims and len(mock.calls) == 1


def test_sixty_declarations_preserve_all_calls_and_evidence_under_slot_capacity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_EXPLANATION_SLOTS", 24)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 100000)
    monkeypatch.setattr(settings, "LLM_MAX_CONTEXT_TOKENS", 200000)
    repository = tmp_path / "synthetic"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    lines = []
    for index in range(60):
        lines.append(f"def E9{index:03d}(value):")
        if index == 0:
            lines.append("    return value")
        elif index == 1:
            lines.append("    return E9000(value)")
        else:
            lines.append(f"    return E9{index - 1:03d}(value) + E9{index - 2:03d}(value)")
        lines.append("")
    (repository / "pipeline.py").write_text("\n".join(lines), encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "synthetic capacity fixture")
    snapshot = export_snapshot(repository, tmp_path / "snapshot", git(repository, "rev-parse", "HEAD"), 10)
    bundle = FactExtractor(snapshot).extract()
    contexts = packets(bundle, snapshot)
    assert len(contexts) == 3
    assert sorted(len(packet["items"]) for packet in contexts) == [13, 24, 24]
    assert len([fact for fact in bundle.facts if fact.kind == "symbol"]) == 60
    assert len([relation for relation in bundle.relations if relation.type == "CALLS"]) == 117
    assert {item["fact"]["id"] for packet in contexts for item in packet["items"]} == {
        fact.id for fact in bundle.facts}
    assert {relation["id"] for packet in contexts for item in packet["items"] for relation in item["relations"]} == {
        relation.id for relation in bundle.relations}
    assert {ref["id"] for packet in contexts for item in packet["items"] for ref in item["evidence"]} == {
        ref.id for ref in bundle.source_refs}


def test_relation_fragments_keep_all_candidates_with_one_slot_per_packet(transport, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_EXPLANATION_SLOTS", 1)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 4096)
    caller, callee = transport.bundle.facts
    transport.bundle.relations = [Relation(id=stable_id("relation", "capacity", str(index)),
        from_id=caller.id, type="CALLS", to_id=None, target_name=f"candidate_{index}",
        resolution="candidate", source_ref_ids=caller.source_ref_ids,
        candidates=[callee.id]) for index in range(300)]
    contexts = packets(transport.bundle, transport.snapshot)
    found = []
    fragments = []
    for packet in contexts:
        assert len(packet["items"]) == 1
        assert token_bound(encoded(packet_wire(packet))) <= 4096
        context = compact_packet(packet)
        scope = next(iter(SourceBoundPlan.from_packet(packet, context).scopes.values()))
        item = packet["items"][0]
        assert set(scope.relation_ids) == {relation["id"] for relation in item["relations"]}
        found.extend(relation["id"] for relation in item["relations"])
        if scope.fact_id == caller.id:
            fragments.append((scope.fragment_index, scope.fragment_total))
        for relation in read_rows(context.wire, "relations"):
            assert relation["resolution"] == "candidate" and relation["to_id"] is None
            assert len(relation["candidates"]) == 1
    assert len(found) == len(set(found)) == 300
    assert set(found) == {relation.id for relation in transport.bundle.relations}
    assert len(fragments) > 1
    assert sorted(fragments) == [(index, len(fragments)) for index in range(len(fragments))]


@pytest.mark.parametrize("mode", ["source_bound", "auto", "strict_tool"])
def test_summary_capacity_retains_every_group_unknown_and_source(transport, monkeypatch, mode) -> None:
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", mode)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_EXPLANATION_SLOTS", 2)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    monkeypatch.setattr("src.core.project_brain.summary.MAX_SUMMARY_CHILDREN", 2)
    bundle = transport.bundle
    fact = bundle.facts[0]
    bundle = bundle.model_copy(deep=True, update={"modules": {fact.module_id: bundle.modules[fact.module_id]},
                                                "facts": [fact], "relations": []})
    units = []
    for index in range(5):
        compiled = Compilation(title=f"模块{index}",
            claims=[{"text": f"完整结论{index}", "evidence_ids": fact.source_ref_ids}],
            unknowns=[f"完整未知项{index}"])
        units.append(BrainUnit(key=f"compile:{index}", kind="compile", status="completed",
                               payload={"module_id": fact.module_id}, result=compiled.model_dump()))
    written = next_summary_tasks(bundle, units)
    assert len(written) == 3
    assert all(len(unit.payload["items"]) <= 2 for unit in written)
    assert all(len(SourceBoundPlan.from_packet(unit.payload, compact_packet(unit.payload)).scopes) == 1
               for unit in written)
    items = [item for unit in written for item in unit.payload["items"]]
    assert [item["fact"]["claims"][0]["text"] for item in items] == [f"完整结论{i}" for i in range(5)]
    assert [item["fact"]["unknowns"] for item in items] == [[f"完整未知项{i}"] for i in range(5)]
    assert all({ref["id"] for ref in item["evidence"]} == set(fact.source_ref_ids) for item in items)


def test_summary_byte_fragments_preserve_every_complete_unknown(transport, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_EXPLANATION_SLOTS", 1)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 6000)
    bundle = transport.bundle
    fact = bundle.facts[0]
    bundle = bundle.model_copy(deep=True, update={"modules": {fact.module_id: bundle.modules[fact.module_id]},
                                                "facts": [fact], "relations": []})
    compiled = Compilation(title="完整模块",
        claims=[{"text": f"完整结论{i}：" + "说明" * 120, "evidence_ids": fact.source_ref_ids} for i in range(5)],
        unknowns=[f"完整未知项{i}：" + "限制" * 100 for i in range(5)])
    unit = BrainUnit(key="compile:0", kind="compile", status="completed",
                     payload={"module_id": fact.module_id}, result=compiled.model_dump())
    written = next_summary_tasks(bundle, [unit])
    assert len(written) > 1
    found_claims, found_unknowns = [], []
    for summary in written:
        assert token_bound(encoded(packet_wire(summary.payload))) <= 6000
        for item in summary.payload["items"]:
            found_claims.extend(item["fact"]["claims"])
            found_unknowns.extend(item["fact"]["unknowns"])
            assert {ref["id"] for ref in item["evidence"]} == set(fact.source_ref_ids)
    assert found_claims == [claim.model_dump() for claim in compiled.claims]
    assert found_unknowns == compiled.unknowns
