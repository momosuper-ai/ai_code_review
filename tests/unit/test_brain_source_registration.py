"""Stable project identity registration survives new builds and process caches."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import PureWindowsPath
from uuid import uuid4

import pytest

from src.config import settings
from src.core.project_brain.exceptions import PrivacyCheckpointMissing
from src.core.project_brain.lifecycle import create_build
from src.core.project_brain.source_catalog import _within_resolved, identity_key
from src.core.project_brain.privacy import PrivacyLedger
from src.models.brain import BrainProject, BrainUnit
from src.tasks import brain_task
from tests.unit.test_project_brain import database, repository, snapshot  # noqa: F401


def test_concurrent_first_identity_registration_has_one_key(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: identity_key(tmp_path, 480, create=True), range(16)))
    assert len(set(values)) == 1
    assert identity_key(tmp_path, 480, create=False) == values[0]


@pytest.mark.parametrize("child,root,expected", [
    ("\\\\?\\D:\\workspace\\private\\identity", "D:\\workspace\\private", True),
    ("D:\\workspace\\private\\identity", "\\\\?\\D:\\workspace\\private", True),
    ("\\\\?\\D:\\workspace\\private-other\\identity", "D:\\workspace\\private", False),
    ("\\\\?\\E:\\workspace\\private\\identity", "D:\\workspace\\private", False),
    ("\\\\?\\UNC\\server\\share\\private\\identity", "\\\\server\\share\\private", True),
    ("\\\\?\\UNC\\other\\share\\private\\identity", "\\\\server\\share\\private", False),
    ("\\\\.\\D:\\workspace\\private\\identity", "D:\\workspace\\private", False),
])
def test_windows_namespace_equivalence_preserves_containment(child, root, expected):
    assert _within_resolved(PureWindowsPath(child), PureWindowsPath(root)) is expected


def test_losing_entire_registered_identity_directory_cannot_rotate_key(database, snapshot, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_BASE_DIR", str(tmp_path / "public"))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path / "private"))
    with database() as session:
        build = create_build(session, 480, "main", "fixture/project", snapshot.commit_sha)
        project = session.get(BrainProject, build.project_id)
        session.commit()
    original = brain_task._source_catalog(build, project, snapshot,
        create=brain_task._source_identity_create(build, create=True))
    with database() as session:
        session.add(BrainUnit(build_id=build.id, key="extract", kind="extract", status="completed",
                              result={"source_identity_registered": True}))
        session.commit()
    directory = tmp_path / "private/source-identities/480/1"
    assert directory.resolve().is_relative_to(tmp_path.resolve())
    directory.rename(directory.with_name("retained-original"))
    # Even the same prefork's in-memory catalog must not mask lost disk history.
    assert original is not None
    assert not brain_task._source_identity_create(build, create=True)
    with pytest.raises(PrivacyCheckpointMissing):
        brain_task._source_catalog(build, project, snapshot,
            create=brain_task._source_identity_create(build, create=True))
    assert not directory.exists()


@pytest.mark.parametrize("parent_project,expected", [(480, 1), (481, 0)])
def test_worker_history_uses_same_gitlab_project_across_branches(database, tmp_path, monkeypatch, parent_project, expected):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_BASE_DIR", str(tmp_path / "public"))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path / "private"))
    with database() as session:
        parent = create_build(session, parent_project, "other-branch", "fixture/project", "a" * 40)
        child = create_build(session, 480, "main", "fixture/project", "b" * 40)
        parent.status = "failed"
        session.add(BrainUnit(build_id=parent.id, key="extract", kind="extract", status="completed", result={}))
        unit = BrainUnit(build_id=child.id, key="extract", kind="extract", status="running")
        session.add(unit)
        session.commit()
    identity_key(tmp_path / "private", 480, create=True)
    ledger = PrivacyLedger(tmp_path / "private/work" / parent.id)
    secret = "synthetic_prior_credential"
    ledger.remember_scan([{"value": secret, "type": "SECRET"}], [{"field": "text", "text": secret}],
        call_id=str(uuid4()), scope=ledger.digest("input"), packet=ledger.digest("packet"),
        context={"build_id": parent.id, "unit_id": 1, "stage": "privacy"})
    history = brain_task._history_for(unit, 480)
    assert len(history.parents) == expected
    with database() as session:
        row = session.get(BrainUnit, unit.id)
        row.status = "completed"
        row.result = {"privacy_history_digest": history.digest}
        next_unit = BrainUnit(build_id=child.id, key="privacy:0", kind="privacy", status="pending")
        session.add(next_unit)
        session.commit()
    assert brain_task._history_for(next_unit, 480).digest == history.digest
    history.path.unlink()
    with pytest.raises(PrivacyCheckpointMissing):
        brain_task._history_for(next_unit, 480)


@pytest.mark.parametrize("schema2", [False, True])
def test_worker_old_ready_missing_audit_is_explicit_legacy_gap_or_error(database, tmp_path, monkeypatch, schema2):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_BASE_DIR", str(tmp_path / "public"))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path / "private"))
    with database() as session:
        old = create_build(session, 480, "main", "fixture/project", "a" * 40)
        old.status, old.coverage = "ready", {"public_schema_version": "2.0"} if schema2 else {}
        session.add(BrainUnit(build_id=old.id, key="extract", kind="extract", status="completed", result={}))
        child = create_build(session, 480, "next", "fixture/project", "b" * 40)
        unit = BrainUnit(build_id=child.id, key="extract", kind="extract", status="running")
        session.add(unit)
        session.commit()
    identity_key(tmp_path / "private", 480, create=True)
    if schema2:
        with pytest.raises(PrivacyCheckpointMissing):
            brain_task._history_for(unit, 480)
    else:
        history = brain_task._history_for(unit, 480)
        assert not history.parents and history.legacy_unavailable == 1
