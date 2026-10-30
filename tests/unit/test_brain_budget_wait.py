"""Budget contention must wait without losing unknown provider charges."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from src.config import settings
from src.core.llm_client import TokenUsage
from src.core.project_brain import lifecycle
from src.models.brain import BrainBuild, BrainUnit, utcnow
from tests.unit.test_project_brain import database  # noqa: F401


class _MissingBudgetWait(Exception):
    """Make the old implementation fail on its actual premature exhaustion."""


def two_units(database):
    """Return two independently leased units in the same active build."""
    with database() as session:
        build = lifecycle.create_build(session, 1, "main", "local/budget", "a" * 40)
        session.flush()
        second = BrainUnit(build_id=build.id, key="other", kind="compile")
        session.add(second)
        session.commit()
        ids = session.scalars(select(BrainUnit.id).where(BrainUnit.build_id == build.id)).all()
    return [lifecycle.claim_unit(identity) for identity in ids]


def test_inflight_reservation_waits_then_settles_without_a_failed_attempt(database, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_BUILD_TOKENS", 100)
    first, second = two_units(database)
    a, build, _, token_a = first
    b, _, _, token_b = second
    lifecycle.reserve_tokens(build.id, token_a, a.id, 80)
    with pytest.raises(getattr(lifecycle, "BudgetBusy", _MissingBudgetWait)):
        lifecycle.reserve_tokens(build.id, token_b, b.id, 50)
    with database() as session:
        assert session.get(BrainBuild, build.id).reserved_tokens == 80
    lifecycle.account_tokens(build.id, TokenUsage(20, 10), False, 80, unit_id=a.id)
    lifecycle.reserve_tokens(build.id, token_b, b.id, 70)
    with database() as session:
        assert session.get(BrainBuild, build.id).reserved_tokens == 100
        assert session.get(BrainUnit, a.id).payload.get("_reserved_tokens", 0) == 0


def test_unknown_or_expired_requests_are_never_released_for_admission(database, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_BUILD_TOKENS", 100)
    first, second = two_units(database)
    a, build, _, token_a = first
    b, _, _, token_b = second
    lifecycle.reserve_tokens(build.id, token_a, a.id, 80)
    with database() as session:
        row = session.get(BrainUnit, a.id)
        row.lease_until = utcnow() - timedelta(seconds=1)
        session.commit()
    with pytest.raises(ValueError, match="预算") as error:
        lifecycle.reserve_tokens(build.id, token_b, b.id, 50)
    assert not isinstance(error.value, getattr(lifecycle, "BudgetBusy", _MissingBudgetWait))
    with database() as session:
        assert session.get(BrainBuild, build.id).reserved_tokens == 80


def test_settlement_releases_only_known_call_not_prior_unknown_call(database, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_BUILD_TOKENS", 100)
    first, _ = two_units(database)
    unit, build, _, token = first
    lifecycle.reserve_tokens(build.id, token, unit.id, 30)
    lifecycle.reserve_tokens(build.id, token, unit.id, 50)
    lifecycle.account_tokens(build.id, TokenUsage(10, 10), False, 50, unit_id=unit.id)
    with database() as session:
        assert session.get(BrainBuild, build.id).reserved_tokens == 50
        assert session.get(BrainUnit, unit.id).payload["_reserved_tokens"] == 30
