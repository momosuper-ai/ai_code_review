import time

import httpx

from src.config import settings
from src.utils.logger import logger

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class GitLabClient:
    def __init__(self):
        base = settings.GITLAB_URL.rstrip("/")
        self._base = f"{base}/api/v4"
        self._headers = {"PRIVATE-TOKEN": settings.GITLAB_TOKEN}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base,
            headers=self._headers,
            timeout=_TIMEOUT,
        )

    async def get_mr_changes(self, project_id: int, mr_iid: int) -> dict:
        """GET /projects/:id/merge_requests/:iid/changes"""
        async with self._client() as c:
            t0 = time.time()
            resp = await c.get(
                f"/projects/{project_id}/merge_requests/{mr_iid}/changes"
            )
            duration_ms = (time.time() - t0) * 1000
            logger.info(
                "GitLab API: GET 合并请求变更 项目=%d MR=%d -> 状态码 %d (%.0f毫秒)",
                project_id, mr_iid, resp.status_code, duration_ms,
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "GitLab API 失败: GET 合并请求变更 项目=%d MR=%d 状态码=%d 响应=%s",
                    project_id, mr_iid, resp.status_code, resp.text[:500],
                )
                raise exc
            return resp.json()

    async def get_compare(
        self, project_id: int, from_sha: str, to_sha: str
    ) -> dict:
        """GET /projects/:id/repository/compare"""
        async with self._client() as c:
            t0 = time.time()
            resp = await c.get(
                f"/projects/{project_id}/repository/compare",
                params={"from": from_sha, "to": to_sha},
            )
            duration_ms = (time.time() - t0) * 1000
            logger.info(
                "GitLab API: GET 提交比较 项目=%d %s..%s -> 状态码 %d (%.0f毫秒)",
                project_id, from_sha[:8], to_sha[:8], resp.status_code, duration_ms,
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "GitLab API 失败: GET 提交比较 项目=%d 状态码=%d 响应=%s",
                    project_id, resp.status_code, resp.text[:500],
                )
                raise exc
            return resp.json()

    async def post_mr_note(
        self, project_id: int, mr_iid: int, body: str
    ) -> dict:
        """POST /projects/:id/merge_requests/:iid/notes"""
        async with self._client() as c:
            t0 = time.time()
            resp = await c.post(
                f"/projects/{project_id}/merge_requests/{mr_iid}/notes",
                json={"body": body},
            )
            duration_ms = (time.time() - t0) * 1000
            logger.info(
                "GitLab API: POST MR评论 项目=%d MR=%d -> 状态码 %d (%.0f毫秒)",
                project_id, mr_iid, resp.status_code, duration_ms,
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "GitLab API 失败: POST MR评论 项目=%d MR=%d 状态码=%d 响应=%s",
                    project_id, mr_iid, resp.status_code, resp.text[:500],
                )
                raise exc
            return resp.json()

    async def post_commit_comment(
        self, project_id: int, sha: str, note: str
    ) -> dict:
        """POST /projects/:id/repository/commits/:sha/comments"""
        async with self._client() as c:
            t0 = time.time()
            resp = await c.post(
                f"/projects/{project_id}/repository/commits/{sha}/comments",
                json={"note": note},
            )
            duration_ms = (time.time() - t0) * 1000
            logger.info(
                "GitLab API: POST 提交评论 项目=%d 提交=%s -> 状态码 %d (%.0f毫秒)",
                project_id, sha[:8], resp.status_code, duration_ms,
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "GitLab API 失败: POST 提交评论 项目=%d 提交=%s 状态码=%d 响应=%s",
                    project_id, sha[:8], resp.status_code, resp.text[:500],
                )
                raise exc
            return resp.json()

    async def get_project(self, project_id: int) -> dict:
        """GET /projects/:id — used to get clone url."""
        async with self._client() as c:
            t0 = time.time()
            resp = await c.get(f"/projects/{project_id}")
            duration_ms = (time.time() - t0) * 1000
            logger.info(
                "GitLab API: GET 项目信息 项目=%d -> 状态码 %d (%.0f毫秒)",
                project_id, resp.status_code, duration_ms,
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "GitLab API 失败: GET 项目信息 项目=%d 状态码=%d 响应=%s",
                    project_id, resp.status_code, resp.text[:500],
                )
                raise exc
            return resp.json()
