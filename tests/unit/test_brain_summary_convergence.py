"""Bound generated overviews while retaining complete source/detail checkpoints."""
import copy
import json

import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.project_brain import summary
from src.core.project_brain.compiler import CompilationLimitError, compile_packet, encoded
from src.core.project_brain.compiler import packet_wire, token_bound
from src.core.project_brain.summary_budget import (
    SummaryProseTooLarge, require_summary_prose, summary_prose_limit, summary_prose_size,
)
from src.core.project_brain.schema import Compilation
from src.core.project_brain.context import compact_packet
from src.core.project_brain.source_bound import SourceBoundPlan
from tests.unit.test_brain_hierarchy_planner import fixture, ready
from tests.unit.test_brain_compiler_transport import model, transport  # noqa: F401
from tests.unit.test_brain_synthesis_transport import response, synthesis  # noqa: F401


def test_capacity_reaches_worker_factory_without_becoming_contract_error(monkeypatch):
    monkeypatch.setattr(settings, 'PROJECT_BRAIN_PACKET_MAX_TOKENS', 32000)
    monkeypatch.setattr(summary, 'MAX_SUMMARY_CHILDREN', 1)
    bundle, units = fixture((2,))
    leaves = summary.next_summary_tasks(bundle, units)
    completed = [ready(task) for task in leaves]
    units.extend(completed)
    with pytest.raises(CompilationLimitError, match='无法归并'):
        summary.check_summary_task(bundle, completed[0], units)


@pytest.mark.asyncio
async def test_oversized_valid_summary_is_corrected_before_checkpoint(synthesis, transport, monkeypatch):
    monkeypatch.setattr(settings, 'PROJECT_BRAIN_PACKET_MAX_TOKENS', 32000)
    monkeypatch.setattr(settings, 'PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE', 'auto')
    monkeypatch.setattr(settings, 'AGENT_FORMAT_RETRY_MAX', 1)
    large = response(text='完整但冗长的概览。' * 1500)
    compact = response(text='模块负责入口协调；动态行为仍需核实。')
    mock = model(monkeypatch, [ChatCompletionMessage(role='assistant', content=encoded(large)),
                              ChatCompletionMessage(role='assistant', content=encoded(compact))])
    before = copy.deepcopy(synthesis)
    result = await compile_packet(synthesis, transport.guard)
    assert len(mock.calls) == 2
    assert result.claims[0].text == compact['slots']['S1']['texts'][0]
    assert synthesis == before
    assert 'summary_prose_too_large' in mock.calls[1][-1]['content']
    assert mock.calls[1][-2]['content'] == '{}'
    assert large['slots']['S1']['texts'][0] not in encoded(mock.calls[1])
    assert all(call['max_tokens'] <= summary_prose_limit(32000) for call in mock.kwargs)


@pytest.mark.asyncio
async def test_summary_budget_exhaustion_is_terminal_capacity(synthesis, transport, monkeypatch):
    monkeypatch.setattr(settings, 'PROJECT_BRAIN_PACKET_MAX_TOKENS', 32000)
    monkeypatch.setattr(settings, 'PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE', 'auto')
    monkeypatch.setattr(settings, 'AGENT_FORMAT_RETRY_MAX', 1)
    large = ChatCompletionMessage(role='assistant', content=encoded(response(text='概览仍然冗长。' * 1800)))
    mock = model(monkeypatch, [large, large])
    with pytest.raises(CompilationLimitError, match='概览'):
        await compile_packet(synthesis, transport.guard)
    assert len(mock.calls) == 2


def test_large_leaf_details_converge_without_losing_detail_or_unknowns(monkeypatch):
    monkeypatch.setattr(settings,'PROJECT_BRAIN_PACKET_MAX_TOKENS',32000)
    bundle, units = fixture((49,))
    for index, unit in enumerate(units):
        unit.result['claims'][0]['text'] = f'完整声明{index}：'+ '原始叶结论与证据必须保留。'*400
    original = copy.deepcopy([unit.result for unit in units])
    rounds = 0
    while True:
        tasks = summary.next_summary_tasks(bundle,units)
        if not tasks: break
        rounds += 1
        assert rounds <= summary.MAX_SUMMARY_DEPTH+1
        for task in tasks:
            assert token_bound(encoded(packet_wire(task.payload))) <= 32000
            unit = ready(task,text='概览归纳职责与边界，保留动态行为不确定性。'*40)
            assert summary_prose_size(Compilation.model_validate(unit.result['compilation'])) <= summary_prose_limit(32000)
            units.append(unit)
    hierarchy = summary.hierarchy_for_assembly(bundle,units)
    assert hierarchy['coverage']['leaf_ids'] == [f'compile:{index}' for index in range(49)]
    assert {f'未知{index}' for index in range(49)} <= set(hierarchy['all_unknowns']['project'])
    assert [unit.result for unit in units[:49]] == original


def test_complete_authored_prose_counts_unicode_escaping_title_and_unknowns():
    value = Compilation(title='标题',claims=[{'text':'正文','evidence_ids':['evidence:0']}],unknowns=['限制'])
    before = summary_prose_size(value)
    value.unknowns.append('"\\\n隐私与不确定性'*300)
    assert summary_prose_size(value) > before
    with pytest.raises(SummaryProseTooLarge,match='^summary_prose_too_large$'):
        require_summary_prose(value,32000)
    assert len(value.unknowns[-1]) > 1000


def test_machine_provenance_size_does_not_consume_authored_prose_budget():
    value = Compilation(title='标题',claims=[{'text':'正文','evidence_ids':['evidence:0']}],unknowns=[])
    before = summary_prose_size(value)
    value.claims[0].evidence_ids = ['evidence:'+'a'*64]*500
    assert summary_prose_size(value) == before


def test_bad_provenance_is_not_hidden_by_oversized_prose(monkeypatch):
    monkeypatch.setattr(settings,'PROJECT_BRAIN_PACKET_MAX_TOKENS',32000)
    bundle, units = fixture((2,))
    task = summary.next_summary_tasks(bundle,units)[0]
    plan = SourceBoundPlan.from_packet(task.payload,compact_packet(task.payload))
    value = plan.bind(encoded(response(text='超长且来源不匹配。'*1000)))
    value.claims[0].evidence_ids = ['foreign-evidence']
    with pytest.raises(ValueError,match='来源契约') as caught:
        summary.wrap_summary_result(task.payload,value)
    assert type(caught.value) is ValueError


@pytest.mark.asyncio
async def test_privacy_expansion_cannot_publish_an_oversized_summary(synthesis,transport,monkeypatch):
    from src.core.project_brain import compiler
    monkeypatch.setattr(settings,'PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE','auto')
    original = compiler.sanitize_structure
    def expanding(value,privacy):
        result = original(value,privacy)
        if type(result) is dict and set(result) == {'title','claims','unknowns','document_path'}:
            result['claims'][0]['text'] = 'SAFE_PLACEHOLDER_'*1000
        return result
    monkeypatch.setattr(compiler,'sanitize_structure',expanding)
    mock = model(monkeypatch,[ChatCompletionMessage(role='assistant',content=encoded(response()))])
    with pytest.raises(CompilationLimitError,match='隐私处理后'):
        await compile_packet(synthesis,transport.guard)
    assert len(mock.calls) == 1
