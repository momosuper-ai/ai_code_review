"""Summarization retains complete conclusions, limitations and source identity."""

from src.config import settings
from src.core.project_brain.artifacts import assemble_cards
from src.core.project_brain.compiler import encoded, packet_wire, token_bound
from src.core.project_brain.extractor import FactExtractor
from src.core.project_brain.schema import Compilation
from src.core.project_brain.summary import next_summary_tasks
from src.models.brain import BrainUnit
from tests.unit.test_project_brain import repository, snapshot  # noqa: F401


def test_summary_fragments_keep_every_complete_claim_unknown_and_source_ref(snapshot, monkeypatch) -> None:
    bundle = FactExtractor(snapshot).extract()
    ref = bundle.source_refs[0]
    module = next(f.module_id for f in bundle.facts if ref.id in f.source_ref_ids)
    bundle.modules = {module: bundle.modules[module]}
    bundle.facts = [fact for fact in bundle.facts if fact.module_id == module]
    bundle.relations = []
    claims = [{"text": f"完整结论{i}：" + "说明" * 180, "evidence_ids": [ref.id]} for i in range(8)]
    unknowns = ["静态源码无法确认运行时分派", "数据库访问尚未适配"]
    compilation = Compilation(title="模块解释", claims=claims, unknowns=unknowns)
    unit = BrainUnit(key="compile:0", kind="compile", status="completed",
                     payload={"module_id": module}, result=compilation.model_dump())
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 6000)
    written = next_summary_tasks(bundle, [unit])
    assert len(written) > 1
    found_claims, found_unknowns = [], []
    for summary in written:
        assert token_bound(encoded(packet_wire(summary.payload))) <= 6000
        for item in summary.payload["items"]:
            found_claims.extend(item["fact"]["claims"])
            found_unknowns.extend(item["fact"]["unknowns"])
            assert item["evidence"] == [ref.model_dump()]
    assert found_claims == [claim.model_dump() for claim in compilation.claims]
    assert found_unknowns == unknowns


def test_project_card_exposes_module_and_summary_unknowns(snapshot) -> None:
    bundle = FactExtractor(snapshot).extract()
    fact = bundle.facts[0]
    claim = {"text": "职责解释", "evidence_ids": fact.source_ref_ids}
    local = Compilation(title="模块", claims=[claim], unknowns=["候选调用尚未确认"])
    summary = Compilation(title="项目", claims=[claim], unknowns=["部署参数需要团队确认"])
    cards = assemble_cards(bundle, [(fact.module_id, local)], [summary])
    project = next(card for card in cards if card.kind == "project")
    assert {"候选调用尚未确认", "部署参数需要团队确认"} <= set(project.unknowns)
