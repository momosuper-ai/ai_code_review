"""Late privacy discoveries must not invalidate paid sibling checkpoints."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.project_brain.artifacts import ArtifactStore, assemble_cards
from src.core.project_brain.compiler import compile_packet, encoded, sanitize_structure
from src.core.project_brain.context import compact_packet
from src.core.project_brain.extractor import FactExtractor
from src.core.project_brain.lifecycle import recipe_hash
from src.core.project_brain.privacy import LedgerPrivacyFilter, PrivacyLedger
from src.core.project_brain.schema import Compilation
from src.core.project_brain.source_bound import SourceBoundPlan
from src.core.project_brain.summary import check_summary_task, hierarchy_for_assembly, next_summary_tasks, wrap_summary_result
from src.core.project_brain.trusted_packet import checkpoint_digest
from src.tasks.brain_task import _execute
from tests.unit.test_brain_hierarchy_planner import ready
from tests.unit.test_brain_compiler_transport import model, transport  # noqa: F401
from tests.unit.test_project_brain import repository, snapshot  # noqa: F401


@pytest.fixture
def late_privacy_case(snapshot, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_BASE_DIR", str(tmp_path / "brain"))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle = FactExtractor(snapshot).extract()
    units = []
    late_value = "synthetic-inherited-late-credential"
    for index, module in enumerate(sorted({fact.module_id for fact in bundle.facts})):
        fact = next(fact for fact in bundle.facts if fact.module_id == module)
        compilation = Compilation(title=f"叶级{index}", claims=[{
            "text": f"叶级职责{index}", "evidence_ids": fact.source_ref_ids}],
            unknowns=[f"旧检查点保留的完整未知{index}：{late_value}"])
        units.append(SimpleNamespace(kind="compile", key=f"compile:{index}", status="completed",
                                     payload={"module_id": module}, result=compilation.model_dump()))
    build = SimpleNamespace(id=str(uuid4()), source_commit=snapshot.commit_sha, recipe_hash=recipe_hash())
    finalize = SimpleNamespace(kind="finalize", key="finalize", status="completed", result={
        "bundle_hash": checkpoint_digest(bundle.model_dump()), "source_commit": build.source_commit,
        "recipe_hash": build.recipe_hash})
    privacy = SimpleNamespace(kind="privacy", key="privacy:0", status="completed")
    tasks = next_summary_tasks(bundle, units)
    unit = ready(tasks[0], unknown="新的概览未知")
    compilation = Compilation.model_validate(unit.result["compilation"])
    unit.status, unit.result = "running", None
    inputs = [*units, finalize, privacy, unit]
    ledger = PrivacyLedger(tmp_path / "private")
    guard = SimpleNamespace(filter=LedgerPrivacyFilter(ledger), close=AsyncMock())

    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def scalars(self, *args): return SimpleNamespace(all=lambda: inputs)

    monkeypatch.setattr("src.tasks.brain_task.SyncSession", Session)
    monkeypatch.setattr("src.tasks.brain_task._guard", lambda *args: guard)
    monkeypatch.setattr("src.tasks.brain_task.read_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(ArtifactStore, "read_work", lambda *args: bundle.model_dump())
    return SimpleNamespace(bundle=bundle, units=units, build=build, tasks=tasks, unit=unit,
                           inputs=inputs, guard=guard, ledger=ledger, late_value=late_value,
                           compilation=compilation, snapshot=snapshot)


@pytest.mark.asyncio
async def test_late_hmac_finding_does_not_invalidate_completed_sibling(late_privacy_case, monkeypatch):
    case = late_privacy_case
    async def compile_result(packet, guard, *, summary_proof):
        summary_proof.validate(packet)
        # Simulate a discovery made by the existing complete output scanner.
        case.ledger.remember([{"value": case.late_value, "type": "SECRET"}])
        return case.compilation
    model = AsyncMock(side_effect=compile_result)
    monkeypatch.setattr("src.tasks.brain_task.compile_packet", model)
    case.unit.result = await _execute(case.unit, case.build, SimpleNamespace(), "lease")
    case.unit.status = "completed"
    # The old worker sanitized only the persisted derived unknown copy, then
    # rebuilding this same sibling rejected that changed wrapper field.
    check_summary_task(case.bundle, case.unit, case.inputs)
    following = next_summary_tasks(case.bundle, case.inputs)
    assert following
    assert "all_unknowns" not in case.unit.result
    assert case.late_value not in encoded(case.unit.result)
    assert case.ledger.mask(case.late_value) != case.late_value
    model.assert_awaited_once()
    case.guard.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_does_not_redact_immutable_summary_envelope_metadata(late_privacy_case, monkeypatch):
    case = late_privacy_case
    child_hash = next(iter(case.unit.payload["synthesis"]["child_hashes"].values()))
    async def compile_result(packet, guard, *, summary_proof):
        summary_proof.validate(packet)
        case.ledger.remember([{"value": child_hash, "type": "SECRET"}])
        return case.compilation
    monkeypatch.setattr("src.tasks.brain_task.compile_packet", AsyncMock(side_effect=compile_result))
    result = await _execute(case.unit, case.build, SimpleNamespace(), "lease")
    assert case.ledger.mask(child_hash) != child_hash
    assert result["child_hashes"] == case.unit.payload["synthesis"]["child_hashes"]
    assert result["packet_digest"] == checkpoint_digest(case.unit.payload)
    assert result["compilation"] == case.compilation.model_dump()
    assert "all_unknowns" not in result


@pytest.mark.asyncio
async def test_every_inherited_unknown_reaches_next_input_and_final_detail(late_privacy_case, monkeypatch):
    case = late_privacy_case
    async def compile_result(packet, guard, *, summary_proof):
        summary_proof.validate(packet)
        case.ledger.remember([{"value": case.late_value, "type": "SECRET"}])
        return case.compilation
    monkeypatch.setattr("src.tasks.brain_task.compile_packet", AsyncMock(side_effect=compile_result))
    case.unit.result = await _execute(case.unit, case.build, SimpleNamespace(), "lease")
    case.unit.status = "completed"
    for _ in range(20):
        tasks = next_summary_tasks(case.bundle, case.inputs)
        if not tasks:
            break
        for task in tasks:
            all_unknowns = task.payload["synthesis"]["all_unknowns"]
            if task.kind == "project_summary":
                inherited = {value for unit in case.units for value in unit.result["unknowns"]}
                assert inherited <= set(all_unknowns)
                # It is the full canonical scanner input, including inherited
                # details omitted only from the explicit upper semantic view.
                assert case.late_value in encoded(task.payload)
                assert case.late_value not in encoded(sanitize_structure(task.payload, case.guard.filter))
                bound = SourceBoundPlan.from_packet(task.payload, compact_packet(task.payload))
                assert bound.synthesis_wire["inherited_limitations"]["count"] == len(all_unknowns)
            case.inputs.append(ready(task))
    else:
        pytest.fail("synthetic hierarchy did not converge")
    hierarchy = hierarchy_for_assembly(case.bundle, case.inputs)
    expected = {value for unit in case.units for value in unit.result["unknowns"]}
    assert expected <= set(hierarchy["all_unknowns"]["project"])
    compiled = [(unit.payload["module_id"], Compilation.model_validate(unit.result)) for unit in case.units]
    cards = assemble_cards(case.bundle, compiled, [], hierarchy=hierarchy)
    project = next(card for card in cards if card.kind == "project")
    assert expected <= set(project.detail_unknowns)
    # The existing final assembly HMAC pass still protects all complete detail.
    safe = sanitize_structure(project.model_dump(), case.guard.filter)
    assert case.late_value not in encoded(safe)
    assert len(safe["detail_unknowns"]) == len(project.detail_unknowns)


@pytest.mark.asyncio
async def test_reconstructed_unknowns_are_actually_passed_to_full_input_scanner(late_privacy_case, transport, monkeypatch):
    case = late_privacy_case
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "auto")
    case.ledger.remember([{"value": case.late_value, "type": "SECRET"}])
    case.unit.status, case.unit.result = "completed", wrap_summary_result(case.unit.payload, case.compilation)
    case.inputs.extend(ready(task) for task in next_summary_tasks(case.bundle, case.inputs))
    project = next(task for task in next_summary_tasks(case.bundle, case.inputs) if task.kind == "project_summary")
    expected = {value for unit in case.units for value in unit.result["unknowns"]}
    assert expected <= set(project.payload["synthesis"]["all_unknowns"])
    transport.guard.filter = case.guard.filter
    clean = transport.guard.clean
    scanned = []
    async def capture(text):
        scanned.append(json.loads(text))
        return await clean(text)
    monkeypatch.setattr(transport.guard, "clean", capture)
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=encoded({
        "title": "项目概览", "slots": {"S1": {"texts": ["完整项目解释"], "unknowns": ["最终未知"]}},
        "unknowns": [], "document_path": None}))])
    result = await compile_packet(project.payload, transport.guard)
    assert scanned[0] == project.payload
    assert expected <= set(scanned[0]["synthesis"]["all_unknowns"])
    assert case.late_value not in mock.calls[0][1]["content"]
    assert result.unknowns == ["最终未知"]


def test_unexpected_derived_result_copy_is_rejected_not_ignored(late_privacy_case):
    case = late_privacy_case
    case.unit.status, case.unit.result = "completed", wrap_summary_result(case.unit.payload, case.compilation)
    case.unit.result["all_unknowns"] = copy.deepcopy(case.unit.payload["synthesis"]["all_unknowns"])
    with pytest.raises(ValueError, match="来源契约"):
        check_summary_task(case.bundle, case.unit, case.inputs)
