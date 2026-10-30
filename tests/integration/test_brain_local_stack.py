"""Opt-in API, broker and actual Celery worker checks in the local stack."""

import os

import httpx
import pytest
from sqlalchemy import func, select

from src.config import settings
from src.models import SyncSession
from src.models.brain import BrainProject
from src.tasks.celery_app import celery_app

pytestmark = pytest.mark.skipif(
    os.environ.get("BRAIN_LOCAL_TEST") != "1" or os.environ.get("BRAIN_LOCAL_STACK") != "1",
    reason="需已启动隔离本机 API/Worker，并显式启用 BRAIN_LOCAL_STACK",
)


def test_local_health_and_swagger() -> None:
    """实际 API 可以连接 PostgreSQL 并公开 Brain 查询契约。"""
    with httpx.Client(base_url="http://api:8000", timeout=10) as client:
        assert client.get("/health").json() == {"status": "ok"}
        schema = client.get("/openapi.json").json()
        assert "/api/v1/projects/{project_id}/brain/builds" in schema["paths"]


def test_local_api_and_webhook_authentication() -> None:
    """认证必须真实生效，非法事件不能登记请求。"""
    with httpx.Client(base_url="http://api:8000", timeout=10) as client:
        assert client.get("/api/v1/projects/1/brain", params={"branch": "main"}).status_code == 401
        authorized = client.get("/api/v1/projects/1/brain", params={"branch": "main"}, headers={"X-API-Key": settings.API_KEYS.split(",")[0]})
        assert authorized.status_code == 404
        assert client.post("/api/v1/webhooks/gitlab", json={"object_kind": "push"}).status_code == 401
        valid = client.post("/api/v1/webhooks/gitlab", json={"object_kind": "tag_push"}, headers={"X-Gitlab-Token": settings.GITLAB_WEBHOOK_SECRET})
        assert valid.status_code == 200 and valid.json()["status"] == "ignored"


def test_local_broker_and_real_brain_worker() -> None:
    """通过真实 Redis 投递并发恢复任务，验证 Worker 与 PostgreSQL 的连接。"""
    with SyncSession() as session:
        if session.scalar(select(func.count()).select_from(BrainProject)):
            pytest.skip("已有真实项目，避免对试点重复安排恢复任务")
    requests = [celery_app.send_task("src.tasks.brain_task.recover_brain", queue="brain") for _ in range(4)]
    try:
        outcomes = [request.get(timeout=45) for request in requests]
        assert all(isinstance(outcome, dict) for outcome in outcomes)
    finally:
        for request in requests:
            request.forget()
