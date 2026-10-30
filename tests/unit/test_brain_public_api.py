"""Schema 2 API displays sealed metadata, never private operational identities."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.api.v1 import brain as api
from src.config import settings
from src.core.project_brain.public_schema import PublicBrainManifest, PublicMetadata, PublicProjectionInfo
from src.core.project_brain.publisher import publication_actions, publish
from src.models.brain import BrainBuild, BrainProject, BrainPublication, BrainUnit
from tests.unit.test_project_brain import database  # noqa: F401


PRIVATE = "fixture-credential-collision"


def manifest(build_id=None, **overrides):
    values = dict(generator="project-brain/2.0.0", build_id=build_id or str(uuid4()), project_id=1,
                  branch="<redacted-branch>", source_commit="a" * 40, brain_version=1,
                  recipe_hash="b" * 64, source_fingerprint="fp", files={}, coverage={"files": 2}, issues=[],
                  public_metadata=PublicMetadata(branch_display="<redacted-branch>", branch_id="branch:opaque"),
                  projection=PublicProjectionInfo(identity_version="1", key_version="1", privacy_seal="sealed",
                      content_digest="c" * 64, redacted_source_paths=2, direct_source_links=0,
                      source_reference_count=2, direct_link_ratio=0))
    return PublicBrainManifest(**{**values, **overrides})


def build(status="failed", **overrides):
    values = dict(id=str(uuid4()), source_commit="a" * 40, brain_version=1, generation=0, historical=True,
                  base_build_id=None, status=status, recipe_hash="b" * 64,
                  execution_state="running", pause_requested_at=None, paused_at=None, resumed_at=None,
                  coverage={"public_schema_version": "2.0", "issues": [PRIVATE], "cost_plan": {PRIVATE: PRIVATE}},
                  error_message=f"source path {PRIVATE}", total_prompt_tokens=11, total_completion_tokens=3,
                  total_cached_tokens=0, reserved_tokens=14, usage_estimated=False, usage_tracking_version=1,
                  created_at=None, completed_at=None, artifact_path=None)
    return SimpleNamespace(**{**values, **overrides})


class AsyncRead:
    def __init__(self, session):
        self.session = session

    async def scalar(self, query):
        return self.session.scalar(query)

    async def scalars(self, query):
        return self.session.scalars(query)

    async def get(self, model, key):
        return self.session.get(model, key)

    async def execute(self, query):
        return self.session.execute(query)


def test_schema2_pending_display_has_no_private_coverage_or_error_text():
    item = build()
    data = api._build_data(item)
    assert PRIVATE not in json.dumps(data)
    assert data["coverage"] == {} and data["error_message"] == "build_failed"
    assert data["total_prompt_tokens"] == 11
    assert api._public_manifest(item) is None


@pytest.mark.parametrize("text, expected", [
    (f"{PRIVATE};failure_code=source_identity_privacy_conflict", "source_identity_privacy_conflict"),
    (f"failure_code={PRIVATE}", "unit_failed"),
    (None, None),
])
def test_error_categories_are_closed(text, expected):
    assert api._error_code(text, "unit_failed") == expected


def test_publication_new_schema_never_echoes_db_branch_url_or_error():
    publication = SimpleNamespace(id=1, status="failed", target_branch=PRIVATE, commit_sha="d" * 40,
        merge_commit_sha=None, mr_iid=7, mr_url=f"https://example.test/{PRIVATE}/-/merge_requests/7",
        error_message=PRIVATE, error_code=PRIVATE, policy_version="2")
    data = api._publication_data(publication, public=True, manifest=manifest())
    assert PRIVATE not in json.dumps(data)
    assert data["mr_url"] is None and data["mr_iid"] == 7
    assert data["target_branch"] == "<redacted-branch>"
    assert data["error_code"] == "publication_failed"
    # Legacy is explicit compatibility, without a new guarantee.
    assert api._publication_data(publication)["mr_url"] == publication.mr_url


def test_ready_metadata_is_authenticated_and_no_db_fallback(monkeypatch):
    item = build("ready", artifact_path="present")
    public = manifest(item.id)
    text = public.model_dump_json()
    item.coverage["manifest_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    monkeypatch.setattr(api, "ArtifactStore", lambda: SimpleNamespace(repository=lambda _: {"docs/project-brain/manifest.json": text}))
    sealed = api._public_manifest(item)
    data = api._build_data(item, sealed)
    assert data["coverage"] == {"files": 2}
    assert api._branch_data(sealed) == {"branch": "<redacted-branch>", "branch_id": "branch:opaque"}
    item.coverage["manifest_sha256"] = "f" * 64
    with pytest.raises(HTTPException) as error:
        api._public_manifest(item)
    assert error.value.status_code == 409 and PRIVATE not in str(error.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["list", "json", "markdown", "export"])
async def test_all_public_content_endpoints_authenticate_db_manifest_before_returning_data(monkeypatch, endpoint):
    item = build("ready", artifact_path="present")
    item.coverage["manifest_sha256"] = "f" * 64
    sealed = manifest(item.id)
    store = SimpleNamespace(repository=lambda _: {"docs/project-brain/manifest.json": sealed.model_dump_json()},
        read_cards=MagicMock(side_effect=AssertionError("must verify first")),
        export_path=MagicMock(side_effect=AssertionError("must verify first")))
    monkeypatch.setattr(api, "_build", AsyncMock(return_value=item))
    monkeypatch.setattr(api, "ArtifactStore", lambda: store)
    with pytest.raises(HTTPException) as error:
        if endpoint == "list":
            await api.list_cards(1, item.id, session=None)
        elif endpoint == "export":
            await api.export_build(1, item.id, session=None)
        else:
            await api.get_card(1, item.id, "opaque-card", format=endpoint, session=None)
    assert error.value.status_code == 409
    store.read_cards.assert_not_called()
    store.export_path.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_ready_build_with_base_and_no_incremental_summary_is_accessible(database, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from src.core.project_brain.lifecycle import create_build
    from src.models import get_session
    from tests.unit.test_project_brain import AsyncSessionAdapter

    with database() as session:
        base = create_build(session, 1, "main", "synthetic/retry", "a" * 40)
        base.status, base.artifact_path = "ready", "legacy"
        base.coverage = {"public_schema_version": "1.0"}
        child = create_build(session, 1, "main", "synthetic/retry", "b" * 40)
        assert child.base_build_id == base.id
        child.status = "ready"
        child.coverage = {"public_schema_version": "1.0"}
        child.diagnostic = None
        project = session.get(BrainProject, child.project_id)
        project.active_build_id = child.id
        session.commit()

    monkeypatch.setattr(settings, "API_KEYS", "internal-test-key")
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(api, "_public_manifest", lambda build: None)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")

    async def dependency():
        async with AsyncSessionAdapter(database) as session:
            yield session

    app.dependency_overrides[get_session] = dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/projects/1/brain", params={"branch": "main"},
                                    headers={"X-API-Key": "internal-test-key"})
    assert response.status_code == 200
    assert response.json()["current"]["status"] == "ready"


@pytest.mark.asyncio
async def test_failed_sealed_build_cannot_retry_existing_checkpoints(monkeypatch):
    item = build(error_message="隐私封存冲突；failure_code=privacy_seal_conflict")
    monkeypatch.setattr(api, "_enabled", lambda: None)
    monkeypatch.setattr(api, "_build", AsyncMock(return_value=item))
    dispatch = MagicMock()
    monkeypatch.setattr(api, "dispatch_pending", dispatch)
    with pytest.raises(HTTPException) as error:
        await api.retry_build(1, item.id, session=None)
    assert error.value.status_code == 409 and item.status == "failed"
    assert "新构建" in error.value.detail
    dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["quarantined", "changed_recipe"])
async def test_failed_unsafe_or_old_recipe_cannot_resume_paid_work(monkeypatch, kind):
    item = build(error_message="failure_code=known_privacy_regression" if kind == "quarantined" else "failed")
    monkeypatch.setattr(api, "_enabled", lambda: None)
    monkeypatch.setattr(api, "_build", AsyncMock(return_value=item))
    monkeypatch.setattr(api, "recipe_hash", lambda: "c" * 64)
    dispatch = MagicMock()
    monkeypatch.setattr(api, "dispatch_pending", dispatch)
    with pytest.raises(HTTPException) as error:
        await api.retry_build(1, item.id, session=None)
    assert error.value.status_code == 409 and item.status == "failed"
    assert PRIVATE not in str(error.value.detail)
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_failed_build_endpoint_hides_private_unit_keys_and_branch(database):
    with database() as session:
        project = BrainProject(gitlab_project_id=1, project_name=PRIVATE, branch=PRIVATE)
        session.add(project)
        session.flush()
        item = BrainBuild(id=str(uuid4()), project_id=project.id, source_commit="a" * 40,
            recipe_hash="b" * 64, request_seq=1, brain_version=1, status="failed",
            coverage={"public_schema_version": "2.0", "issues": [PRIVATE]}, error_message=PRIVATE)
        session.add(item)
        session.flush()
        project.active_build_id = item.id
        session.add(BrainUnit(build_id=item.id, key=f"compile:{PRIVATE}", kind="compile", status="failed", error_message=PRIVATE))
        session.add(BrainPublication(build_id=item.id, target_branch=PRIVATE, mr_url=f"https://example.test/{PRIVATE}"))
        session.commit()
        data = await api.get_build(1, item.id, AsyncRead(session))
        current = await api.get_brain(1, PRIVATE, AsyncRead(session))
    assert PRIVATE not in json.dumps(data, default=str)
    assert PRIVATE not in json.dumps(current, default=str)
    assert data["units"][0]["key"].startswith("unit:")
    assert current["branch"] is None and current["branch_id"] is None


@pytest.mark.asyncio
async def test_schema2_usage_hides_aliases_without_changing_numeric_attribution(database):
    from src.core.llm_client import TokenUsage
    from src.core.project_brain.usage import CallSpec
    from tests.unit.test_brain_model_usage import claimed

    _, item, _, meter = claimed(database)
    for role, prompt, completion in (("privacy", 8, 2), ("semantic", 20, 3)):
        call_id = str(uuid4())
        meter.reserve(call_id, 100, CallSpec(role, PRIVATE, "initial"))
        meter.settle(call_id, TokenUsage(prompt, completion), False, 100, PRIVATE)
    with database() as session:
        session.get(BrainBuild, item.id).coverage = {"public_schema_version": "2.0"}
        session.commit()
        data = await api.get_model_usage(1, item.id, AsyncRead(session))
    assert PRIVATE not in json.dumps(data)
    assert data["total_tokens"] == 33 and data["exact_model_shares_available"]
    assert {model["requested_model"] for model in data["models"]} == {"model:1", "model:2"}
    assert {model["role"]: model["total_tokens"] for model in data["models"]} == {"privacy": 10, "semantic": 23}
    assert sum(model["token_share_percent"] for model in data["models"]) == pytest.approx(100)
    assert all(group["response_model"] is None for group in data["breakdown"])


@pytest.mark.asyncio
async def test_schema2_publisher_uses_operational_db_branch_not_safe_display(monkeypatch):
    public = manifest()
    verified = {"PROJECT_BRAIN.md": "safe", "docs/project-brain/manifest.json": public.model_dump_json()}
    store = SimpleNamespace(read_manifest=lambda _: public, repository=lambda _: verified)
    client = MagicMock()
    client.get_branch = AsyncMock(return_value={"commit": {"id": "head"}, "protected": False})
    client.get_repository_file = AsyncMock(return_value=None)
    client.create_brain_commit = AsyncMock(return_value={"id": "commit"})
    monkeypatch.setattr("src.core.project_brain.publisher.ArtifactStore", lambda: store)
    monkeypatch.setattr("src.core.project_brain.publisher.GitLabClient", lambda **_: client)
    monkeypatch.setattr("src.core.project_brain.publisher.branch_fingerprint", AsyncMock(return_value="fp"))
    monkeypatch.setattr("src.core.project_brain.publisher.assert_tree_safe", AsyncMock())
    monkeypatch.setattr("src.core.project_brain.publisher.verify_commit", AsyncMock())
    result = await publish(public.build_id, 1, PRIVATE, {}, lambda _: None, lambda *_: True)
    assert result["status"] == "published"
    assert client.create_brain_commit.call_args.args[1] == PRIVATE
    assert all(call.args[1] == PRIVATE for call in client.get_branch.call_args_list)
    assert public.branch != PRIVATE


@pytest.mark.asyncio
async def test_remote_schema2_manifest_retains_ownership_and_human_edit_checks():
    public = manifest(files={"PROJECT_BRAIN.md": hashlib.sha256(b"old").hexdigest()})
    client = SimpleNamespace(get_repository_file=AsyncMock(side_effect=lambda pid, path, ref: {
        "decoded_content": public.model_dump_json() if path.endswith("manifest.json") else "old", "last_commit_id": "head"}))
    actions, deleted = await publication_actions(client, 1, "head", {"docs/project-brain/manifest.json": "new"}, lambda *_: True)
    assert deleted == {"PROJECT_BRAIN.md"}
    assert {action["action"] for action in actions} == {"update", "delete"}
    from src.core.project_brain.write_policy import WriteConflict
    with pytest.raises(WriteConflict):
        await publication_actions(client, 1, "head", {}, lambda *_: False)
