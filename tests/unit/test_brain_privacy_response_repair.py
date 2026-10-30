"""Synthetic scan responses exercise correction without weakening provenance."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.llm_client import ChatResult, TokenUsage
from src.core.project_brain.compiler import CompilationGuard, encoded
from src.core.project_brain.privacy import PrivacyLedger
from src.core.project_brain.exceptions import BudgetBusy, PrivacyResponseInvalid
from tests.unit.test_project_brain import database  # noqa: F401


def response(items):
    return ChatResult(ChatCompletionMessage(role="assistant", content=encoded({"items": items})),
                      TokenUsage(10, 2), finish_reason="stop")


@pytest.fixture
def scan(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    local = SimpleNamespace(chat=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", lambda **kwargs: local)
    charges, reservations = [], []
    guard = CompilationGuard(lambda usage, *args: charges.append(usage.total_tokens),
                             reservations.append, PrivacyLedger(tmp_path))
    records = [{"field": "source_excerpt", "text": "synthetic credential: AmberFixtureOnly"}]
    return SimpleNamespace(guard=guard, local=local, records=records, charges=charges,
                           reservations=reservations, root=tmp_path,
                           scope=guard.ledger.digest("synthetic scope"),
                           key=guard.ledger.digest("packet:v2:" + encoded(records)))


@pytest.mark.asyncio
@pytest.mark.parametrize("bad,reason", [("", "empty_value"), ("NotInInputFixtureOnly", "value_not_in_input")])
async def test_corrects_invalid_reference_with_closed_feedback_and_keeps_positive_findings(scan, bad, reason):
    sent = []

    async def chat(messages, **kwargs):
        sent.append(messages)
        if len(sent) == 1:
            return response([{"value": bad, "type": "SECRET"}])
        assert reason in messages[0]["content"]
        assert len(messages) == 2 and messages[1]["role"] == "user"
        assert "NotInInputFixtureOnly" not in encoded(messages)
        return response([{"value": "AmberFixtureOnly", "type": "SECRET"}])

    scan.local.chat.side_effect = chat
    await scan.guard._scan(scan.records, scan.scope, 0)
    assert len(sent) == 2 and scan.charges == [12, 12]
    assert json.loads(sent[0][1]["content"]) == json.loads(sent[1][1]["content"])
    assert scan.guard.ledger.receipt(scan.key) == "completed"
    assert scan.guard.ledger.mask("AmberFixtureOnly") != "AmberFixtureOnly"
    assert scan.guard.ledger.mask("NotInInputFixtureOnly") == "NotInInputFixtureOnly"
    assert all("AmberFixtureOnly" not in p.read_text(encoding="utf-8") for p in scan.root.rglob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("bad,reason", [
    ('not-json-FixturePrivateOnly', 'invalid_json'),
    ('[]', 'invalid_shape'),
    ('{"items":null}', 'invalid_shape'),
    ('{"items":[],"extra":"FixturePrivateOnly"}', 'invalid_shape'),
    ('{"items":[{"value":"AmberFixtureOnly","type":"UNSUPPORTED"}]}', 'invalid_item'),
    ('{"items":[{"value":null,"type":"SECRET"}]}', 'invalid_item'),
    ('{"items":[{"value":"","type":"SECRET"}]}', 'empty_value'),
    ('{"items":[{"value":"AmberFixtureOnly","type":"SECRET"},{"value":"FixturePrivateOnly","type":"SECRET"}]}', 'value_not_in_input'),
])
async def test_invalid_whole_response_is_bounded_and_never_commits_partial_findings(scan, bad, reason, monkeypatch):
    logs = []
    monkeypatch.setattr('src.core.project_brain.compiler.logger.warning', lambda s, *a: logs.append(s % a))
    scan.local.chat.return_value = ChatResult(ChatCompletionMessage(role='assistant', content=bad), TokenUsage(10, 2))
    with pytest.raises(PrivacyResponseInvalid) as error:
        await scan.guard._scan(scan.records, scan.scope, 0)
    assert error.value.reason == reason and error.value.correction_exhausted
    assert scan.local.chat.await_count == 3 and scan.charges == [12] * 3
    assert scan.guard.ledger.receipt(scan.key) is None
    assert scan.guard.ledger.mask('AmberFixtureOnly') == 'AmberFixtureOnly'
    assert 'FixturePrivateOnly' not in '\n'.join(logs)
    assert all('FixturePrivateOnly' not in encoded(call.args) for call in scan.local.chat.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('obstacle', ['deadline', 'call_limit', 'budget_busy'])
async def test_invalid_paid_response_cannot_reset_allowance_by_deferring(scan, monkeypatch, obstacle):
    async def chat(*args, **kwargs):
        if obstacle == 'deadline':
            monkeypatch.setattr(scan.guard, 'remaining_seconds', lambda: 0)
        elif obstacle == 'call_limit':
            monkeypatch.setattr(settings, 'PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT', 1)
        else:
            def busy(_):
                raise BudgetBusy()
            scan.guard.reserve = busy
        return response([{'value': 'FixtureNotSent', 'type': 'SECRET'}])
    scan.local.chat.side_effect = chat
    with pytest.raises(PrivacyResponseInvalid) as error:
        await scan.guard._scan(scan.records, scan.scope, 0)
    assert error.value.correction_exhausted and error.value.reason == 'value_not_in_input'
    assert scan.local.chat.await_count == 1 and scan.charges == [12]
    assert scan.guard.ledger.receipt(scan.key) is None


@pytest.mark.asyncio
async def test_split_parent_does_not_restart_exhausted_child(scan):
    records = scan.records + [{'field': 'source_excerpt', 'text': 'Second full synthetic field'}]
    scan.local.chat.side_effect = [
        ChatResult(ChatCompletionMessage(role='assistant', content='{"items":[]}'), TokenUsage(10, 2), finish_reason='length'),
        *[response([{'value': 'AbsentSyntheticValue', 'type': 'SECRET'}]) for _ in range(3)],
    ]
    with pytest.raises(PrivacyResponseInvalid):
        await scan.guard._scan(records, scan.scope, 0)
    assert scan.local.chat.await_count == 4 and scan.charges == [12] * 4
    assert scan.guard.ledger.receipt(scan.guard.ledger.digest('packet:v2:' + encoded(records))) == 'split'


@pytest.mark.asyncio
async def test_corrective_call_binds_latest_projection_after_reservation(scan):
    value = "LongSyntheticPriorCredential" * 4
    scan.records = [{"field": "source_excerpt", "text": "fixture " + value}]
    scan.key = scan.guard.ledger.digest("packet:v2:" + encoded(scan.records))
    original_reserve = scan.guard.reserve
    paid = 0
    def reserve(amount):
        nonlocal paid
        original_reserve(amount)
        if amount:
            paid += 1
            if paid == 2:
                scan.guard.ledger.remember([{'value': value, 'type': 'SECRET'}])
    scan.guard.reserve = reserve
    async def chat(messages, **kwargs):
        text = json.loads(messages[1]['content'])['source_excerpt'][0]
        if scan.local.chat.await_count == 1:
            assert value in text
        else:
            assert value not in text
        # Returning a stale original value must fail after the late discovery.
        return response([{'value': 'AbsentSyntheticValue' if scan.local.chat.await_count == 1 else value, 'type': 'SECRET'}])
    scan.local.chat.side_effect = chat
    with pytest.raises(PrivacyResponseInvalid):
        await scan.guard._scan(scan.records, scan.scope, 0)
    assert scan.local.chat.await_count == 3 and scan.charges == [12] * 3
    assert scan.guard.ledger.receipt(scan.key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('content', ['', 'source_excerpt', 'firstsecond', r'line\nquoted'])
async def test_reference_validator_never_fuzzy_matches_names_joined_fields_or_json_escapes(scan, content):
    records = [{'field': 'source_excerpt', 'text': s} for s in ['first', 'second', 'line\nquoted']]
    scan.local.chat.return_value = response([{'value': content, 'type': 'SECRET'}])
    with pytest.raises(PrivacyResponseInvalid):
        await scan.guard._scan(records, scan.scope, 0)
    assert scan.local.chat.await_count == 3


@pytest.mark.asyncio
async def test_exact_decoded_unicode_reference_is_accepted_once(scan):
    value = '合成"口令\\甲\n乙'
    records = [{'field': 'source_excerpt', 'text': 'before ' + value + ' after'}]
    scan.local.chat.return_value = response([{'value': value, 'type': 'SECRET'}])
    await scan.guard._scan(records, scan.scope, 0)
    assert scan.local.chat.await_count == 1 and scan.charges == [12]
    assert scan.guard.ledger.mask(value) != value


@pytest.mark.asyncio
async def test_refusal_is_not_a_correctable_protocol_response(scan):
    scan.local.chat.return_value = ChatResult(ChatCompletionMessage(role='assistant', content='{"items":[]}', refusal='synthetic refusal'), TokenUsage(10, 2))
    with pytest.raises(ValueError, match='未正常完成'):
        await scan.guard._scan(scan.records, scan.scope, 0)
    assert scan.local.chat.await_count == 1 and scan.charges == [12]
    assert scan.guard.ledger.receipt(scan.key) is None


def test_error_metadata_and_api_are_closed():
    from src.api.v1.brain import _error_code
    assert _error_code(str(PrivacyResponseInvalid('empty_value')), 'stage_failed') == 'privacy_response_invalid'
    for reason in ['FixturePrivateOnly', {}, None, 2]:
        with pytest.raises(ValueError):
            PrivacyResponseInvalid(reason)


def test_exhausted_scan_stops_worker_once_and_preserves_each_call_bill(scan, database, monkeypatch):
    from sqlalchemy import select
    from unittest.mock import MagicMock
    from src.core.project_brain.usage import BuildCallMeter
    from src.models.brain import BrainBuild, BrainModelCall, BrainUnit
    from src.tasks import brain_task
    from tests.unit.test_brain_execution_worker import seed

    trial = seed(database, 'privacy', 'privacy')
    monkeypatch.setattr(brain_task, '_retention', MagicMock())
    retry = MagicMock(side_effect=AssertionError('invalid response must not restart whole unit'))
    monkeypatch.setattr(brain_task.execute_brain_unit, 'retry', retry)
    dispatch = MagicMock()
    monkeypatch.setattr(brain_task, 'dispatch_pending', dispatch)
    scan.local.chat.return_value = response([{'value': 'AbsentSyntheticValue', 'type': 'SECRET'}])

    async def execute(unit, build, project, token):
        scan.guard.call_meter = BuildCallMeter(build.id, unit.id, token)
        await scan.guard._scan(scan.records, scan.scope, 0)

    monkeypatch.setattr(brain_task, '_execute', execute)
    assert brain_task.execute_brain_unit.run(trial.units[0]) == {'status': 'failed'}
    retry.assert_not_called()
    dispatch.assert_not_called()
    with database() as session:
        build = session.get(BrainBuild, trial.build_id)
        unit = session.get(BrainUnit, trial.units[0])
        calls = list(session.scalars(select(BrainModelCall).where(BrainModelCall.build_id == build.id)))
        assert build.status == unit.status == 'failed' and unit.attempts == 1
        assert 'failure_code=privacy_response_invalid' in build.error_message
        assert 'value_not_in_input' in build.error_message and 'AbsentSyntheticValue' not in build.error_message
        assert len(calls) == len({c.id for c in calls}) == 3
        assert sorted(c.attempt for c in calls) == [1, 2, 3]
        assert all(c.state == 'settled' for c in calls)
        assert build.total_prompt_tokens + build.total_completion_tokens == build.reserved_tokens == 36
