"""Terminal HTTP errors retain unknown costs and need their own retry success."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.config import settings
from src.core.llm_client import TokenUsage
from src.core.project_brain.exceptions import ModelCallsIncomplete
from src.core.project_brain.lifecycle import BudgetExhausted, claim_unit, create_build
from src.core.project_brain.usage import (
    HTTP_ERROR_UNIT_LIMIT, BuildCallMeter, CallSpec, abandon_reservation,
    assert_calls_terminal, usage_data,
)
from src.models.brain import BrainBuild, BrainModelCall, BrainUnit, utcnow
from tests.unit.test_brain_model_usage import claimed, snapshot_usage
from tests.unit.test_project_brain import database  # noqa: F401


def reserve_http(meter, group, number=1, bound=100, **changes):
    spec = {"role": "privacy", "requested_model": "local_model", "purpose": "generic",
            "transport_group": group, "transport_attempt": number, **changes}
    call_id = str(uuid4())
    meter.reserve(call_id, bound, CallSpec(**spec))
    return call_id


def completed_group(database):
    unit, build, token, meter = claimed(database)
    group = str(uuid4())
    first = reserve_http(meter, group)
    assert meter.close_http_error(first, 100, 500) == 1
    last = reserve_http(meter, group, 2, 120)
    meter.settle(last, TokenUsage(20, 2, 3), False, 120, None)
    return unit, build, token, meter, first, last


def test_closed_error_retains_full_charge_and_own_success_does_not_clear_it(database):
    unit, build, _, _, first, _ = completed_group(database)
    with database() as session:
        closed = session.get(BrainModelCall, first)
        stored = session.get(BrainBuild, build.id)
        assert closed.state == "http_error" and closed.http_status == 500
        assert closed.closed_at is not None and closed.settled_at is None
        assert (closed.prompt_tokens, closed.completion_tokens, closed.cached_tokens) == (0, 0, 0)
        assert not closed.usage_estimated
        assert stored.reserved_tokens == 122
        assert (stored.total_prompt_tokens, stored.total_completion_tokens) == (20, 2)
        assert session.get(BrainUnit, unit.id).payload["_reserved_tokens"] == 0
        assert_calls_terminal(session, build.id)
    data = snapshot_usage(database, build.id)
    assert data["attribution_status"] == "complete"
    assert data["total_tokens"] == 22 and data["unknown_reserved_tokens"] == 100
    assert data["closed_http_error_calls"] == 1 and data["closed_http_error_reserved_tokens"] == 100
    assert data["ambiguous_reserved_tokens"] == data["live_reserved_tokens"] == 0
    assert not data["exact_model_shares_available"]
    for row in [*data["models"], *data["breakdown"]]:
        assert row["closed_http_error_calls"] == 1
        assert row["closed_http_error_reserved_tokens"] == row["unknown_reserved_tokens"] == 100
        assert row["ambiguous_reserved_tokens"] == 0
    assert data["models"][0]["token_share_percent"] is None


def test_close_is_idempotent_and_conflicting_repeat_cannot_change_charge(database):
    _, build, _, meter = claimed(database)
    call_id = reserve_http(meter, str(uuid4()))
    assert meter.close_http_error(call_id, 100, 503) == 1
    assert meter.close_http_error(call_id, 100, 503) == 1
    with pytest.raises(ValueError, match="重复关闭"):
        meter.close_http_error(call_id, 100, 500)
    with database() as session:
        assert session.get(BrainBuild, build.id).reserved_tokens == 100
        assert session.get(BrainModelCall, call_id).http_status == 503


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, True, "500"])
def test_non_retryable_status_cannot_create_terminal_error(database, status):
    _, build, _, meter = claimed(database)
    call_id = reserve_http(meter, str(uuid4()))
    with pytest.raises(ValueError):
        meter.close_http_error(call_id, 100, status)
    with database() as session:
        assert session.get(BrainModelCall, call_id).state == "reserved"
        assert session.get(BrainBuild, build.id).reserved_tokens == 100


@pytest.mark.parametrize("fault", ["bound", "lease", "foreign_unit", "expired", "settled", "unknown",
                                  "prompt", "completion", "cached", "estimated", "response_model", "closed_at"])
def test_close_rejects_bad_ownership_or_nonempty_response_metadata(database, fault):
    unit, build, token, meter = claimed(database)
    call_id = reserve_http(meter, str(uuid4()))
    with database() as session:
        call = session.get(BrainModelCall, call_id)
        if fault in {"settled", "unknown"}:
            call.state = fault
        elif fault in {"prompt", "completion", "cached"}:
            setattr(call, fault + "_tokens", 1)
        elif fault == "estimated":
            call.usage_estimated = True
        elif fault == "response_model":
            call.response_model = "local_model"
        elif fault == "closed_at":
            call.closed_at = utcnow()
        elif fault == "expired":
            session.get(BrainUnit, unit.id).lease_until = utcnow() - timedelta(seconds=1)
        session.commit()
    if fault == "lease":
        meter = BuildCallMeter(build.id, unit.id, str(uuid4()))
    if fault == "foreign_unit":
        meter = BuildCallMeter(build.id, unit.id + 999, token)
    with pytest.raises(ValueError):
        meter.close_http_error(call_id, 99 if fault == "bound" else 100, 500)
    with database() as session:
        assert session.get(BrainBuild, build.id).reserved_tokens == 100


@pytest.mark.parametrize("change", [{"role": "semantic"}, {"requested_model": "other"},
                                   {"purpose": "initial"}, {"attempt": 2}])
def test_group_cannot_change_semantic_call_identity(database, change):
    _, _, _, meter = claimed(database)
    group = str(uuid4())
    first = reserve_http(meter, group)
    meter.close_http_error(first, 100, 500)
    with pytest.raises(ValueError, match="来源或顺序"):
        reserve_http(meter, group, 2, **change)


def test_group_rejects_skips_live_predecessors_successors_after_success_and_foreign_scope(database):
    unit, build, _, meter = claimed(database)
    group = str(uuid4())
    with pytest.raises(ValueError):
        reserve_http(meter, group, 2)
    first = reserve_http(meter, group)
    with pytest.raises(ValueError):
        reserve_http(meter, group, 2)
    meter.close_http_error(first, 100, 500)
    with database() as session:
        foreign = BrainUnit(build_id=build.id, key="other", kind="privacy")
        session.add(foreign)
        session.commit()
        foreign_id = foreign.id
    other, _, _, other_token = claim_unit(foreign_id)
    with pytest.raises(ValueError):
        reserve_http(BuildCallMeter(build.id, other.id, other_token), group, 2)
    second = reserve_http(meter, group, 2)
    meter.settle(second, TokenUsage(5, 1), False, 100, None)
    with pytest.raises(ValueError):
        reserve_http(meter, group, 3)


def test_closed_unknown_charge_cannot_be_settled_as_zero_or_abandoned_twice(database):
    unit, build, _, meter = claimed(database)
    first = reserve_http(meter, str(uuid4()))
    meter.close_http_error(first, 100, 500)
    with pytest.raises(ValueError, match="状态无效"):
        meter.settle(first, TokenUsage(), True, 100, None)
    with database() as session:
        current = session.get(BrainUnit, unit.id)
        abandon_reservation(current, session)
        abandon_reservation(current, session)
        session.commit()
        assert current.payload.get("_unknown_tokens", 0) == 0
        assert session.get(BrainBuild, build.id).reserved_tokens == 100
        assert session.get(BrainModelCall, first).state == "http_error"


def test_transport_retries_and_six_error_cap_are_persistent_across_new_leases(database):
    unit, build, _, meter = claimed(database)
    for index in range(HTTP_ERROR_UNIT_LIMIT):
        group = str(uuid4())
        call = reserve_http(meter, group)
        assert meter.close_http_error(call, 100, 500) == index + 1
    with database() as session:
        current = session.get(BrainUnit, unit.id)
        current.status, current.lease_token, current.lease_until = "pending", None, None
        session.commit()
    fresh, _, _, token = claim_unit(unit.id)
    with pytest.raises(ValueError, match="硬上限"):
        reserve_http(BuildCallMeter(build.id, fresh.id, token), str(uuid4()))
    with database() as session:
        assert session.get(BrainBuild, build.id).reserved_tokens == 600
        assert len(session.scalars(select(BrainModelCall).where(BrainModelCall.build_id == build.id)).all()) == 6


def test_reserved_retry_cannot_spend_closed_error_charge_again(database, monkeypatch):
    _, build, _, meter = claimed(database)
    group = str(uuid4())
    first = reserve_http(meter, group)
    meter.close_http_error(first, 100, 500)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_BUILD_TOKENS", 150)
    with pytest.raises(BudgetExhausted):
        reserve_http(meter, group, 2, 60)
    with database() as session:
        assert session.get(BrainBuild, build.id).reserved_tokens == 100


@pytest.mark.parametrize("fault", ["pending_success", "unknown_success", "unsupported_state", "wrong_success_group",
                                  "changed_role", "gap", "bad_status", "missing_closed_time", "nonzero_error_usage",
                                  "unbound_error", "wrong_total", "wrong_cached", "wrong_stage"])
def test_terminal_gate_rejects_unresolved_unmatched_or_forged_groups(database, fault):
    unit, build, _, _, first, last = completed_group(database)
    with database() as session:
        closed, success = session.get(BrainModelCall, first), session.get(BrainModelCall, last)
        if fault == "pending_success":
            success.state = "reserved"
        elif fault == "unknown_success":
            success.state = "unknown"
        elif fault == "unsupported_state":
            success.state = "other"
        elif fault == "wrong_success_group":
            success.transport_group, success.transport_attempt = str(uuid4()), 1
        elif fault == "changed_role":
            success.role = "semantic"
        elif fault == "gap":
            success.transport_attempt = 3
        elif fault == "bad_status":
            closed.http_status = 400
        elif fault == "missing_closed_time":
            closed.closed_at = None
        elif fault == "nonzero_error_usage":
            closed.completion_tokens = 1
        elif fault == "unbound_error":
            closed.transport_group = closed.transport_attempt = None
        elif fault == "wrong_total":
            session.get(BrainBuild, build.id).reserved_tokens -= 1
        elif fault == "wrong_cached":
            session.get(BrainBuild, build.id).total_cached_tokens -= 1
        elif fault == "wrong_stage":
            closed.stage = "compile"
        session.flush()
        with pytest.raises(ModelCallsIncomplete):
            assert_calls_terminal(session, build.id)


def test_terminal_gate_preserves_tracking_zero_unattributed_actual_without_fabricating_calls(database):
    _, build, _, _ = claimed(database)
    with database() as session:
        stored = session.get(BrainBuild, build.id)
        stored.usage_tracking_version = 0
        stored.total_prompt_tokens, stored.total_completion_tokens, stored.reserved_tokens = 20, 3, 23
        session.flush()
        assert_calls_terminal(session, build.id)
        stored.usage_tracking_version = 1
        with pytest.raises(ModelCallsIncomplete):
            assert_calls_terminal(session, build.id)


@pytest.mark.parametrize("group,number", [(None, 1), (str(uuid4()), None), ("not-uuid", 1),
                                         (str(uuid4()), True), (str(uuid4()), 0), (str(uuid4()), 4)])
def test_transport_metadata_is_paired_typed_and_bounded(group, number):
    with pytest.raises(ValueError):
        CallSpec("privacy", "local_model", "generic", transport_group=group, transport_attempt=number).validate()
