"""Offline hierarchical planning, coverage and tamper-resistance contracts."""

import copy
from types import SimpleNamespace

import pytest

from src.config import settings
from src.core.project_brain.compiler import CompilationLimitError, encoded, packet_wire, token_bound
from src.core.project_brain.context import compact_packet
from src.core.project_brain.schema import Compilation, Fact, FactBundle, Relation, SourceRef
from src.core.project_brain.source_bound import SourceBoundPlan
from src.core.project_brain.summary import (
    check_summary_task, hierarchy_for_assembly, next_summary_tasks, wrap_summary_result,
)
from src.core.project_brain import summary as summary_module


def fixture(module_counts=(2, 1)):
    facts, refs, units = [], [], []
    number = 0
    for module, count in enumerate(module_counts):
        for _ in range(count):
            ref = SourceRef(id=f"evidence:{number}", commit_sha="a" * 40, path=f"m{module}/f{number}.py",
                            blob_sha="b" * 40, line_start=1, line_end=3, symbol_id=f"fact:{number}")
            fact = Fact(id=f"fact:{number}", kind="function", name=f"f{number}", module_id=f"m{module}",
                        source_ref_ids=[ref.id], extractor="fixture")
            compilation = Compilation(title=f"声明{number}", claims=[{"text": f"完整职责{number}", "evidence_ids": [ref.id]}],
                                      unknowns=[f"未知{number}"])
            units.append(SimpleNamespace(key=f"compile:{number}", kind="compile", payload={"module_id": f"m{module}"},
                                         status="completed", input_hash=f"input{number}", result=compilation.model_dump()))
            facts.append(fact)
            refs.append(ref)
            number += 1
    bundle = FactBundle(source_commit="a" * 40, files={}, modules={f"m{i}": f"模块{i}" for i in range(len(module_counts))},
                        facts=facts, source_refs=refs, relations=[], source_fingerprint="f" * 64, issues=["未执行项目"])
    return bundle, units


def ready(task, *, text="完整集合职责及边界", unknown="新的综合限制"):
    plan = SourceBoundPlan.from_packet(task.payload, compact_packet(task.payload))
    compilation = plan.bind(encoded({"title": "综合概览", "slots": {"S1": {"texts": [text], "unknowns": [unknown]}},
                                     "unknowns": [], "document_path": None}))
    return SimpleNamespace(key=task.key, kind=task.kind, payload=task.payload,
                           input_hash=task.input_hash, result=wrap_summary_result(task.payload, compilation), status="completed")


def finish(bundle, units):
    for _ in range(20):
        tasks = next_summary_tasks(bundle, units)
        if not tasks:
            return hierarchy_for_assembly(bundle, units)
        units.extend(ready(task) for task in tasks)
    pytest.fail("planner did not converge")


def test_module_roots_precede_complete_project_synthesis(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture()
    tasks = next_summary_tasks(bundle, units)
    assert {task.kind for task in tasks} == {"module_summary"}
    assert len(tasks) == 2
    first = ready(tasks[0])
    units.append(first)
    assert [task.key for task in next_summary_tasks(bundle, units)] == [tasks[1].key]
    units.append(ready(tasks[1]))
    projects = next_summary_tasks(bundle, units)
    assert len(projects) == 1 and projects[0].kind == "project_summary"
    assert projects[0].payload["synthesis"]["module_ids"] == ["m0", "m1"]
    assert projects[0].payload["synthesis"]["leaf_ids"] == ["compile:0", "compile:1", "compile:2"]
    assert {selection["source_key"] for selection in projects[0].payload["synthesis"]["selections"].values()} == {
        tasks[0].key, tasks[1].key}
    units.append(ready(projects[0]))
    hierarchy = hierarchy_for_assembly(bundle, units)
    assert set(hierarchy["modules"]) == {"m0", "m1"}
    assert hierarchy["coverage"]["root"] == projects[0].key
    assert {"未知0", "未知1", "未知2", "新的综合限制"} <= set(hierarchy["all_unknowns"]["project"])
    assert next_summary_tasks(bundle, units) == []


def test_stable_plan_uses_numeric_leaf_order_and_not_completion_order(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture((12,))
    initial = next_summary_tasks(bundle, units)
    reversed_plan = next_summary_tasks(bundle, list(reversed(units)))
    assert initial == reversed_plan
    sources = [selection["source_key"] for selection in initial[0].payload["synthesis"]["selections"].values()]
    assert sources == [f"compile:{i}" for i in range(12)]


def test_max_24_children_reduces_level_and_retains_intermediates(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture((49,))
    first = next_summary_tasks(bundle, units)
    assert [len(task.payload["items"]) for task in first] == [24, 24, 1]
    units.extend(ready(task) for task in first)
    second = next_summary_tasks(bundle, units)
    assert len(second) == 1 and second[0].payload["synthesis"]["level"] == 1
    hierarchy = finish(bundle, units)
    assert len(hierarchy["intermediates"]["m0"]) == 3
    assert hierarchy["coverage"]["leaf_ids"] == [f"compile:{i}" for i in range(49)]


@pytest.mark.parametrize("status", ["pending", "running", "failed"])
def test_unfinished_dependencies_never_create_upper_level(status, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture()
    units[0].status = status
    assert next_summary_tasks(bundle, units) == []
    with pytest.raises(ValueError, match="来源契约"):
        hierarchy_for_assembly(bundle, units)
    units[0].status = "completed"
    tasks = next_summary_tasks(bundle, units)
    queued = ready(tasks[0])
    queued.status = status
    queued.result = None
    units.extend([queued, ready(tasks[1])])
    assert next_summary_tasks(bundle, units) == []
    with pytest.raises(ValueError, match="来源契约"):
        hierarchy_for_assembly(bundle, units)


@pytest.mark.parametrize("tamper", ["input_hash", "payload", "child_hash", "range", "result", "scope"])
def test_existing_checkpoint_authentication_rebuilds_canonical_input(tamper, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture((2,))
    unit = ready(next_summary_tasks(bundle, units)[0])
    if tamper == "input_hash":
        unit.input_hash = "x" * 64
    elif tamper == "payload":
        unit.payload["items"][0]["fact"]["claims"][0]["text"] = "伪造来源解释"
    elif tamper == "child_hash":
        child = unit.payload["synthesis"]["child_ids"][0]
        unit.payload["synthesis"]["child_hashes"][child] = "e" * 64
    elif tamper == "range":
        child = unit.payload["synthesis"]["child_ids"][0]
        unit.payload["synthesis"]["selections"][child]["claim_end"] = 999
    elif tamper == "result":
        unit.result["leaf_ids"] = ["compile:99"]
    else:
        unit.result["compilation"]["claims"][0]["source_scope"] = None
    units.append(unit)
    with pytest.raises(ValueError, match="来源契约"):
        check_summary_task(bundle, unit, units)


def test_dependency_replacement_and_unexpected_root_are_rejected(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture((1,))
    unit = ready(next_summary_tasks(bundle, units)[0])
    units.append(unit)
    units[0].result["claims"][0]["text"] = "内容改变"
    with pytest.raises(ValueError, match="来源契约"):
        next_summary_tasks(bundle, units)
    bundle, units = fixture((1,))
    unit.key = "project_summary:9:9"
    units.append(unit)
    with pytest.raises(ValueError, match="来源契约"):
        next_summary_tasks(bundle, units)


def test_full_records_split_without_text_loss_and_unknown_only_keeps_parent_refs(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 2500)
    bundle, units = fixture((1,))
    compilation = Compilation(title="完整说明", claims=[{"text": f"职责{i}：" + "甲" * 280, "evidence_ids": ["evidence:0"]}
                                                      for i in range(4)],
                              unknowns=["限制：" + "乙" * 420, "其余动态行为未知"])
    units[0].result = compilation.model_dump()
    tasks = next_summary_tasks(bundle, units)
    claims, unknowns = [], []
    for task in tasks:
        assert token_bound(encoded(packet_wire(task.payload))) <= 2500
        for item in task.payload["items"]:
            claims.extend(item["fact"]["claims"])
            unknowns.extend(item["fact"]["unknowns"])
            assert item["evidence"] == [bundle.source_refs[0].model_dump()]
    assert claims == [claim.model_dump() for claim in compilation.claims]
    assert unknowns == compilation.unknowns
    assert any(not item["fact"]["claims"] for task in tasks for item in task.payload["items"])


def test_oversized_single_record_fails_instead_of_truncation(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 1800)
    bundle, units = fixture((1,))
    units[0].result["claims"][0]["text"] = "长" * 1000
    with pytest.raises(CompilationLimitError, match="单条结论"):
        next_summary_tasks(bundle, units)


def test_candidate_and_resolved_relations_keep_canonical_state(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture((2,))
    bundle.relations = [Relation(id="relation:1", from_id="fact:0", to_id="fact:1", target_name="f1", type="calls",
                                resolution="resolved", source_ref_ids=["evidence:0"]),
                        Relation(id="relation:2", from_id="fact:0", to_id=None, target_name="dynamic", type="calls",
                                resolution="candidate", candidates=["fact:1"], source_ref_ids=["evidence:0"])]
    tasks = next_summary_tasks(bundle, units)
    relations = {value["id"]: value for item in tasks[0].payload["items"] for value in item["relations"]}
    assert relations == {relation.id: relation.model_dump() for relation in bundle.relations}
    hierarchy = finish(bundle, units)
    assert hierarchy["project"].claims[0].source_scope.kind == "summary_synthesis"


def test_nonconverging_single_child_groups_fail_explicitly(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture((2,))
    # Keep an irreducible metadata-heavy case even when every newly authored
    # overview obeys the producer's prose budget.
    bundle.issues = ["完整覆盖限制" * 150]
    full = next_summary_tasks(bundle, units)[0]
    maximum = token_bound(encoded(packet_wire(full.payload))) - 1
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", maximum)
    text = "归纳" * 40
    # Force independent first-level complete overviews; they may not loop
    # indefinitely when their resulting complete records still cannot merge.
    first = next_summary_tasks(bundle, units)
    assert len(first) == 2
    units.extend(ready(task, text=text) for task in first)
    with pytest.raises(CompilationLimitError, match="无法归并"):
        next_summary_tasks(bundle, units)


def test_leaf_cannot_use_other_module_evidence_to_claim_ownership(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture((1, 1))
    units[0].result["claims"][0]["evidence_ids"] = ["evidence:1"]
    with pytest.raises(ValueError, match="来源契约"):
        next_summary_tasks(bundle, units)


def test_tree_depth_is_hard_gate_without_publishing_partial_root(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    monkeypatch.setattr(summary_module, "MAX_SUMMARY_DEPTH", 1)
    bundle, units = fixture((25,))
    first = next_summary_tasks(bundle, units)
    assert len(first) == 2
    units.extend(ready(task) for task in first)
    with pytest.raises(CompilationLimitError, match="深度硬上限"):
        next_summary_tasks(bundle, units)
    with pytest.raises(CompilationLimitError, match="深度硬上限"):
        hierarchy_for_assembly(bundle, units)


def test_wrapper_rejects_mutated_scope_identity_and_scope_free_interpretation(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture((1,))
    task = next_summary_tasks(bundle, units)[0]
    wrapped = ready(task)
    compilation = Compilation.model_validate(wrapped.result["compilation"])
    compilation.claims[0].source_scope.id = "forged-scope"
    with pytest.raises(ValueError, match="来源契约"):
        wrap_summary_result(task.payload, compilation)
    compilation.claims[0].source_scope = None
    with pytest.raises(ValueError, match="来源契约"):
        wrap_summary_result(task.payload, compilation)


@pytest.mark.parametrize("purpose,level,module_counts,expected_view", [
    ("module", 0, (1,), "symbol_graph"),
    ("module", 1, (25,), "resolution_counts"),
    ("project", 0, (1, 1), "resolution_counts"),
])
def test_summary_scope_binds_exact_stage_relationship_view(
        monkeypatch, purpose, level, module_counts, expected_view):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture(module_counts)
    for _ in range(3):
        tasks = next_summary_tasks(bundle, units)
        selected = [task for task in tasks if task.payload["synthesis"]["purpose"] == purpose
                    and task.payload["synthesis"]["level"] == level]
        if selected:
            task = selected[0]
            break
        units.extend(ready(task) for task in tasks)
    else:
        pytest.fail("expected relationship-view stage not reached")

    wrapped = ready(task)
    compilation = Compilation.model_validate(wrapped.result["compilation"])
    scope = compilation.claims[0].source_scope
    assert scope.relationship_view == expected_view
    assert wrap_summary_result(task.payload, compilation) == wrapped.result

    # Older schemas may read absent view metadata as null. A fresh summary
    # checkpoint must still prove the exact relationship view it was given.
    for bad_view in [None, "resolution_counts" if expected_view == "symbol_graph" else "symbol_graph"]:
        scope.relationship_view = bad_view
        with pytest.raises(ValueError, match="来源契约"):
            wrap_summary_result(task.payload, compilation)


def test_inherited_unknown_detail_is_retained_without_repeated_overview_payload(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture((2,))
    tasks = next_summary_tasks(bundle, units)
    units.extend(ready(task, unknown="模块综合未知") for task in tasks)
    project = next_summary_tasks(bundle, units)[0]
    meta = project.payload["synthesis"]
    assert {"未知0", "未知1", "模块综合未知"} == set(meta["all_unknowns"])
    assert project.payload["items"][0]["fact"]["unknowns"] == ["模块综合未知"]
    wire = packet_wire(project.payload)
    assert wire["inherited_limitations"] == {"count": 3, "detail_retained": True}
    assert all("未知0" not in encoded(child) for child in wire["children"])
    assert "未知0" in encoded(project.payload)


def test_missing_module_leaf_and_duplicate_unit_identity_fail(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture()
    with pytest.raises(ValueError, match="来源契约"):
        next_summary_tasks(bundle, units[:2])
    with pytest.raises(ValueError, match="来源契约"):
        next_summary_tasks(bundle, [*units, copy.deepcopy(units[0])])


def test_declared_module_without_facts_is_not_given_fake_evidence_root(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture((1,))
    bundle.modules["empty"] = "无可提取事实模块"
    hierarchy = finish(bundle, units)
    assert hierarchy["coverage"]["module_ids"] == ["m0"]
    assert "empty" not in hierarchy["modules"]


def test_durable_budget_counters_do_not_change_canonical_summary_input(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture((1,))
    task = next_summary_tasks(bundle, units)[0]
    completed = ready(task)
    completed.payload = {**completed.payload, "_reserved_tokens": 0, "_unknown_tokens": 6711}
    assert check_summary_task(bundle, completed, [*units, completed]) == task.payload
    assert next_summary_tasks(bundle, [*units, completed])[0].kind == "project_summary"
    completed.payload["_reserved_tokens"] = -1
    with pytest.raises(ValueError, match="来源契约"):
        check_summary_task(bundle, completed, [*units, completed])


def test_undeclared_payload_metadata_cannot_bypass_canonical_summary_check(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    bundle, units = fixture((1,))
    task = next_summary_tasks(bundle, units)[0]
    completed = ready(task)
    completed.payload = {**completed.payload, "_future_metadata": "unclassified"}
    with pytest.raises(ValueError, match="来源契约"):
        check_summary_task(bundle, completed, [*units, completed])
