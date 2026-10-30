"""The additive pause migration preserves old checkpoints and uncertain costs."""
import importlib.util
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import MetaData, create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError


def migration(filename):
    path = Path(__file__).parents[2] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONTROL = migration("010_brain_execution_control.py")


@pytest.fixture
def legacy():
    engine = create_engine("sqlite://")
    build_id, call_id = str(uuid4()), str(uuid4())
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            for filename in ("005_add_project_brain.py", "006_brain_automatic_maintenance.py",
                             "007_brain_model_usage.py", "008_brain_http_transport.py",
                             "009_brain_checkpoint_recovery.py"):
                migration(filename).upgrade()
        schema = MetaData()
        schema.reflect(connection)
        now = datetime(2026, 10, 26)
        connection.execute(schema.tables["brain_projects"].insert().values(id=1, gitlab_project_id=1,
            project_name="fixture/project", branch="main", auto_update=False, gitlab_sync=False,
            request_seq=1, next_version=2, created_at=now))
        connection.execute(schema.tables["brain_builds"].insert().values(id=build_id, project_id=1,
            source_commit="a" * 40, recipe_hash="b" * 64, mode="full", request_seq=1, brain_version=1,
            historical=True, status="failed", coverage={}, total_prompt_tokens=0,
            total_completion_tokens=0, total_cached_tokens=0, reserved_tokens=7,
            usage_estimated=False, created_at=now))
        connection.execute(schema.tables["brain_units"].insert().values(id=1, build_id=build_id,
            key="compile:fixture", kind="compile", input_hash="c" * 64,
            payload={"_reserved_tokens": 0, "_unknown_tokens": 7}, result={"checkpoint": "kept"},
            status="failed", attempts=1))
        connection.execute(schema.tables["brain_model_calls"].insert().values(id=call_id, build_id=build_id,
            unit_id=1, lease_token=str(uuid4()), stage="compile", role="privacy", purpose="semantic_output",
            requested_model="local_model", attempt=1, state="unknown", reserved_tokens=7,
            prompt_tokens=0, completion_tokens=0, cached_tokens=0, usage_estimated=False, created_at=now))
        old_columns = {name: list(schema.tables[name].columns.keys()) for name in (
            "brain_projects", "brain_builds", "brain_units", "brain_model_calls")}
    yield engine, build_id, old_columns
    engine.dispose()


def snapshot(legacy):
    engine, _, old_columns = legacy
    with engine.connect() as connection:
        schema = MetaData()
        schema.reflect(connection)
        return {name: [tuple(row) for row in connection.execute(select(
            *(schema.tables[name].c[column] for column in columns)).order_by(schema.tables[name].c.id))]
                for name, columns in old_columns.items()}


def run(engine, method):
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(CONTROL, method)()


def test_control_migration_backfills_running_without_changing_old_rows_or_unknown(legacy):
    engine, _, _ = legacy
    before = snapshot(legacy)
    run(engine, "upgrade")
    assert snapshot(legacy) == before
    with engine.connect() as connection:
        assert connection.execute(text("SELECT execution_state, pause_requested_at, paused_at, resumed_at FROM brain_builds")).all() == [
            ("running", None, None, None)]
    assert "ck_brain_build_execution_state" in {
        constraint["name"] for constraint in inspect(engine).get_check_constraints("brain_builds")}
    run(engine, "downgrade")
    assert snapshot(legacy) == before
    assert "execution_state" not in {column["name"] for column in inspect(engine).get_columns("brain_builds")}


def test_control_migration_rejects_unknown_execution_state(legacy):
    engine, _, _ = legacy
    run(engine, "upgrade")
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text("UPDATE brain_builds SET execution_state = 'invalid'"))


@pytest.mark.parametrize("state", ["pausing", "paused"])
def test_downgrade_cannot_erase_pause_intent(legacy, state):
    engine, _, _ = legacy
    before = snapshot(legacy)
    run(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(text("UPDATE brain_builds SET execution_state = :state"), {"state": state})
    with pytest.raises(RuntimeError, match="brain_control_downgrade_requires_no_pause_intent"):
        run(engine, "downgrade")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT execution_state FROM brain_builds")).scalar_one() == state
    assert snapshot(legacy) == before
