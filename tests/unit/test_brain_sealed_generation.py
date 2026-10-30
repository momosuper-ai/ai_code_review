"""A closed privacy history stays closed and keeps its original model costs."""

from sqlalchemy import select

from src.core.project_brain.lifecycle import create_build
from src.models.brain import BrainProject, BrainUnit
from tests.unit.test_project_brain import database  # noqa: F401


def test_sealed_failure_requires_explicit_new_generation(database):
    with database() as session:
        old = create_build(session, 1, "main", "group/project", "a" * 40)
        old.status = "failed"
        old.error_message = "failure_code=privacy_seal_conflict"
        old.reserved_tokens = 700
        old.total_tokens = 500
        unit = session.scalar(select(BrainUnit).where(BrainUnit.build_id == old.id))
        unit.status, unit.payload = "failed", {"_unknown_tokens": 200}
        session.commit()

        project = session.get(BrainProject, old.project_id)
        automatic = create_build(session, 1, "main", "group/project", "a" * 40,
                                 observed_seq=project.event_seq)
        assert automatic.id == old.id and automatic.status == "failed"

        fresh = create_build(session, 1, "main", "group/project", "a" * 40)
        session.commit()
        assert fresh.id != old.id and fresh.generation == old.generation + 1
        assert fresh.brain_version > old.brain_version and fresh.status == "pending"
        assert old.status == "failed" and old.reserved_tokens == 700 and old.total_tokens == 500
        assert unit.status == "failed" and unit.payload == {"_unknown_tokens": 200}
        assert session.scalar(select(BrainUnit).where(BrainUnit.build_id == fresh.id)).kind == "scan"
        assert create_build(session, 1, "main", "group/project", "a" * 40).id == fresh.id


def test_ordinary_failure_still_reuses_completed_checkpoints(database):
    with database() as session:
        old = create_build(session, 1, "main", "group/project", "a" * 40)
        old.status, old.error_message = "failed", "failure_code=capacity_limit"
        session.commit()
        same = create_build(session, 1, "main", "group/project", "a" * 40)
        assert same.id == old.id and same.generation == old.generation
