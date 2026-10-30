"""A completed retry does not hide an outstanding older model response."""

from uuid import uuid4

import pytest

from src.core.project_brain.exceptions import ModelCallsIncomplete
from src.core.project_brain.lifecycle import create_build
from src.models.brain import BrainModelCall, BrainUnit
from src.tasks.brain_task import _assert_calls_complete
from tests.unit.test_project_brain import database  # noqa: F401


@pytest.mark.parametrize("fault", ["reserved", "unknown", "unit_live", "unit_unknown", "sibling_pending"])
def test_ready_rejects_incomplete_call_history_without_changing_costs(database, fault):
    with database() as session:
        build = create_build(session, 1, "main", "fixture/project", "a" * 40)
        scan = session.query(BrainUnit).filter_by(build_id=build.id).one()
        scan.kind = "privacy"
        scan.status = "completed"
        assembly = BrainUnit(build_id=build.id, kind="assemble", key="assemble", status="running")
        session.add(assembly)
        session.flush()
        call = BrainModelCall(id=str(uuid4()), build_id=build.id, unit_id=scan.id,
            lease_token=str(uuid4()), stage="privacy", role="privacy", purpose="initial",
            requested_model="synthetic", attempt=1, state="settled", reserved_tokens=100,
            prompt_tokens=20, completion_tokens=10)
        session.add(call)
        build.reserved_tokens, build.total_prompt_tokens, build.total_completion_tokens = 30, 20, 10
        session.flush()
        _assert_calls_complete(session, build.id, assembly.id)
        if fault in {"reserved", "unknown"}:
            call.state = fault
        elif fault == "sibling_pending":
            scan.status = "pending"
        else:
            scan.payload = {"_reserved_tokens" if fault == "unit_live" else "_unknown_tokens": 100}
        session.flush()
        with pytest.raises(ModelCallsIncomplete, match="^model_calls_incomplete$"):
            _assert_calls_complete(session, build.id, assembly.id)
        assert build.reserved_tokens == 30 and build.total_prompt_tokens == 20 and build.total_completion_tokens == 10
        assert scan.payload == ({"_reserved_tokens": 100} if fault == "unit_live" else
                                {"_unknown_tokens": 100} if fault == "unit_unknown" else {})
