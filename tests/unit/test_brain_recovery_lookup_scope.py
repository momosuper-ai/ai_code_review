"""Multi-parent receipt search stays bound to one current discovery revision."""

import pytest

from src.core.project_brain import recovery
from src.core.project_brain.compiler import encoded
from src.core.project_brain.recovery_privacy import FrozenPrivacyReceipts, RecoveryPrivacyError
from tests.unit.test_brain_recovery_guard import (
    allow_empty_synthetic_scan,
    guard_case,
    no_external_network,
)
from tests.unit.test_brain_recovery_privacy import COMPONENT, environment


def multi_parent_case(tmp_path, monkeypatch, partitions):
    """Real authenticated parents; only the database selector is stubbed."""
    test = guard_case(tmp_path / 'primary', monkeypatch)
    cases = [test.case] + [environment(tmp_path / f'other-{index}')
        for index in range(1, len(partitions))]
    parents = []
    for case, texts in zip(cases, partitions, strict=True):
        for text in texts:
            record = {'field': 'value', 'text': text}
            case['parent'].complete(case['parent'].digest('record:v2:' + encoded(record)))
        parents.append(FrozenPrivacyReceipts.freeze(case['scope'], **case['kwargs']))
    monkeypatch.setattr(recovery, 'load_recovery', lambda *args: (
        {'components': {'privacy': COMPONENT}}, parents))
    test.parents, test.parent_cases, test.lookups = parents, cases, []
    original = FrozenPrivacyReceipts.lookup_records

    def counted(parent, records, **kwargs):
        index = next(index for index, item in enumerate(parents) if item is parent)
        test.lookups.append((index, tuple(record['text'] for record in records)))
        result = original(parent, records, **kwargs)
        hook = getattr(test, 'after_lookup', None)
        if hook is not None:
            hook(index, records, result)
        return result

    monkeypatch.setattr(FrozenPrivacyReceipts, 'lookup_records', counted)
    return test


def bind(test):
    return recovery.bind_guard(test.guard, test.child, test.project, test.store)


@pytest.mark.asyncio
async def test_first_parent_full_hit_does_not_lookup_later_parent(tmp_path, monkeypatch):
    texts = ['ordinary alpha declaration', 'ordinary beta declaration']
    test = multi_parent_case(tmp_path, monkeypatch, [texts, texts, texts])
    guard = bind(test)
    try:
        assert await guard.clean(encoded(texts)) == encoded(texts)
        assert test.lookups == [(0, tuple(texts))]
        assert guard.recovery_receipt_hits == 2 and guard.calls == 0
        assert test.local.chat.await_count == 0 and not test.accounts
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_later_parents_only_receive_records_without_an_earlier_hit(tmp_path, monkeypatch):
    texts = ['ordinary alpha declaration', 'ordinary beta declaration', 'ordinary gamma declaration']
    test = multi_parent_case(tmp_path, monkeypatch, [[texts[0]], [texts[1]], [texts[2]], texts])
    guard = bind(test)
    try:
        assert await guard.clean(encoded(texts)) == encoded(texts)
        assert test.lookups == [(0, tuple(texts)), (1, tuple(texts[1:])), (2, (texts[2],))]
        assert guard.recovery_receipt_hits == 3 and guard.calls == 0
        assert test.local.chat.await_count == 0
    finally:
        await guard.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('replace_discovery_check', [False, True])
async def test_unused_later_parent_mutation_still_fails_global_refresh_fence(
    tmp_path, monkeypatch, replace_discovery_check,
):
    text = 'ordinary alpha declaration'
    test = multi_parent_case(tmp_path, monkeypatch, [[text], [text]])
    changed = False

    def mutate_unused_parent(index, records, result):
        nonlocal changed
        if index == 0 and not changed:
            changed = True
            test.parent_cases[1]['parent'].complete('f' * 64)

    test.after_lookup = mutate_unused_parent
    guard = bind(test)
    if replace_discovery_check:
        guard.discovery_check = lambda: None
    try:
        with pytest.raises(RecoveryPrivacyError, match='recovery_privacy_parent_changed'):
            await guard.clean(text)
        assert [index for index, _ in test.lookups] == [0]
        assert test.local.chat.await_count == 0 and not test.accounts
    finally:
        await guard.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation_cut', ['partial_parent', 'full_cover'])
async def test_current_revision_advance_discards_all_previously_collected_hits(
    tmp_path, monkeypatch, mutation_cut,
):
    texts = ['ordinary alpha declaration', 'ordinary beta declaration']
    partitions = [[texts[0]], [texts[1]]] if mutation_cut == 'partial_parent' else [texts, texts]
    test = multi_parent_case(tmp_path, monkeypatch, partitions)
    allow_empty_synthetic_scan(test)
    changed = False
    observed_external = []
    original_clean = test.guard.clean

    async def inspect_original_clean(text):
        observed_external.append(set(test.guard.ledger.external))
        return await original_clean(text)

    test.guard.clean = inspect_original_clean

    def advance_current_after_first_lookup(index, records, result):
        nonlocal changed
        if index == 0 and not changed:
            changed = True
            current = test.case['current']
            current.remember_policy([{'value': 'alpha', 'type': 'SECRET'}],
                [{'field': 'value', 'text': texts[0]}], kind='deterministic_rule',
                scope=current.digest('late-current-revision'), context=test.case['context'])

    test.after_lookup = advance_current_after_first_lookup
    guard = bind(test)
    try:
        safe = await guard.clean(encoded(texts))
        assert observed_external == [set()]
        assert guard.recovery_receipt_hits == 0
        assert test.local.chat.await_count >= 1
        assert 'alpha' not in safe
        assert 'ordinary beta declaration' in safe
        assert guard.ledger.external == set()
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_current_revision_advance_between_record_batches_discards_earlier_hits(tmp_path, monkeypatch):
    texts = [f'ordinary synthetic declaration number {index}' for index in range(129)]
    test = multi_parent_case(tmp_path, monkeypatch, [texts])
    allow_empty_synthetic_scan(test)
    changed = False
    observed_external = []
    original_clean = test.guard.clean

    async def inspect_original_clean(text):
        observed_external.append(set(test.guard.ledger.external))
        return await original_clean(text)

    test.guard.clean = inspect_original_clean

    def advance_after_first_batch(index, records, result):
        nonlocal changed
        if not changed:
            changed = True
            current = test.case['current']
            current.remember_policy([{'value': 'number 0', 'type': 'SECRET'}],
                [{'field': 'value', 'text': texts[0]}], kind='deterministic_rule',
                scope=current.digest('late-current-between-batches'), context=test.case['context'])

    test.after_lookup = advance_after_first_batch
    guard = bind(test)
    try:
        safe = await guard.clean(encoded(texts))
        assert observed_external == [set()]
        assert guard.recovery_receipt_hits == 0
        assert test.local.chat.await_count >= 1
        assert 'number 0' not in safe
    finally:
        await guard.close()
