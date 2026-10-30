"""HTTP closure changes billing certainty without creating privacy evidence."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.core.project_brain.exceptions import ModelCallsIncomplete, ModelTransportFailed, ModelWorkAdmissionDenied
from src.core.project_brain.usage import BuildCallMeter, CallSpec
from src.models.brain import BrainBuild, BrainModelCall, BrainUnit
from src.tasks.brain_task import _assert_privacy_calls_settled, _model_usage_snapshot, execute_brain_unit
from tests.unit.test_brain_http_accounting import completed_group
from tests.unit.test_brain_unresolved_transport_task import work
from tests.unit.test_project_brain import database  # noqa: F401


def test_ready_keeps_closed_cost_but_only_success_can_supply_privacy_origins(database):
    unit, build, _, _, error, success = completed_group(database)
    with database() as session:
        session.get(BrainUnit, unit.id).status = "completed"
        assembly = BrainUnit(build_id=build.id, key="assemble", kind="assemble", status="running")
        session.add(assembly)
        session.flush()
        data = _model_usage_snapshot(session, build.id, assembly.id)
        assert data == {"model_calls_settled": 1, "model_calls_closed_http_errors": 1,
                        "model_usage_unknown_bound_tokens": 100, "model_usage_settled_tokens": 22,
                        "model_usage_budget_occupied_tokens": 122, "model_usage_financially_exact": False}
        metadata = {"context": {"build_id": build.id, "unit_id": unit.id, "stage": unit.kind}}
        _assert_privacy_calls_settled(session, build.id, SimpleNamespace(origins={"calls": {success: metadata}}))
        with pytest.raises(ModelCallsIncomplete):
            _assert_privacy_calls_settled(session, build.id, SimpleNamespace(origins={"calls": {error: metadata}}))


@pytest.mark.parametrize("code", ["model_http_retry_exhausted", "model_http_retry_window_exhausted"])
def test_http_recovery_exhaustion_never_falls_through_to_celery_retry(database, monkeypatch, code):
    build_id, unit_id, sibling_id, completed_id = work(database)
    call_id, group = str(uuid4()), str(uuid4())
    with database() as session:
        session.get(BrainUnit, unit_id).diagnostic = {"transient_retries": 3}
        session.commit()

    async def fail(unit, build, project, token):
        meter = BuildCallMeter(build.id, unit.id, token)
        meter.reserve(call_id, 100, CallSpec("privacy", "local_model", "semantic_output",
                                            transport_group=group, transport_attempt=1))
        meter.close_http_error(call_id, 100, 500)
        raise ModelTransportFailed(code)

    monkeypatch.setattr("src.tasks.brain_task._execute", fail)
    retry = MagicMock()
    monkeypatch.setattr(execute_brain_unit, "retry", retry)
    monkeypatch.setattr("src.tasks.brain_task.dispatch_pending", MagicMock())
    dispatch = MagicMock()
    monkeypatch.setattr(execute_brain_unit, "apply_async", dispatch)
    assert execute_brain_unit.run(unit_id) == {"status": "failed"}
    retry.assert_not_called()
    dispatch.assert_not_called()
    with database() as session:
        build = session.get(BrainBuild, build_id)
        current = session.get(BrainUnit, unit_id)
        assert build.status == current.status == "failed" and current.attempts == 1
        assert build.reserved_tokens == 100 and build.total_prompt_tokens == build.total_completion_tokens == 0
        assert f"failure_code={code}" in build.error_message
        assert current.payload.get("_reserved_tokens", 0) == current.payload.get("_unknown_tokens", 0) == 0
        assert session.get(BrainModelCall, call_id).state == "http_error"
        assert session.get(BrainUnit, sibling_id).status == "failed"
        assert session.get(BrainUnit, completed_id).result == {"checkpoint": "preserved"}


@pytest.mark.parametrize("role,after_response", [("privacy", False), ("privacy", True), ("semantic", False)])
def test_work_denial_after_reservation_preserves_unknown_without_celery_retry(database, monkeypatch,
                                                                           role, after_response):
    """The new classification never creates a fake zero-usage settlement."""
    build_id, unit_id, sibling_id, completed_id = work(database)
    call_id = str(uuid4())

    async def fail(unit, build, project, token):
        meter = BuildCallMeter(build.id, unit.id, token)
        meter.reserve(call_id, 100, CallSpec(role, "synthetic-model", "semantic_output"))
        raise ModelWorkAdmissionDenied("deadline", "after_reserve", after_response=after_response)

    monkeypatch.setattr("src.tasks.brain_task._execute", fail)
    retry, dispatch = MagicMock(), MagicMock()
    monkeypatch.setattr(execute_brain_unit, "retry", retry)
    monkeypatch.setattr("src.tasks.brain_task.dispatch_pending", dispatch)
    assert execute_brain_unit.run(unit_id) == {"status": "failed"}
    retry.assert_not_called()
    dispatch.assert_not_called()
    with database() as session:
        build, current = session.get(BrainBuild, build_id), session.get(BrainUnit, unit_id)
        call = session.get(BrainModelCall, call_id)
        assert build.status == current.status == "failed" and current.attempts == 1
        assert build.reserved_tokens == 100 and build.total_prompt_tokens == build.total_completion_tokens == 0
        assert current.payload["_reserved_tokens"] == 0 and current.payload["_unknown_tokens"] == 100
        assert call.state == "unknown" and call.settled_at is None
        assert "failure_code=model_calls_incomplete" in build.error_message
        assert session.get(BrainUnit, sibling_id).status == "failed"
        assert session.get(BrainUnit, completed_id).result == {"checkpoint": "preserved"}


def test_retry_limit_is_part_of_the_recipe(monkeypatch):
    from src.config import settings
    from src.core.project_brain.lifecycle import recipe_hash
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MODEL_HTTP_RETRIES", 2)
    previous = recipe_hash()
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MODEL_HTTP_RETRIES", 1)
    assert recipe_hash() != previous


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
def test_http_backend_runtime_version_is_part_of_the_recipe(monkeypatch, backend):
    from src.core.project_brain import recipes as lifecycle

    original = lifecycle.version
    monkeypatch.setattr(lifecycle, "version", lambda package: "synthetic-before" if package == backend else original(package))
    before = lifecycle.recipe_hash()
    monkeypatch.setattr(lifecycle, "version", lambda package: "synthetic-after" if package == backend else original(package))
    assert lifecycle.recipe_hash() != before
