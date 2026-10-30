"""Hierarchical overviews preserve detail without claiming semantic proof."""

import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.core.project_brain.artifacts import ArtifactStore, assemble_cards, render_card, validate
from src.core.project_brain.extractor import FactExtractor
from src.core.project_brain.schema import BrainManifest, Compilation, InferenceScope, Relation, stable_id
from src.core.project_brain.summary import hierarchy_for_assembly, next_summary_tasks, wrap_summary_result
from src.core.project_brain.synthesis_wire import synthesis_relationship_view
from src.core.project_brain.trusted_packet import checkpoint_digest
from tests.unit.test_project_brain import repository, snapshot  # noqa: F401


def synthesis(text, refs, modules, children, *, unknowns=(), document_path=None):
    """Use a service-assigned scope, not model-selected evidence identities."""
    scope = InferenceScope(
        id=stable_id("scope", text), kind="summary_synthesis", packet_digest="a" * 64,
        fact_id=None, relation_ids=[], source_ref_ids=refs, fragment_index=0, fragment_total=1,
        module_ids=modules, child_ids=children,
    )
    return Compilation(title=text, claims=[{"text": text, "evidence_ids": refs, "source_scope": scope}],
                       unknowns=list(unknowns), document_path=document_path)


def completed_summary_unit(task, *, text=None, document_path=None):
    """Simulate one valid semantic result while preserving the exact input proof."""
    refs = sorted({ref["id"] for item in task.payload["items"] for ref in item["evidence"]})
    meta = task.payload["synthesis"]
    result = synthesis(text or "综合解释：" + task.key, refs, meta["module_ids"], meta["child_ids"],
                       unknowns=["综合层新增未知：" + task.key], document_path=document_path)
    result.claims[0].source_scope.packet_digest = checkpoint_digest(task.payload)
    result.claims[0].source_scope.id = stable_id("scope", checkpoint_digest(task.payload), "S1")
    result.claims[0].source_scope.relationship_view = synthesis_relationship_view(meta["purpose"], meta["level"])
    return SimpleNamespace(key=task.key, kind=task.kind, payload=task.payload, input_hash=task.input_hash,
                           status="completed", result=wrap_summary_result(task.payload, result))


def finish_summary_tree(bundle, units):
    """Complete a bounded synthetic tree without executing model or project code."""
    for _ in range(20):
        tasks = next_summary_tasks(bundle, units)
        if not tasks:
            return hierarchy_for_assembly(bundle, units)
        units.extend(completed_summary_unit(task) for task in reversed(tasks))
    raise AssertionError("synthetic summary tree did not converge")


@pytest.fixture
def hierarchy_case(snapshot):
    bundle = FactExtractor(snapshot).extract()
    compiled, modules, intermediates, all_unknowns, leaf_ids = [], {}, {}, {}, []
    for index, module in enumerate(sorted({f.module_id for f in bundle.facts})):
        refs = next(f.source_ref_ids for f in bundle.facts if f.module_id == module)
        leaf_id = f"compile:{index}"
        leaf_ids.append(leaf_id)
        compiled.append((module, Compilation(title=f"叶级{index}",
            claims=[{"text": f"完整叶级解释{index}", "evidence_ids": refs}], unknowns=[f"原始未知{index}"])))
        intermediates[module] = [synthesis(f"中间模块解释{index}", refs, [module], [leaf_id],
                                          unknowns=[f"中间未知{index}"])]
        modules[module] = synthesis(f"模块概览{index}", refs, [module], [f"module:{index}:0"],
                                   unknowns=[f"最终模块未知{index}"],
                                   document_path=f"docs/project-brain/自由布局/模块{index}.md")
        all_unknowns[module] = [f"原始未知{index}", f"中间未知{index}"]
    module_ids = sorted(modules)
    root_refs = sorted({ref for overview in modules.values() for claim in overview.claims for ref in claim.evidence_ids})
    intermediates["project"] = [synthesis("项目中间综合", root_refs, module_ids,
                                         [f"module:{i}:root" for i in range(len(module_ids))],
                                         unknowns=["中间项目未知"])]
    all_unknowns["project"] = ["中间项目未知"]
    hierarchy = {
        "modules": modules,
        "project": synthesis("项目整体综合", root_refs, module_ids, ["project:0:0"], unknowns=["最终项目未知"]),
        "intermediates": intermediates,
        "all_unknowns": all_unknowns,
        "coverage": {"module_ids": module_ids, "leaf_ids": leaf_ids, "root": "project:1:0"},
    }
    return bundle, compiled, hierarchy


def test_overviews_are_distinct_from_complete_leaf_and_intermediate_details(hierarchy_case, snapshot):
    bundle, compiled, hierarchy = hierarchy_case
    cards = assemble_cards(bundle, compiled, [], hierarchy=hierarchy)
    for index, module in enumerate(sorted(hierarchy["modules"])):
        card = next(c for c in cards if c.id == module)
        assert [c.text for c in card.claims] == [f"模块概览{index}"]
        assert [c.text for c in card.detail_claims] == [f"完整叶级解释{index}", f"中间模块解释{index}"]
        assert {f"原始未知{index}", f"中间未知{index}"} <= set(card.detail_unknowns)
        assert f"最终模块未知{index}" in card.unknowns
        assert card.document_path == f"docs/project-brain/自由布局/模块{index}.md"
    project = next(c for c in cards if c.kind == "project")
    assert [c.text for c in project.claims if c.knowledge_type == "AI_INFERRED"] == ["项目整体综合"]
    assert [c.text for c in project.detail_claims] == ["项目中间综合"]
    assert set(project.links) == set(hierarchy["modules"])
    assert set(hierarchy["modules"]) == set(project.claims[-1].source_scope.module_ids)
    assert {u for _, value in compiled for u in value.unknowns} <= set(project.detail_unknowns)
    assert "最终项目未知" in project.unknowns
    assert "中间项目未知" in project.detail_unknowns
    metrics = validate(bundle, cards, snapshot)
    assert metrics["semantic_support_status"] == "unreviewed"


@pytest.mark.parametrize("mutation", ["missing_module", "extra_module", "duplicate_module", "duplicate_leaf", "empty_root"])
def test_incomplete_or_ambiguous_hierarchy_cannot_fall_back_to_old_summary(hierarchy_case, mutation):
    bundle, compiled, hierarchy = hierarchy_case
    if mutation == "missing_module":
        hierarchy["modules"].pop(next(iter(hierarchy["modules"])))
    elif mutation == "extra_module":
        hierarchy["modules"]["foreign-module"] = hierarchy["project"]
    elif mutation == "duplicate_module":
        hierarchy["coverage"]["module_ids"].append(hierarchy["coverage"]["module_ids"][0])
    elif mutation == "duplicate_leaf":
        hierarchy["coverage"]["leaf_ids"].append(hierarchy["coverage"]["leaf_ids"][0])
    else:
        hierarchy["coverage"]["root"] = ""
    with pytest.raises(ValueError, match="完整模块"):
        assemble_cards(bundle, compiled, [hierarchy["project"]], hierarchy=hierarchy)


def test_project_root_cannot_bind_only_a_subset_of_modules(hierarchy_case):
    bundle, compiled, hierarchy = hierarchy_case
    hierarchy["project"].claims[0].source_scope.module_ids.pop()
    with pytest.raises(ValueError, match="完整模块集合"):
        assemble_cards(bundle, compiled, [], hierarchy=hierarchy)


def test_module_overview_cannot_bind_a_different_module(hierarchy_case):
    bundle, compiled, hierarchy = hierarchy_case
    first, second = sorted(hierarchy["modules"])[:2]
    hierarchy["modules"][first].claims[0].source_scope.module_ids = [second]
    with pytest.raises(ValueError, match="来源归属"):
        assemble_cards(bundle, compiled, [], hierarchy=hierarchy)


def test_unbound_overview_is_not_published_as_covered_synthesis(hierarchy_case):
    bundle, compiled, hierarchy = hierarchy_case
    hierarchy["project"].claims[0].source_scope = None
    with pytest.raises(ValueError, match="完整模块集合"):
        assemble_cards(bundle, compiled, [], hierarchy=hierarchy)


def test_detail_evidence_and_knowledge_type_receive_the_same_validation(hierarchy_case, snapshot):
    bundle, compiled, hierarchy = hierarchy_case
    cards = assemble_cards(bundle, compiled, [], hierarchy=hierarchy)
    module = next(c for c in cards if c.kind == "module")
    module.detail_claims[0].evidence_ids = ["foreign-evidence"]
    with pytest.raises(ValueError, match="卡片引用无效"):
        validate(bundle, cards, snapshot)
    module.detail_claims[0].evidence_ids = bundle.facts[0].source_ref_ids
    module.detail_claims[1].knowledge_type = "FACT"
    with pytest.raises(ValueError, match="推断来源范围或知识类型"):
        validate(bundle, cards, snapshot)


@pytest.mark.parametrize("field,value", [("module_ids", ["foreign-module"]), ("child_ids", []),
                                        ("child_ids", ["compile:0", "compile:0"])])
def test_detail_synthesis_scope_rejects_unknown_or_duplicate_coverage(hierarchy_case, snapshot, field, value):
    bundle, compiled, hierarchy = hierarchy_case
    cards = assemble_cards(bundle, compiled, [], hierarchy=hierarchy)
    module = next(c for c in cards if c.kind == "module")
    setattr(module.detail_claims[-1].source_scope, field, value)
    with pytest.raises(ValueError, match="分层汇总来源覆盖无效"):
        validate(bundle, cards, snapshot)


def test_candidate_cross_module_relation_never_becomes_architecture_or_flow_edge(hierarchy_case, snapshot):
    bundle, compiled, hierarchy = hierarchy_case
    origin = next(f for f in bundle.facts if f.kind == "interface")
    destination = next(f for f in bundle.facts if f.module_id != origin.module_id)
    candidate = Relation(id=stable_id("relation", "hierarchy-candidate"), from_id=origin.id,
                         type="CALLS", to_id=None, target_name="dynamic_candidate", resolution="candidate",
                         candidates=[destination.id], source_ref_ids=origin.source_ref_ids)
    bundle.relations.append(candidate)
    cards = assemble_cards(bundle, compiled, [], hierarchy=hierarchy)
    assert all(candidate.id not in claim.relation_ids for card in cards if card.kind in {"architecture", "flow"}
               for claim in card.claims)
    flow = next(c for c in cards if c.kind == "flow" and c.module_id == origin.module_id)
    assert any("candidate" in u and "未展开" in u for u in flow.unknowns)
    assert all(claim.source_scope is None for card in cards if card.kind == "flow" for claim in card.claims)
    validate(bundle, cards, snapshot)


def manifest(bundle):
    return BrainManifest(generator="hierarchy-fixture", build_id=str(uuid4()), project_id=1, branch="main",
                         source_commit=bundle.source_commit, brain_version=1, recipe_hash="a" * 64,
                         source_fingerprint=bundle.source_fingerprint, files={}, coverage={}, issues=[])


def test_rendering_retains_full_details_and_unknowns_but_leads_with_overview(hierarchy_case):
    bundle, compiled, hierarchy = hierarchy_case
    cards = assemble_cards(bundle, compiled, [], hierarchy=hierarchy)
    card = next(c for c in cards if c.kind == "module")
    rendered = render_card(card, manifest(bundle), bundle, "https://gitlab.invalid/synthetic", cards)
    assert rendered.index(card.claims[0].text) < rendered.index("<details>") < rendered.index(card.detail_claims[0].text)
    assert "<summary>完整解释与限制</summary>" in rendered


@pytest.mark.parametrize("view,label", [("resolution_counts", "模型未读取本层逐条符号关系"),
                                       ("symbol_graph", "候选与未解析目标仍保持原状态")])
def test_rendered_synthesis_discloses_actual_relationship_view(hierarchy_case, view, label):
    bundle, compiled, hierarchy = hierarchy_case
    cards = assemble_cards(bundle, compiled, [], hierarchy=hierarchy)
    card = next(c for c in cards if c.kind == "module")
    card.claims[0].source_scope.relationship_view = view
    rendered = render_card(card, manifest(bundle), bundle, "https://gitlab.invalid/synthetic", cards)
    assert label in rendered
    assert "语义支持尚未独立核验" in rendered
    assert card.detail_claims[0].text in rendered
    assert "语义支持尚未独立核验" in rendered
    assert all(u in rendered for u in card.unknowns + card.detail_unknowns)
    assert bundle.source_commit in rendered and "/-/blob/" + bundle.source_commit in rendered
    assert rendered.rstrip().endswith("</details>")


def test_immutable_export_retains_overview_and_complete_details(hierarchy_case, tmp_path):
    bundle, compiled, hierarchy = hierarchy_case
    cards = assemble_cards(bundle, compiled, [], hierarchy=hierarchy)
    brain_manifest = manifest(bundle)
    store = ArtifactStore(str(tmp_path / "artifacts"))
    store.finalize(brain_manifest, bundle, cards, "https://gitlab.invalid/synthetic")
    restored = store.read_cards(brain_manifest.build_id)
    assert [c.model_dump() for c in restored] == [c.model_dump() for c in cards]
    assert store.export_path(brain_manifest.build_id).is_file()
    assert "cards.json" in store.read_manifest(brain_manifest.build_id).service_files


def test_old_card_contract_remains_readable_without_detail_fields(hierarchy_case):
    bundle, compiled, _ = hierarchy_case
    original = assemble_cards(bundle, compiled, [])
    assert all(not c.detail_claims and not c.detail_unknowns for c in original)
    encoded = original[0].model_dump()
    encoded.pop("detail_claims")
    encoded.pop("detail_unknowns")
    assert type(original[0]).model_validate(copy.deepcopy(encoded)).detail_claims == []


def test_empty_module_is_a_visible_coverage_gap_without_fabricated_card(hierarchy_case):
    bundle, compiled, hierarchy = hierarchy_case
    bundle.modules["empty-module"] = "未适配模块"
    cards = assemble_cards(bundle, compiled, [], hierarchy=hierarchy)
    assert all(c.id != "empty-module" for c in cards)
    project = next(c for c in cards if c.kind == "project")
    assert "empty-module" not in project.links
    assert any("未适配模块" in u and "未提取可编译事实" in u for u in project.unknowns)


def test_recursive_synthesis_covers_every_module_and_leaf_even_when_finish_order_changes(hierarchy_case, monkeypatch):
    bundle, compiled, _ = hierarchy_case
    monkeypatch.setattr("src.core.project_brain.summary.MAX_SUMMARY_CHILDREN", 2)
    units = [SimpleNamespace(key=f"compile:{i}", kind="compile", status="completed",
                             payload={"module_id": module}, result=value.model_dump())
             for i, (module, value) in enumerate(compiled)]
    first_module, first_result = compiled[0]
    for index in range(4):
        units.append(SimpleNamespace(key=f"compile:{len(units)}", kind="compile", status="completed",
                                     payload={"module_id": first_module}, result=first_result.model_dump()))
    baseline_units = copy.deepcopy(units)
    completed = finish_summary_tree(bundle, units)
    reordered = finish_summary_tree(bundle, list(reversed(baseline_units)))
    assert completed == reordered
    assert set(completed["coverage"]["module_ids"]) == {f.module_id for f in bundle.facts}
    assert len(completed["coverage"]["leaf_ids"]) == len(compiled) + 4
    assert completed["project"].claims[0].source_scope.kind == "summary_synthesis"
    assert set(completed["project"].claims[0].source_scope.module_ids) == set(bundle.modules)
    assert all(u in completed["all_unknowns"]["project"] for _, value in compiled for u in value.unknowns)
    assert completed["intermediates"][first_module]
    assert completed["intermediates"]["project"]


def test_failed_intermediate_blocks_project_synthesis_and_assembly(hierarchy_case):
    bundle, compiled, _ = hierarchy_case
    units = [SimpleNamespace(key=f"compile:{i}", kind="compile", status="completed",
                             payload={"module_id": module}, result=value.model_dump())
             for i, (module, value) in enumerate(compiled)]
    tasks = next_summary_tasks(bundle, units)
    failed = completed_summary_unit(tasks[0])
    failed.status = "failed"
    units.extend([failed, *[completed_summary_unit(task) for task in tasks[1:]]])
    assert not next_summary_tasks(bundle, units)
    with pytest.raises(ValueError, match="来源契约无效"):
        hierarchy_for_assembly(bundle, units)
    failed.status = "completed"
    assert all(task.kind == "project_summary" for task in next_summary_tasks(bundle, units))
