"""Deterministic compilation capacity failures must not repeat billed calls."""

from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from src.core.project_brain.compiler import CompilationLimitError
from src.core.project_brain.lifecycle import create_build
from src.models.brain import BrainBuild, BrainUnit
from src.tasks.brain_task import execute_brain_unit
from tests.unit.test_project_brain import database  # noqa: F401


def test_compilation_hard_limit_stops_first_attempt_preserving_spent_tokens(database, monkeypatch) -> None:
    with database() as session:
        build = create_build(session, 1, "main", "fixture/project", "a" * 40)
        build.total_prompt_tokens = 30
        build.total_completion_tokens = 20
        build.reserved_tokens = 50
        session.flush()
        current = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id))
        sibling = BrainUnit(build_id=build.id, key="waiting", kind="compile", status="pending")
        session.add(sibling)
        session.commit()
        unit_id, sibling_id = current.id, sibling.id
    monkeypatch.setattr("src.tasks.brain_task._execute", AsyncMock(side_effect=CompilationLimitError("知识单元硬上限不足")))
    retry = MagicMock()
    monkeypatch.setattr(execute_brain_unit, "retry", retry)
    assert execute_brain_unit.run(unit_id) == {"status": "failed"}
    retry.assert_not_called()
    with database() as session:
        stored = session.get(BrainBuild, build.id)
        assert stored.status == "failed" and stored.completed_at is not None
        assert stored.total_prompt_tokens + stored.total_completion_tokens == 50
        assert stored.reserved_tokens == 50
        assert session.get(BrainUnit, unit_id).attempts == 1
        assert session.get(BrainUnit, sibling_id).status == "failed"
        assert "CompilationLimitError" in stored.error_message
