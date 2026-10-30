"""Durable accounting stays atomic, fenced and explicit about missing costs."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.config import settings
from src.core.llm_client import TokenUsage
from src.core.project_brain.lifecycle import BudgetBusy, BudgetExhausted, account_tokens, claim_unit, create_build, reserve_tokens
from src.core.project_brain.usage import BuildCallMeter, CallSpec, usage_data
from src.models.brain import BrainBuild, BrainModelCall, BrainUnit, utcnow
from src.tasks.brain_task import _abandon_live_reservation
from tests.unit.test_project_brain import database  # noqa: F401


def claimed(database):
    with database() as session:
        build = create_build(session, 1, "main", "fixture/project", "a" * 40)
        session.commit()
        unit_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build.id))
    unit, build, _, token = claim_unit(unit_id)
    return unit, build, token, BuildCallMeter(build.id, unit.id, token)


def snapshot_usage(database, build_id):
    with database() as session:
        build = session.get(BrainBuild, build_id)
        rows = session.execute(select(BrainModelCall, BrainUnit).join(BrainUnit).where(BrainModelCall.build_id == build_id)).all()
        return usage_data(build, rows)


def test_model_roles_sum_to_build_and_cached_is_only_a_subset(database):
    unit, build, token, meter = claimed(database)
    for role, purpose, attempt, usage in [("privacy", "generic", 1, TokenUsage(60, 4)),
                                          ("semantic", "initial", 1, TokenUsage(100, 20, 80)),
                                          ("semantic", "format_retry", 2, TokenUsage(50, 10, 30))]:
        call_id = str(uuid4())
        meter.reserve(call_id, 300, CallSpec(role, "same-alias", purpose, attempt))
        meter.settle(call_id, usage, False, 300, "provider-reported-name")
    data = snapshot_usage(database, build.id)
    assert data["attribution_status"] == "complete" and data["exact_model_shares_available"]
    assert data["total_tokens"] == data["attributed_tokens"] == 244
    assert data["total_cached_tokens_subset"] == 110
    assert len(data["models"]) == 2  # Equal aliases still have distinct roles.
    assert {m["role"]: m["total_tokens"] for m in data["models"]} == {"privacy": 64, "semantic": 180}
    assert sum(m["token_share_percent"] for m in data["models"]) == pytest.approx(100)
    assert {g["purpose"] for g in data["breakdown"]} == {"generic", "initial", "format_retry"}
    assert data["currency_cost"] is None


def test_duplicate_settlement_is_idempotent_and_conflicting_repeat_rolls_back(database):
    _, build, _, meter = claimed(database)
    call_id = str(uuid4())
    meter.reserve(call_id, 100, CallSpec("privacy", "local_model", "generic"))
    meter.settle(call_id, TokenUsage(30, 2), False, 100, None)
    meter.settle(call_id, TokenUsage(30, 2), False, 100, None)
    with pytest.raises(ValueError, match="重复结算内容不一致"):
        meter.settle(call_id, TokenUsage(31, 2), False, 100, None)
    data = snapshot_usage(database, build.id)
    assert data["total_tokens"] == 32 and data["models"][0]["settled_calls"] == 1


@pytest.mark.parametrize("wrong", ["bound", "lease", "unit"])
def test_foreign_accounting_cannot_mutate_totals_or_record(database, wrong):
    unit, build, token, meter = claimed(database)
    call_id = str(uuid4())
    meter.reserve(call_id, 100, CallSpec("semantic", "deepseek-v4-flash", "initial"))
    with pytest.raises(ValueError, match="归属无效"):
        account_tokens(build.id, TokenUsage(10, 2), False, 99 if wrong == "bound" else 100,
                       unit_id=unit.id + 10 if wrong == "unit" else unit.id,
                       lease_token="other" if wrong == "lease" else token, call_id=call_id)
    with database() as session:
        assert session.get(BrainBuild, build.id).reserved_tokens == 100
        assert session.get(BrainBuild, build.id).total_prompt_tokens == 0
        assert session.get(BrainModelCall, call_id).state == "reserved"


def test_request_id_cannot_reserve_twice(database):
    _, build, _, meter = claimed(database)
    call_id = str(uuid4())
    spec = CallSpec("privacy", "local_model", "generic")
    meter.reserve(call_id, 80, spec)
    with pytest.raises(ValueError, match="禁止重复发送"):
        meter.reserve(call_id, 80, spec)
    with database() as session:
        assert session.get(BrainBuild, build.id).reserved_tokens == 80


def test_legacy_late_settlement_does_not_clear_an_already_reserved_successor(database):
    """Existing legacy rows can contain two leases; settlement must stay fenced.

    Current claim admission no longer creates this state after an unknown
    response. Construct the historical ledger explicitly instead of bypassing
    the new guard through a fresh claim/reserve call.
    """
    unit, build, token, meter = claimed(database)
    old = str(uuid4())
    meter.reserve(old, 80, CallSpec("privacy", "local_model", "generic"))
    with database() as session:
        unit = session.get(BrainUnit, unit.id)
        _abandon_live_reservation(unit, session)
        unit.status, unit.lease_token, unit.lease_until = "pending", None, None
        session.commit()
    data = snapshot_usage(database, build.id)
    assert data["unknown_reserved_tokens"] == 80 and not data["exact_model_shares_available"]
    next_token = str(uuid4())
    successor = str(uuid4())
    with database() as session:
        current = session.get(BrainUnit, unit.id)
        current.status, current.lease_token = "running", next_token
        current.lease_until = utcnow() + timedelta(seconds=60)
        current.payload = {**current.payload, "_reserved_tokens": 100}
        stored = session.get(BrainBuild, build.id)
        stored.reserved_tokens += 100
        session.add(BrainModelCall(id=successor, build_id=build.id, unit_id=unit.id,
            lease_token=next_token, stage=current.kind, role="semantic", purpose="initial",
            requested_model="deepseek-v4-flash", attempt=1, state="reserved", reserved_tokens=100))
        session.commit()
    next_meter = BuildCallMeter(build.id, unit.id, next_token)
    meter.settle(old, TokenUsage(20, 2), False, 80, "Qwen/fixture")
    with database() as session:
        current = session.get(BrainUnit, unit.id)
        assert current.lease_token == next_token and current.payload["_reserved_tokens"] == 100
        assert current.payload["_unknown_tokens"] == 0
        assert session.get(BrainBuild, build.id).reserved_tokens == 122
    next_meter.settle(successor, TokenUsage(30, 4), False, 100, "DeepSeek/fixture")
    data = snapshot_usage(database, build.id)
    assert data["total_tokens"] == 56 and data["exact_model_shares_available"]


def test_expired_direct_claim_marks_old_call_unknown_and_stops_before_new_lease(database):
    unit, build, _, meter = claimed(database)
    call_id = str(uuid4())
    meter.reserve(call_id, 80, CallSpec("privacy", "local_model", "generic"))
    with database() as session:
        old = session.get(BrainUnit, unit.id)
        old.lease_until = utcnow() - timedelta(seconds=1)
        session.commit()
    assert claim_unit(unit.id) is None
    with database() as session:
        assert session.get(BrainModelCall, call_id).state == "unknown"
        current = session.get(BrainUnit, unit.id)
        assert current.payload["_unknown_tokens"] == 80 and current.payload["_reserved_tokens"] == 0
        assert current.status == "failed" and current.attempts == 1
        assert current.lease_token is None and current.lease_until is None
        stored = session.get(BrainBuild, build.id)
        assert stored.status == "failed" and stored.completed_at is not None
        assert stored.reserved_tokens == 80
        assert "failure_code=model_calls_incomplete" in stored.error_message
    meter.settle(call_id, TokenUsage(20, 2), False, 80, None)
    with database() as session:
        stored = session.get(BrainBuild, build.id)
        assert stored.status == "failed" and stored.reserved_tokens == 22
        assert session.get(BrainModelCall, call_id).state == "settled"
    assert claim_unit(unit.id) is None


def test_superseded_build_restoration_preserves_old_charge_and_late_settlement(database):
    unit, build, _, old_meter = claimed(database)
    old_call = str(uuid4())
    old_meter.reserve(old_call, 80, CallSpec("privacy", "local_model", "generic"))
    with database() as session:
        session.get(BrainBuild, build.id).status = "superseded"
        session.commit()
    with database() as session:
        restored = create_build(session, 1, "main", "fixture/project", "a" * 40)
        assert restored.id == build.id
        session.commit()
    assert claim_unit(unit.id) is None
    with database() as session:
        assert session.get(BrainModelCall, old_call).state == "unknown"
        current = session.get(BrainUnit, unit.id)
        assert current.payload["_reserved_tokens"] == 0 and current.payload["_unknown_tokens"] == 80
        assert current.status == "failed" and current.lease_token is None
        stored = session.get(BrainBuild, build.id)
        assert stored.reserved_tokens == 80 and stored.status == "failed"
    old_meter.settle(old_call, TokenUsage(20, 2), False, 80, None)
    with database() as session:
        current = session.get(BrainUnit, unit.id)
        assert current.payload["_reserved_tokens"] == 0 and current.payload["_unknown_tokens"] == 0
        assert current.status == "failed" and current.lease_token is None
        assert session.get(BrainBuild, build.id).status == "failed"
        assert session.get(BrainBuild, build.id).reserved_tokens == 22
        assert session.get(BrainModelCall, old_call).state == "settled"
    assert snapshot_usage(database, build.id)["total_tokens"] == 22
    assert claim_unit(unit.id) is None


def test_failed_build_reserved_record_is_reported_unknown_even_before_recovery(database):
    _, build, _, meter = claimed(database)
    call_id = str(uuid4())
    meter.reserve(call_id, 80, CallSpec("privacy", "local_model", "generic"))
    with database() as session:
        stored = session.get(BrainBuild, build.id)
        stored.status = "failed"
        session.commit()
    data = snapshot_usage(database, build.id)
    assert data["unknown_reserved_tokens"] == 80 and data["live_reserved_tokens"] == 0


def test_estimates_are_explicit_and_do_not_claim_exact_shares(database):
    _, build, _, meter = claimed(database)
    call_id = str(uuid4())
    meter.reserve(call_id, 100, CallSpec("semantic", "deepseek-v4-flash", "initial"))
    meter.settle(call_id, TokenUsage(30, 2), True, 100, None)
    data = snapshot_usage(database, build.id)
    assert data["estimated_tokens"] == 32 and data["models"][0]["estimated_calls"] == 1
    assert not data["exact_model_shares_available"] and data["models"][0]["token_share_percent"] is None


def test_legacy_totals_are_not_fabricated_into_model_shares(database):
    _, build, _, _ = claimed(database)
    with database() as session:
        stored = session.get(BrainBuild, build.id)
        stored.usage_tracking_version = 0
        stored.total_prompt_tokens, stored.total_completion_tokens = 70, 7
        stored.reserved_tokens = 77
        session.commit()
    data = snapshot_usage(database, build.id)
    assert data["attribution_status"] == "legacy_unattributed"
    assert data["unattributed_tokens"] == 77 and not data["models"]


def test_partial_old_and_new_accounting_keeps_unattributed_cost_in_denominator(database):
    unit, build, token, meter = claimed(database)
    reserve_tokens(build.id, token, unit.id, 100)
    account_tokens(build.id, TokenUsage(20, 2), False, 100, unit_id=unit.id, lease_token=token)
    call_id = str(uuid4())
    meter.reserve(call_id, 100, CallSpec("privacy", "local_model", "generic"))
    meter.settle(call_id, TokenUsage(30, 3), False, 100, None)
    data = snapshot_usage(database, build.id)
    assert data["total_tokens"] == 55 and data["unattributed_tokens"] == 22
    assert data["attribution_status"] == "partial" and data["models"][0]["token_share_percent"] is None


def test_invalid_provider_model_label_is_not_persisted_or_echoed(database):
    _, build, _, meter = claimed(database)
    call_id = str(uuid4())
    meter.reserve(call_id, 100, CallSpec("privacy", "local_model", "generic"))
    marker = "arbitrary-untrusted-response\nbody"
    meter.settle(call_id, TokenUsage(30, 2), False, 100, marker)
    data = snapshot_usage(database, build.id)
    assert data["breakdown"][0]["response_model"] is None
    assert marker not in str(data)


def test_unclassified_provider_label_is_not_persisted_even_if_identifier_shaped(database):
    _, build, _, meter = claimed(database)
    call_id = str(uuid4())
    meter.reserve(call_id, 100, CallSpec("privacy", "local_model", "generic"))
    meter.settle(call_id, TokenUsage(30, 2), False, 100, "unclassified-provider-value")
    data = snapshot_usage(database, build.id)
    assert data["breakdown"][0]["response_model"] is None
    assert "unclassified-provider-value" not in str(data)


def test_provider_usage_above_budget_is_atomically_recorded_before_failure(database, monkeypatch):
    _, build, _, meter = claimed(database)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_BUILD_TOKENS", 100)
    call_id = str(uuid4())
    meter.reserve(call_id, 80, CallSpec("semantic", "deepseek-v4-flash", "initial"))
    with pytest.raises(BudgetExhausted):
        meter.settle(call_id, TokenUsage(120, 5), False, 80, None)
    with pytest.raises(BudgetExhausted):
        meter.settle(call_id, TokenUsage(120, 5), False, 80, None)
    with database() as session:
        assert session.get(BrainBuild, build.id).reserved_tokens == 125
        assert session.get(BrainModelCall, call_id).state == "settled"
        assert session.get(BrainBuild, build.id).total_prompt_tokens == 120


@pytest.mark.parametrize("usage", [TokenUsage(-1, 2), TokenUsage(1, -2), TokenUsage(2, 1, 3)])
def test_invalid_usage_leaves_reservation_unsettled(database, usage):
    _, build, _, meter = claimed(database)
    call_id = str(uuid4())
    meter.reserve(call_id, 100, CallSpec("privacy", "local_model", "generic"))
    with pytest.raises(ValueError, match="用量结构无效"):
        meter.settle(call_id, usage, False, 100, None)
    with database() as session:
        assert session.get(BrainModelCall, call_id).state == "reserved"
        assert session.get(BrainBuild, build.id).reserved_tokens == 100


def test_budget_busy_does_not_create_phantom_model_calls(database, monkeypatch):
    unit, build, _, meter = claimed(database)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_BUILD_TOKENS", 100)
    with database() as session:
        other = BrainUnit(build_id=build.id, key="other", kind="compile", status="running",
                          lease_token="other", lease_until=utcnow() + timedelta(seconds=60), payload={"_reserved_tokens": 80})
        session.add(other)
        stored = session.get(BrainBuild, build.id)
        stored.reserved_tokens = 80
        session.commit()
    with pytest.raises(BudgetBusy):
        meter.reserve(str(uuid4()), 80, CallSpec("privacy", "local_model", "generic"))
    with database() as session:
        assert not session.scalars(select(BrainModelCall)).all()
        assert session.get(BrainBuild, build.id).reserved_tokens == 80
