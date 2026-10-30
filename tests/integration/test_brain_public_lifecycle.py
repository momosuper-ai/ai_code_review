"""Real PostgreSQL gates for public-schema lifecycle; no model or GitLab calls."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.core.project_brain.exceptions import ModelCallsIncomplete
from src.core.project_brain.lifecycle import create_build
from src.models import SyncSession
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainUnit
from src.tasks.brain_task import _assert_calls_complete, _source_identity_create
from tests.integration.test_brain_postgres import pg_project  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get("BRAIN_LOCAL_TEST") != "1",
                              reason="仅在独立本机 PostgreSQL 测试栈执行")


@pytest.mark.parametrize("status, result, expected", [
    ("pending", {"source_identity_registered": True}, True),
    ("failed", {"source_identity_registered": True}, True),
    ("completed", {"source_identity_registered": False}, True),
    ("completed", {}, True),
    ("completed", None, True),
    ("completed", {"source_identity_registered": True}, False),
])
def test_pg_identity_registration_requires_completed_json_marker(pg_project, status, result, expected):
    with SyncSession() as session:
        build = create_build(session, pg_project, "main", "fixture/public-lifecycle", "a" * 40)
        session.add(BrainUnit(build_id=build.id, key="extract", kind="extract", status=status, result=result))
        session.commit()
    assert _source_identity_create(build, create=True) is expected
    assert _source_identity_create(build, create=False) is False


def test_pg_identity_registration_covers_later_builds_and_project_branches(pg_project):
    with SyncSession() as session:
        first = create_build(session, pg_project, "main", "fixture/public-lifecycle", "a" * 40)
        session.commit()
    assert _source_identity_create(first, create=True) is True
    with SyncSession() as session:
        session.add(BrainUnit(build_id=first.id, key="extract", kind="extract", status="completed",
                              result={"source_identity_registered": True}))
        session.commit()
        later = create_build(session, pg_project, "main", "fixture/public-lifecycle", "b" * 40)
        branch = create_build(session, pg_project, "release-fixture", "fixture/public-lifecycle", "c" * 40,
                              historical=True)
        session.commit()
        assert later.project_id == first.project_id and branch.project_id != first.project_id
    # The identity material is scoped to GitLab project, not the DB branch row.
    assert _source_identity_create(first, create=True) is False
    assert _source_identity_create(later, create=True) is False
    assert _source_identity_create(branch, create=True) is False


@pytest.mark.parametrize("fault", ["reserved", "unknown", "unit_live", "unit_unknown", "sibling_pending",
                                    "boolean_counter", "negative_counter"])
def test_pg_ready_gate_rejects_incomplete_calls_and_retains_accounting(pg_project, fault):
    with SyncSession() as session:
        build = create_build(session, pg_project, "main", "fixture/public-lifecycle", "d" * 40)
        scan = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        scan.kind = "privacy"
        scan.status = "completed"
        assembly = BrainUnit(build_id=build.id, key="assemble", kind="assemble", status="running")
        session.add(assembly)
        session.flush()
        session.add(BrainModelCall(id=str(uuid4()), build_id=build.id, unit_id=scan.id,
            lease_token=str(uuid4()), stage="privacy", role="privacy", purpose="generic",
            requested_model="offline-fixture", attempt=1, state="settled", reserved_tokens=100,
            prompt_tokens=20, completion_tokens=10, cached_tokens=5))
        build.total_prompt_tokens, build.total_completion_tokens, build.total_cached_tokens, build.reserved_tokens = 20, 10, 5, 30
        session.commit()
        build_id, scan_id, assembly_id = build.id, scan.id, assembly.id
    with SyncSession() as session:
        _assert_calls_complete(session, build_id, assembly_id)
        current, unit = session.get(BrainBuild, build_id), session.get(BrainUnit, scan_id)
        if fault in {"reserved", "unknown"}:
            session.add(BrainModelCall(id=str(uuid4()), build_id=build_id, unit_id=scan_id,
                lease_token=str(uuid4()), stage="privacy", role="privacy", purpose="generic",
                requested_model="offline-fixture", attempt=2, state=fault, reserved_tokens=100))
            current.reserved_tokens = 130
        elif fault == "sibling_pending":
            unit.status = "pending"
        elif fault in {"unit_live", "unit_unknown"}:
            unit.payload = {"_reserved_tokens" if fault == "unit_live" else "_unknown_tokens": 100}
            current.reserved_tokens = 130
        else:
            unit.payload = {"_unknown_tokens": True if fault == "boolean_counter" else -1}
        session.commit()
    with SyncSession() as session:
        before = session.get(BrainBuild, build_id)
        expected = (before.total_prompt_tokens, before.total_completion_tokens,
                    before.total_cached_tokens, before.reserved_tokens)
        payload = dict(session.get(BrainUnit, scan_id).payload)
        call_states = session.scalars(select(BrainModelCall.state).where(BrainModelCall.build_id == build_id)
                                      .order_by(BrainModelCall.id)).all()
        with pytest.raises(ModelCallsIncomplete, match="^model_calls_incomplete$"):
            _assert_calls_complete(session, build_id, assembly_id)
        session.commit()
    with SyncSession() as session:
        after = session.get(BrainBuild, build_id)
        assert (after.total_prompt_tokens, after.total_completion_tokens,
                after.total_cached_tokens, after.reserved_tokens) == expected
        assert session.get(BrainUnit, scan_id).payload == payload
        assert session.scalars(select(BrainModelCall.state).where(BrainModelCall.build_id == build_id)
                               .order_by(BrainModelCall.id)).all() == call_states


def test_pg_sealed_failure_explicit_generation_preserves_old_costs_and_unknown(pg_project):
    with SyncSession() as session:
        old = create_build(session, pg_project, "main", "fixture/public-lifecycle", "e" * 40)
        old.status, old.error_message = "failed", "failure_code=privacy_seal_conflict"
        old.total_prompt_tokens, old.total_completion_tokens = 100, 50
        old.total_cached_tokens, old.reserved_tokens = 40, 350
        unit = session.scalar(select(BrainUnit).where(BrainUnit.build_id == old.id))
        unit.status, unit.payload = "failed", {"_unknown_tokens": 200}
        old_call = BrainModelCall(id=str(uuid4()), build_id=old.id, unit_id=unit.id,
            lease_token=str(uuid4()), stage="privacy", role="privacy", purpose="generic",
            requested_model="offline-fixture", attempt=1, state="unknown", reserved_tokens=200)
        session.add(old_call)
        session.commit()
        old_id, old_version, old_generation, unit_id, call_id = old.id, old.brain_version, old.generation, unit.id, old_call.id
        project = session.get(BrainProject, old.project_id)
        automatic = create_build(session, pg_project, "main", "fixture/public-lifecycle", "e" * 40,
                                 observed_seq=project.event_seq)
        assert automatic.id == old_id and automatic.status == "failed"
        fresh = create_build(session, pg_project, "main", "fixture/public-lifecycle", "e" * 40)
        session.commit()
        fresh_id = fresh.id
    with SyncSession() as session:
        preserved, fresh = session.get(BrainBuild, old_id), session.get(BrainBuild, fresh_id)
        assert fresh.id != old_id and fresh.generation == old_generation + 1
        assert fresh.brain_version > old_version and fresh.status == "pending"
        assert preserved.status == "failed"
        assert (preserved.total_prompt_tokens, preserved.total_completion_tokens,
                preserved.total_cached_tokens, preserved.reserved_tokens) == (100, 50, 40, 350)
        assert session.get(BrainUnit, unit_id).payload == {"_unknown_tokens": 200}
        assert session.get(BrainModelCall, call_id).state == "unknown"
        assert session.get(BrainModelCall, call_id).reserved_tokens == 200
        assert session.scalar(select(BrainUnit).where(BrainUnit.build_id == fresh_id)).kind == "scan"
        duplicate = create_build(session, pg_project, "main", "fixture/public-lifecycle", "e" * 40)
        assert duplicate.id == fresh_id


def test_pg_ordinary_failed_generation_keeps_the_existing_checkpoint(pg_project):
    with SyncSession() as session:
        old = create_build(session, pg_project, "main", "fixture/public-lifecycle", "f" * 40)
        old.status, old.error_message = "failed", "failure_code=capacity_limit"
        unit = session.scalar(select(BrainUnit).where(BrainUnit.build_id == old.id))
        unit.status, unit.result = "completed", {"fingerprint": "offline-fixture"}
        session.commit()
        identity = old.id, old.generation
    with SyncSession() as session:
        same = create_build(session, pg_project, "main", "fixture/public-lifecycle", "f" * 40)
        assert (same.id, same.generation) == identity
        assert session.scalar(select(BrainUnit).where(BrainUnit.build_id == same.id)).result == {"fingerprint": "offline-fixture"}
