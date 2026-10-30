"""Recovery lineage is additive and cannot discard old uncertain charges."""
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainRecovery, BrainUnit


def migration(filename):
    path = Path(__file__).parents[2] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RECOVERY_MIGRATION = migration("009_brain_checkpoint_recovery.py")


@pytest.fixture
def storage():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enable_constraints(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            for filename in (
                "005_add_project_brain.py", "006_brain_automatic_maintenance.py",
                "007_brain_model_usage.py", "008_brain_http_transport.py",
                "010_brain_execution_control.py",
                "011_brain_diagnostics.py",
            ):
                migration(filename).upgrade()
    with Session(engine) as session:
        project = BrainProject(gitlab_project_id=1, project_name="fixture/project", branch="main")
        session.add(project)
        session.flush()
        parent = BrainBuild(project_id=project.id, source_commit="a" * 40, recipe_hash="b" * 64,
                            request_seq=1, brain_version=1, status="failed", reserved_tokens=18,
                            total_prompt_tokens=10, total_completion_tokens=1)
        child = BrainBuild(project_id=project.id, source_commit="a" * 40, recipe_hash="c" * 64,
                           request_seq=2, brain_version=2)
        session.add_all([parent, child])
        session.flush()
        unit = BrainUnit(build_id=parent.id, key="compile:1", kind="compile", status="failed",
                         payload={"_reserved_tokens": 0, "_unknown_tokens": 7})
        session.add(unit)
        session.flush()
        for state, bound, prompt, completion in (("settled", 11, 10, 1), ("unknown", 7, 0, 0)):
            session.add(BrainModelCall(id=str(uuid4()), build_id=parent.id, unit_id=unit.id,
                lease_token=str(uuid4()), stage="compile", role="privacy", purpose="semantic_output",
                requested_model="local_model", attempt=1, state=state, reserved_tokens=bound,
                prompt_tokens=prompt, completion_tokens=completion))
        session.commit()
        identities = parent.id, child.id
    yield engine, identities
    engine.dispose()


def run_migration(engine, direction):
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(RECOVERY_MIGRATION, direction)()


def ledger_snapshot(engine):
    # Check every existing machine/accounting field, including unknown calls and
    # payload reservation counters, rather than only aggregate Token totals.
    with engine.connect() as connection:
        return {
            name: [tuple(row) for row in connection.execute(text(f"SELECT * FROM {name} ORDER BY id"))]
            for name in ("brain_projects", "brain_builds", "brain_units", "brain_model_calls")
        }


def recovery(parent_id, child_id, **changes):
    values = dict(parent_build_id=parent_id, child_build_id=child_id, plan_version="recovery/1",
                  compatibility_hash="d" * 64,
                  plan={"version": 1, "unit_ids": [], "source_commit": "a" * 40})
    values.update(changes)
    return BrainRecovery(**values)


def test_migration_is_additive_and_empty_downgrade_preserves_old_ledgers(storage):
    engine, _ = storage
    before = ledger_snapshot(engine)
    run_migration(engine, "upgrade")
    assert ledger_snapshot(engine) == before
    run_migration(engine, "downgrade")
    assert "brain_recoveries" not in inspect(engine).get_table_names()
    assert ledger_snapshot(engine) == before


def test_migration_matches_body_free_model_columns_indexes_and_foreign_keys(storage):
    engine, _ = storage
    run_migration(engine, "upgrade")
    schema = inspect(engine)
    columns = {column["name"]: column for column in schema.get_columns("brain_recoveries")}
    assert set(columns) == set(BrainRecovery.__table__.columns.keys())
    assert set(columns) == {"child_build_id", "parent_build_id", "status", "plan_version",
                            "compatibility_hash", "manifest_hash", "plan", "imports", "error_code",
                            "created_at", "frozen_at", "completed_at"}
    for name, column in columns.items():
        model = BrainRecovery.__table__.columns[name]
        assert column["nullable"] == model.nullable
        if hasattr(model.type, "length"):
            assert column["type"].length == model.type.length
    assert schema.get_pk_constraint("brain_recoveries")["constrained_columns"] == ["child_build_id"]
    foreign_keys = schema.get_foreign_keys("brain_recoveries")
    assert {key["constrained_columns"][0] for key in foreign_keys} == {"child_build_id", "parent_build_id"}
    assert all(key["referred_table"] == "brain_builds" and key["referred_columns"] == ["id"]
               and key["options"]["ondelete"] == "RESTRICT" for key in foreign_keys)
    assert {index["name"] for index in schema.get_indexes("brain_recoveries")} == {
        "ix_brain_recoveries_parent_build_id", "ix_brain_recoveries_status"}
    assert {constraint["name"] for constraint in schema.get_check_constraints("brain_recoveries")} == {
        "ck_brain_recovery_distinct_builds"}


def test_orm_defaults_are_pending_and_utc_without_rewriting_parent(storage):
    engine, (parent, child) = storage
    run_migration(engine, "upgrade")
    before = ledger_snapshot(engine)
    with Session(engine) as session:
        session.add(recovery(parent, child))
        session.commit()
        stored = session.get(BrainRecovery, child)
        assert stored.status == "pending"
        assert stored.created_at is not None and stored.created_at.tzinfo is None
        assert stored.frozen_at is stored.completed_at is stored.manifest_hash is stored.error_code is None
        assert stored.plan["source_commit"] == "a" * 40
        assert stored.imports == {}
    assert ledger_snapshot(engine) == before


def test_nonempty_history_cannot_be_downgraded_or_partially_dropped(storage):
    engine, (parent, child) = storage
    run_migration(engine, "upgrade")
    with Session(engine) as session:
        session.add(recovery(parent, child, status="completed", manifest_hash="e" * 64))
        session.commit()
    before = ledger_snapshot(engine)
    with pytest.raises(RuntimeError, match="^brain_recovery_downgrade_requires_empty_history$"):
        run_migration(engine, "downgrade")
    schema = inspect(engine)
    assert "brain_recoveries" in schema.get_table_names()
    assert len(schema.get_indexes("brain_recoveries")) == 2
    with Session(engine) as session:
        assert session.get(BrainRecovery, child).manifest_hash == "e" * 64
    assert ledger_snapshot(engine) == before


@pytest.mark.parametrize("side", ["parent", "child"])
def test_recovery_cannot_reference_a_missing_build(storage, side):
    engine, (parent, child) = storage
    run_migration(engine, "upgrade")
    with Session(engine) as session:
        session.add(recovery(str(uuid4()) if side == "parent" else parent,
                             str(uuid4()) if side == "child" else child))
        with pytest.raises(IntegrityError):
            session.commit()


def test_child_can_have_only_one_recovery_plan(storage):
    engine, (parent, child) = storage
    run_migration(engine, "upgrade")
    with Session(engine) as session:
        session.add(recovery(parent, child))
        session.commit()
    with Session(engine) as session:
        session.add(recovery(parent, child, compatibility_hash="f" * 64))
        with pytest.raises(IntegrityError):
            session.commit()


def test_self_inheritance_is_rejected_by_database(storage):
    engine, (parent, _) = storage
    run_migration(engine, "upgrade")
    with Session(engine) as session:
        session.add(recovery(parent, parent))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize("side", ["parent", "child"])
def test_retention_cannot_delete_either_build_while_lineage_exists(storage, side):
    engine, (parent, child) = storage
    run_migration(engine, "upgrade")
    with Session(engine) as session:
        # The source fixture parent has units/calls whose own foreign keys would
        # also block deletion. Use an empty parent so this tests recovery lineage
        # specifically, rather than accidentally relying on unrelated records.
        empty_parent = BrainBuild(project_id=session.get(BrainBuild, parent).project_id,
                                  source_commit="a" * 40, recipe_hash="d" * 64,
                                  request_seq=3, brain_version=3, status="failed")
        session.add(empty_parent)
        session.flush()
        parent = empty_parent.id
        session.add(recovery(parent, child))
        session.commit()
    before = ledger_snapshot(engine)
    with Session(engine) as session:
        with pytest.raises(IntegrityError):
            session.execute(text("DELETE FROM brain_builds WHERE id = :id"),
                            {"id": parent if side == "parent" else child})
            session.commit()
    with Session(engine) as session:
        assert session.get(BrainRecovery, child) is not None
    assert ledger_snapshot(engine) == before


def test_migration_revision_chain_is_explicit():
    assert RECOVERY_MIGRATION.revision == "009_brain_recovery"
    assert RECOVERY_MIGRATION.down_revision == "008_brain_http"
