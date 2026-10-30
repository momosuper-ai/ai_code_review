"""Real row locks retain failed HTTP costs independently of successful scans."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.core.llm_client import TokenUsage
from src.core.project_brain.exceptions import ModelCallsIncomplete
from src.core.project_brain.lifecycle import claim_unit, create_build
from src.core.project_brain.usage import BuildCallMeter, CallSpec, assert_calls_terminal, usage_data
from src.models import SyncSession
from src.models.brain import BrainBuild, BrainModelCall, BrainUnit
from tests.integration.test_brain_postgres import pg_project, pytestmark  # noqa: F401


def claimed(pg_project):
    with SyncSession() as session:
        build = create_build(session, pg_project, "main", "local/integration", "a" * 40)
        session.commit()
        unit_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build.id))
    unit, build, _, token = claim_unit(unit_id)
    return unit, build, BuildCallMeter(build.id, unit.id, token)


def test_postgres_concurrent_error_close_is_idempotent_and_own_success_is_terminal(pg_project):
    unit, build, meter = claimed(pg_project)
    group, failed, succeeded = str(uuid4()), str(uuid4()), str(uuid4())
    meter.reserve(failed, 100, CallSpec("privacy", "local_model", "generic",
                                       transport_group=group, transport_attempt=1))
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert list(workers.map(lambda _: meter.close_http_error(failed, 100, 500), range(2))) == [1, 1]
    with SyncSession() as session:
        with pytest.raises(ModelCallsIncomplete):
            assert_calls_terminal(session, build.id)
    meter.reserve(succeeded, 120, CallSpec("privacy", "local_model", "generic",
                                          transport_group=group, transport_attempt=2))
    meter.settle(succeeded, TokenUsage(30, 4), False, 120, None)
    with SyncSession() as session:
        assert_calls_terminal(session, build.id)
        stored = session.get(BrainBuild, build.id)
        assert stored.reserved_tokens == 134
        assert stored.total_prompt_tokens + stored.total_completion_tokens == 34
        assert session.get(BrainUnit, unit.id).payload["_reserved_tokens"] == 0
        error = session.get(BrainModelCall, failed)
        assert error.state == "http_error" and error.http_status == 500 and error.closed_at is not None
        rows = session.execute(select(BrainModelCall, BrainUnit).join(BrainUnit).where(
            BrainModelCall.build_id == build.id)).all()
        report = usage_data(stored, rows)
        assert report["unknown_reserved_tokens"] == report["closed_http_error_reserved_tokens"] == 100
        assert report["ambiguous_reserved_tokens"] == report["live_reserved_tokens"] == 0
        assert not report["exact_model_shares_available"]
    with pytest.raises(ValueError, match="记账状态"):
        meter.settle(failed, TokenUsage(0, 0), False, 100, None)


def test_postgres_http_sequence_unique_and_error_bound_survives_unit_handoff(pg_project):
    unit, build, meter = claimed(pg_project)
    group, call = str(uuid4()), str(uuid4())
    meter.reserve(call, 100, CallSpec("semantic", "deepseek-v4-flash", "initial",
                                     transport_group=group, transport_attempt=1))
    meter.close_http_error(call, 100, 503)
    with pytest.raises(ValueError, match="来源或顺序"):
        meter.reserve(str(uuid4()), 100, CallSpec("semantic", "deepseek-v4-flash", "initial",
                                                 transport_group=group, transport_attempt=1))
    from src.core.project_brain.usage import abandon_reservation
    with SyncSession() as session:
        stored = session.get(BrainUnit, unit.id)
        abandon_reservation(stored, session)
        stored.status, stored.lease_token, stored.lease_until = "pending", None, None
        session.commit()
        assert stored.payload.get("_unknown_tokens", 0) == 0
        assert session.get(BrainBuild, build.id).reserved_tokens == 100
        assert session.get(BrainModelCall, call).state == "http_error"
        with pytest.raises(ModelCallsIncomplete):
            assert_calls_terminal(session, build.id)
