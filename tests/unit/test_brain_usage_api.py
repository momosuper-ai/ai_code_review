"""Usage reads preserve project ownership and expose legacy uncertainty."""
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.api.v1.brain import get_model_usage, router
from src.core.llm_client import TokenUsage
from src.core.project_brain.usage import CallSpec
from src.models.brain import BrainBuild
from tests.unit.test_brain_model_usage import claimed
from tests.unit.test_project_brain import database  # noqa: F401
from uuid import uuid4


class AsyncRead:
    """The real SQLAlchemy SELECT is executed; only its async wrapper is replaced."""
    def __init__(self, session):
        self.session = session

    async def scalar(self, query):
        return self.session.scalar(query)

    async def execute(self, query):
        return self.session.execute(query)


@pytest.mark.asyncio
async def test_usage_legacy_endpoint_returns_recorded_model_shares(database):
    _, build, _, meter = claimed(database)
    call = str(uuid4())
    meter.reserve(call, 100, CallSpec("semantic", "deepseek-v4-flash", "initial"))
    meter.settle(call, TokenUsage(20, 3), False, 100, "deepseek-v4-flash")
    with database() as session:
        session.get(BrainBuild, build.id).coverage = {}  # Explicit legacy metadata contract.
        session.commit()
        result = await get_model_usage(1, build.id, AsyncRead(session))
    assert result["total_tokens"] == 23 and result["exact_model_shares_available"]
    assert result["models"][0]["token_share_percent"] == 100
    assert result["breakdown"][0]["response_model"] == "deepseek-v4-flash"
    assert "lease_token" not in str(result)


@pytest.mark.asyncio
async def test_usage_endpoint_rejects_foreign_project_before_reading_calls(database):
    _, build, _, _ = claimed(database)
    with database() as session:
        with pytest.raises(HTTPException) as error:
            await get_model_usage(2, build.id, AsyncRead(session))
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_usage_endpoint_labels_legacy_tokens_without_invented_model_data(database):
    _, build, _, _ = claimed(database)
    with database() as session:
        old = session.get(BrainBuild, build.id)
        old.usage_tracking_version = 0
        old.total_prompt_tokens, old.total_completion_tokens, old.reserved_tokens = 50, 7, 57
        session.commit()
    with database() as session:
        result = await get_model_usage(1, build.id, AsyncRead(session))
    assert result["unattributed_tokens"] == 57 and not result["models"]
    assert not result["exact_model_shares_available"]


def test_usage_route_inherits_authentication_and_read_rate_limit():
    route = next(value for value in router.routes if value.path.endswith("/builds/{build_id}/usage"))
    dependencies = {dependency.call.__name__ for dependency in route.dependant.dependencies}
    assert "require_api_key" in dependencies
    assert len(dependencies) >= 3  # API key, read limit and DB session.
