"""A discovered path collision stops paid work immediately and fail-closed."""

from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.config import settings
from src.core.project_brain.lifecycle import create_build, recipe_hash
from src.core.project_brain.exceptions import PrivacyCheckpointMissing, PrivacyIdentityConflict
from src.core.project_brain.privacy_identity import SourceIdentityGuard
from src.core.project_brain.privacy import LedgerPrivacyFilter, PrivacyLedger
from src.core.project_brain.schema import Fact, FactBundle, SourceRef
from src.core.privacy_filter import PrivacyFilter
from src.core.privacy_rules import SensitivePattern
import re
from src.models.brain import BrainBuild, BrainUnit, utcnow
from src.tasks import brain_task
from tests.unit.test_project_brain import database  # noqa: F401


def identity_bundle(value="synthetic_backend"):
    path = f"app/{value}.py"
    ref = SourceRef(id="evidence:1", commit_sha="a" * 40, path=path,
                    blob_sha="b" * 40, line_start=1, line_end=2, symbol_id="fact:1")
    return FactBundle(source_commit="a" * 40, files={path: {"available": True}},
                      modules={"module:1": "app"}, source_refs=[ref],
                      facts=[Fact(id="fact:1", kind="symbol", name="run", module_id="module:1",
                                  source_ref_ids=[ref.id], extractor="synthetic")],
                      relations=[], source_fingerprint="f" * 64, observations={path: {}})


@pytest.mark.asyncio
async def test_worker_first_discovery_stops_before_a_second_privacy_call(tmp_path, monkeypatch):
    """The old privacy unit returned success and found this only at finalize."""
    value = "synthetic_backend"
    bundle = identity_bundle(value)
    ledger = PrivacyLedger(tmp_path / "private")
    calls = []
    guard = SimpleNamespace(ledger=ledger, filter=LedgerPrivacyFilter(ledger),
                            calls=0, cache_hits=0, close=AsyncMock())

    async def clean(_):
        calls.append("privacy_call")
        guard.calls += 1
        ledger.remember([{"value": value, "type": "SECRET"}])
        hook = getattr(guard, "discovery_check", None)
        if callable(hook):
            hook()
        calls.append("second_privacy_call")
        guard.calls += 1
        return "[]"

    guard.clean = clean
    extractor = SimpleNamespace(extract=lambda: bundle, reused_files=0)
    build = SimpleNamespace(id=str(uuid4()), source_commit="a" * 40,
                            recipe_hash=recipe_hash(), base_build_id=None, mode="full")
    unit = SimpleNamespace(kind="privacy", payload={"start": 0, "end": 128})
    monkeypatch.setattr(brain_task, "_EXTRACTION_CACHE", None)
    monkeypatch.setattr(brain_task, "_guard", lambda *args: guard)
    monkeypatch.setattr(brain_task, "FactExtractor", lambda *args: extractor)
    monkeypatch.setattr(brain_task, "read_snapshot", lambda *args: SimpleNamespace(commit_sha="a" * 40))
    with pytest.raises(PrivacyIdentityConflict, match="source_identity_privacy_conflict"):
        await brain_task._execute(unit, build, SimpleNamespace(), "lease")
    assert calls == ["privacy_call"]
    assert guard.calls == 1
    guard.close.assert_awaited_once()


@pytest.mark.parametrize("pool", ["files", "modules", "source_refs", "observations"])
def test_every_identity_pool_has_no_directory_or_identifier_exemption(pool):
    value = "synthetic_backend"
    bundle = identity_bundle("safe_backend")
    collision = f"unsupported/{value}/asset.bin"
    if pool == "files":
        bundle.files[collision] = {"available": False}
    elif pool == "modules":
        bundle.modules["module:other"] = collision
    elif pool == "source_refs":
        bundle.source_refs[0].path = collision
    else:
        bundle.observations[collision] = {}
    privacy = SimpleNamespace(sanitize_text=lambda text: text.replace(value, "<<SECRET_1>>"))
    with pytest.raises(PrivacyIdentityConflict) as caught:
        SourceIdentityGuard(bundle, privacy).check()
    assert caught.value.counts == {"files": int(pool == "files"), "modules": int(pool == "modules"),
        "source_refs": int(pool == "source_refs"), "observations": int(pool == "observations"),
        "unique_changed": 1}
    assert value not in str(caught.value) and collision not in str(caught.value)


def test_duplicate_references_are_counted_but_distinct_strings_scanned_once():
    value = "synthetic_backend"
    bundle = identity_bundle(value)
    bundle.modules["module:other"] = bundle.source_refs[0].path
    bundle.source_refs.append(bundle.source_refs[0].model_copy(update={"id": "evidence:2"}))
    calls = []
    def clean(text):
        calls.append(text)
        return text.replace(value, "<<SECRET_1>>")
    with pytest.raises(PrivacyIdentityConflict) as caught:
        SourceIdentityGuard(bundle, SimpleNamespace(sanitize_text=clean)).check()
    assert caught.value.counts == {"files": 1, "modules": 1, "source_refs": 2,
                                   "observations": 1, "unique_changed": 1}
    assert len(calls) == len(set(calls)) == 2


def test_same_count_sibling_fingerprint_replacement_is_detected(tmp_path):
    value = "synthetic_backend"
    first, sibling = PrivacyLedger(tmp_path), PrivacyLedger(tmp_path)
    sibling.remember([{"value": "unrelated_fixture", "type": "SECRET"}])
    identity = SourceIdentityGuard(identity_bundle(value), LedgerPrivacyFilter(first))
    identity.check()
    assert len(first.entries) == 1
    # Simulate a valid equal-sized checkpoint change from a sibling process.
    replacement = {sibling.digest("value:" + value): [len(value), "SECRET"]}
    sibling._write(sibling.index, replacement)
    with pytest.raises(PrivacyIdentityConflict):
        identity.check()
    assert len(first.entries) == 1


def test_same_count_dynamic_rule_replacement_and_new_paths_are_detected():
    value = "synthetic_backend"
    bundle = identity_bundle(value)
    privacy = PrivacyFilter(local_llm=None)
    privacy._dynamic_values = {"unrelated_fixture": "SECRET"}
    identity = SourceIdentityGuard(bundle, privacy)
    identity.check()
    privacy._dynamic_values = {value: "SECRET"}
    with pytest.raises(PrivacyIdentityConflict):
        identity.check()
    other = identity_bundle("other_backend")
    identity = SourceIdentityGuard(other, privacy)
    identity.check()
    other.files[f"private/{value}.bin"] = {"available": False}
    with pytest.raises(PrivacyIdentityConflict):
        identity.check()


def test_success_reuse_is_unit_local_and_checks_complete_filter_state(tmp_path):
    bundle = identity_bundle("safe_backend")
    ledger = PrivacyLedger(tmp_path)
    privacy = LedgerPrivacyFilter(ledger)
    identity = SourceIdentityGuard(bundle, privacy)
    changed = MagicMock(wraps=identity._changed)
    identity._changed = changed
    identity.check()
    assert identity._last_success is not None
    identity.check()
    assert changed.call_count == 1
    # Mapping is part of the rule/filter state, even when it refers to a value
    # outside the identity pool. A same-object mapping cannot bypass recheck.
    privacy._mapping.get_or_create("other_fixture", "SECRET")
    identity.check()
    assert changed.call_count == 2
    privacy._custom_patterns.append(SensitivePattern("new", "SECRET", re.compile("safe_backend")))
    with pytest.raises(PrivacyIdentityConflict):
        identity.check()
    assert changed.call_count == 3
    with pytest.raises(PrivacyIdentityConflict):
        identity.check()
    assert changed.call_count == 4 and identity._last_success is None
    assert SourceIdentityGuard(bundle, LedgerPrivacyFilter(ledger))._last_success is None


@pytest.mark.parametrize("kind", ["unknown_type", "unknown_state", "unknown_implementation"])
def test_unrecognized_filter_or_state_never_reuses_a_success(kind, monkeypatch):
    bundle = identity_bundle("safe_backend")
    if kind == "unknown_type":
        privacy = SimpleNamespace(sanitize_text=lambda value: value)
    else:
        privacy = PrivacyFilter(local_llm=None)
        if kind == "unknown_state":
            privacy.future_rule = "unknown"
        else:
            monkeypatch.setattr(PrivacyFilter, "sanitize_text", lambda self, value: value)
    identity = SourceIdentityGuard(bundle, privacy)
    changed = MagicMock(wraps=identity._changed)
    identity._changed = changed
    identity.check()
    identity.check()
    assert changed.call_count == 2 and identity._last_success is None


def test_in_place_sanitizer_implementation_change_invalidates_success(monkeypatch):
    identity = SourceIdentityGuard(identity_bundle("safe_backend"), PrivacyFilter(local_llm=None))
    identity.check()
    identity.check()
    def replacement(self, value):
        return value.replace("safe_backend", "<<SECRET_1>>")
    # A function object's identity alone is insufficient: its implementation
    # can change without replacing the method object or the filter instance.
    monkeypatch.setattr(PrivacyFilter.sanitize_text, "__code__", replacement.__code__)
    with pytest.raises(PrivacyIdentityConflict):
        identity.check()
    assert identity._last_success is None


@pytest.mark.parametrize("pool", ["files", "modules", "source_refs", "observations"])
def test_success_cache_covers_full_identity_pool_content(pool):
    bundle = identity_bundle("safe_backend")
    privacy = PrivacyFilter(local_llm=None)
    privacy._dynamic_values["newly_sensitive"] = "SECRET"
    identity = SourceIdentityGuard(bundle, privacy)
    identity.check()
    identity.check()
    value = "newly_sensitive"
    if pool == "files":
        bundle.files[value] = {"available": False}
    elif pool == "modules":
        bundle.modules["module:new"] = value
    elif pool == "source_refs":
        bundle.source_refs[0].path = value
    else:
        bundle.observations[value] = {}
    with pytest.raises(PrivacyIdentityConflict):
        identity.check()


def test_identity_conflict_is_not_a_json_format_error():
    error = PrivacyIdentityConflict({"files": 1})
    assert not isinstance(error, ValueError)
    assert str(error) == "source_identity_privacy_conflict"
    for counts in [{"untrusted-private-key": 1}, {"files": "private-value"}, {"files": True}, {"files": -1}]:
        with pytest.raises(ValueError, match="计数无效") as caught:
            PrivacyIdentityConflict(counts)
        assert "untrusted-private-key" not in str(caught.value)
        assert "private-value" not in str(caught.value)


def test_worker_guard_passes_only_numeric_identity_and_stage_context(monkeypatch, tmp_path):
    sentinel = object()
    factory = MagicMock(return_value=sentinel)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(brain_task, "CompilationGuard", factory)
    unit = BrainUnit(id=42, build_id=str(uuid4()), kind="compile")
    PrivacyLedger(brain_task.ArtifactStore().directory(unit.build_id, "work"))
    assert brain_task._guard(unit, "lease") is sentinel
    assert factory.call_args.kwargs["discovery_context"] == {
        "build_id": unit.build_id, "unit_id": 42, "stage": "compile"}


@pytest.mark.parametrize("exception_type", [PrivacyIdentityConflict, PrivacyCheckpointMissing])
def test_terminal_identity_conflict_preserves_spent_and_unknown_accounting(database, monkeypatch, exception_type):
    with database() as session:
        build = create_build(session, 1, "main", "fixture/project", "a" * 40)
        build.total_prompt_tokens, build.total_completion_tokens = 30, 20
        build.reserved_tokens = 140
        session.flush()
        unit = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        unit.payload = {"_reserved_tokens": 30}
        sibling = BrainUnit(build_id=build.id, key="sibling", kind="privacy", status="running",
            lease_token="sibling-token", lease_until=utcnow() + timedelta(seconds=120),
            payload={"_reserved_tokens": 40})
        session.add(sibling)
        session.commit()
        unit_id, sibling_id, build_id = unit.id, sibling.id, build.id
    error = (exception_type({"files": 1, "source_refs": 3, "unique_changed": 1})
             if exception_type is PrivacyIdentityConflict else exception_type())
    async def fail_after_admission(current, active, project, token):
        # Admission rejects pre-existing uncertainty. This test exercises a
        # discovery arriving after the unit was already admitted, and keeps
        # the original terminal privacy-error accounting assertions intact.
        with database() as session:
            row = session.get(BrainUnit, current.id)
            row.payload = {**row.payload, "_unknown_tokens": 20}
            session.commit()
        raise error

    monkeypatch.setattr(brain_task, "_execute", AsyncMock(side_effect=fail_after_admission))
    retry = MagicMock()
    monkeypatch.setattr(brain_task.execute_brain_unit, "retry", retry)
    assert brain_task.execute_brain_unit.run(unit_id) == {"status": "failed"}
    retry.assert_not_called()
    with database() as session:
        stored = session.get(BrainBuild, build_id)
        current, other = session.get(BrainUnit, unit_id), session.get(BrainUnit, sibling_id)
        assert stored.status == "failed" and stored.completed_at is not None
        assert stored.total_prompt_tokens + stored.total_completion_tokens == 50
        assert stored.reserved_tokens == 140
        assert current.attempts == 1 and current.status == other.status == "failed"
        assert current.payload == {"_reserved_tokens": 0, "_unknown_tokens": 50}
        assert other.payload == {"_reserved_tokens": 0, "_unknown_tokens": 40}
        assert current.lease_token is other.lease_token is None
        if exception_type is PrivacyIdentityConflict:
            assert current.error_message == (
                "PrivacyIdentityConflict：源码身份与隐私过滤冲突，构建已停止；"
                "failure_code=source_identity_privacy_conflict")
        else:
            assert current.error_message == (
                "PrivacyCheckpointMissing：知识隐私检查点缺失或损坏，构建已停止；"
                "failure_code=privacy_checkpoint_missing")


@pytest.mark.parametrize("stage", ["privacy", "finalize", "compile", "module_summary", "project_summary", "assemble"])
@pytest.mark.parametrize("missing", ["directory", "key", "invalid_key"])
def test_non_extract_never_initializes_a_missing_private_checkpoint(stage, missing, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path))
    unit = BrainUnit(id=42, build_id=str(uuid4()), kind=stage)
    root = brain_task.ArtifactStore().directory(unit.build_id, "work") / "privacy"
    if missing != "directory":
        root.mkdir(parents=True)
        if missing == "invalid_key":
            (root / "key").write_bytes(b"short")
    existing_files = set(tmp_path.rglob("*"))
    with pytest.raises(PrivacyCheckpointMissing, match="privacy_checkpoint_missing"):
        brain_task._guard(unit, "lease")
    assert set(tmp_path.rglob("*")) == existing_files
    assert not (root / "index.lock").exists()


def test_extract_can_establish_initial_key_but_later_empty_index_is_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path))
    factory = MagicMock(return_value=object())
    monkeypatch.setattr(brain_task, "CompilationGuard", factory)
    build_id = str(uuid4())
    unit = BrainUnit(id=42, build_id=build_id, kind="extract")
    brain_task._guard(unit, "lease")
    root = brain_task.ArtifactStore().directory(build_id, "work") / "privacy"
    key = (root / "key").read_bytes()
    assert len(key) == 32 and not (root / "fingerprints.json").exists()
    unit.kind = "privacy"
    brain_task._guard(unit, "lease")
    assert (root / "key").read_bytes() == key
    assert not (root / "fingerprints.json").exists()

