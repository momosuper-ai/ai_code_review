from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from src.config import settings
from src.core.project_brain.exceptions import ModelTransportFailed, ModelWorkAdmissionDenied
from src.core.project_brain.lifecycle import create_build
from src.models.brain import BrainUnit
from src.tasks import brain_task
from src.tasks.brain_task import _safe_failure_metadata, _transient_failure_detail
from tests.unit.test_project_brain import database  # noqa: F401


def test_admission_denied_metadata_contains_only_safe_scalars():
    error = ModelWorkAdmissionDenied(
        "deadline", "before_prepare", after_response=False,
        remaining_seconds=12, required_seconds=60,
    )
    assert _safe_failure_metadata(error) == {
        "exception": "ModelWorkAdmissionDenied",
        "failure_code": "model_work_window_exhausted",
        "reason": "deadline",
        "phase": "before_prepare",
        "after_response": False,
        "reservation_created": False,
        "remaining_seconds": 12,
        "required_seconds": 60,
    }


def test_transport_failure_messages_are_split_by_failure_code():
    assert _transient_failure_detail(ModelTransportFailed("model_http_retry_exhausted")) == (
        "临时HTTP错误已达到重试次数上限，费用预留保留；failure_code=model_http_retry_exhausted"
    )
    assert _transient_failure_detail(ModelTransportFailed("model_http_retry_window_exhausted")) == (
        "HTTP错误后剩余工作窗口不足以继续重试，费用预留保留；failure_code=model_http_retry_window_exhausted"
    )


def test_failure_metadata_never_contains_exception_message():
    error = RuntimeError("synthetic-sensitive-message")
    assert _safe_failure_metadata(error) == {"exception": "RuntimeError"}


def test_admission_denial_persists_timing_diagnostics(database, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_ENABLED", True)
    with database() as session:
        build = create_build(session, 1, "main", "synthetic/diagnostics", "d" * 40, historical=True)
        unit_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build.id))
        session.commit()
    denial = ModelWorkAdmissionDenied(
        "deadline", "before_prepare", after_response=False,
        remaining_seconds=12, required_seconds=60,
    )
    monkeypatch.setattr(brain_task, "_execute", AsyncMock(side_effect=denial))
    dispatch = MagicMock()
    monkeypatch.setattr(brain_task.execute_brain_unit, "apply_async", dispatch)
    assert brain_task.execute_brain_unit.run(unit_id) == {"status": "deferred"}
    with database() as session:
        diagnostic = session.get(BrainUnit, unit_id).diagnostic
    assert diagnostic["last_failure"]["reason"] == "deadline"
    assert diagnostic["last_failure"]["phase"] == "before_prepare"
    assert diagnostic["last_failure"]["remaining_seconds"] == 12
    assert diagnostic["last_failure"]["required_seconds"] == 60
    assert set(diagnostic["segments"]) == set(brain_task.timing.SEGMENT_NAMES)
    dispatch.assert_called_once()
