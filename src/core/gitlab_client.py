import time
from urllib.parse import quote

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
            logger.info("审查结果: %s", note)
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

    _ALLOWED_COMMIT_FILENAME = "AI_README.md"

    async def file_exists(
        self, project_id: int, file_path: str, ref: str,
    ) -> bool:
        """HEAD /projects/:id/repository/files/:file_path?ref=:ref

        判断文件是否已存在于指定分支/ref。404 返回 False，2xx 返回 True。
        其他错误（网络、权限等）向上抛出。
        """
        encoded_path = quote(file_path, safe="")
        async with self._client() as c:
            t0 = time.time()
            resp = await c.head(
                f"/projects/{project_id}/repository/files/{encoded_path}",
                params={"ref": ref},
            )
            duration_ms = (time.time() - t0) * 1000
            logger.debug(
                "GitLab API: HEAD 文件存在性 项目=%d 路径=%s ref=%s -> 状态码 %d (%.0f毫秒)",
                project_id, file_path, ref, resp.status_code, duration_ms,
            )
            if resp.status_code == 404:
                return False
            if 200 <= resp.status_code < 300:
                return True
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "GitLab API 失败: HEAD 文件存在性 项目=%d 路径=%s 状态码=%d 响应=%s",
                    project_id, file_path, resp.status_code, resp.text[:500],
                )
                raise exc
            return False

    @staticmethod
    def _try_flip_actions(
        actions: list[dict], error_text: str,
    ) -> list[dict] | None:
        """根据 GitLab 错误消息翻转 action 类型。

        - "A file with this name already exists" → create 翻转为 update
        - "A file with this name doesn't exist"  → update 翻转为 create
        返回新的 actions 列表；若错误消息不匹配或无需翻转则返回 None。
        """
        low = error_text.lower()
        if "already exists" in low:
            flipped = [
                {**a, "action": "update"} if a.get("action") == "create" else a
                for a in actions
            ]
        elif "doesn't exist" in low or "does not exist" in low:
            flipped = [
                {**a, "action": "create"} if a.get("action") == "update" else a
                for a in actions
            ]
        else:
            return None
        return flipped if flipped != actions else None

    async def _post_commit(
        self,
        client: httpx.AsyncClient,
        project_id: int,
        branch: str,
        commit_message: str,
        actions: list[dict],
        author_name: str,
        author_email: str,
    ) -> httpx.Response:
        """发送一次 POST /repository/commits 请求，返回原始响应对象。"""
        t0 = time.time()
        payload: dict = {
            "branch": branch,
            "commit_message": commit_message,
            "actions": actions,
        }
        if author_name:
            payload["author_name"] = author_name
        if author_email:
            payload["author_email"] = author_email
        resp = await client.post(
            f"/projects/{project_id}/repository/commits",
            json=payload,
        )
        duration_ms = (time.time() - t0) * 1000
        logger.info(
            "GitLab API: POST 批量提交 项目=%d 分支=%s 文件数=%d -> 状态码 %d (%.0f毫秒)",
            project_id, branch, len(actions), resp.status_code, duration_ms,
        )
        return resp

    async def create_commit(
        self,
        project_id: int,
        branch: str,
        commit_message: str,
        actions: list[dict],
        author_name: str = "",
        author_email: str = "",
    ) -> dict:
        """POST /projects/:id/repository/commits — 批量创建/更新文件。

        actions 格式: [{"action": "create"|"update", "file_path": "...", "content": "..."}]

        安全限制：仅允许提交文件名为 AI_README.md 的文件，
        任何不匹配的文件路径将被拒绝并抛出 ValueError。

        兜底：若 GitLab 返回 400 且错误消息指示 create/update 类型不匹配
        （例如 "A file with this name already exists"），会自动翻转 action 重试一次。
        """
        # 安全校验：只允许提交 AI_README.md 文件
        for act in actions:
            file_path = act.get("file_path", "")
            filename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
            if filename != self._ALLOWED_COMMIT_FILENAME:
                raise ValueError(
                    f"安全限制：仅允许提交 {self._ALLOWED_COMMIT_FILENAME} 文件，"
                    f"拒绝提交: {file_path}"
                )

        async with self._client() as c:
            resp = await self._post_commit(
                c, project_id, branch, commit_message, actions,
                author_name, author_email,
            )
            # 400 fallback：尝试翻转 action 类型重试一次
            if resp.status_code == 400:
                flipped = self._try_flip_actions(actions, resp.text)
                if flipped is not None:
                    logger.warning(
                        "GitLab 批量提交 400 (%s)，翻转 action 类型后重试",
                        resp.text[:200],
                    )
                    resp = await self._post_commit(
                        c, project_id, branch, commit_message, flipped,
                        author_name, author_email,
                    )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "GitLab API 失败: POST 批量提交 项目=%d 状态码=%d 响应=%s",
                    project_id, resp.status_code, resp.text[:500],
                )
                raise exc
            return resp.json()

    async def get_branch(self, project_id: int, branch: str) -> dict:
        """GET /projects/:id/repository/branches/:branch"""
        encoded = quote(branch, safe="")
        async with self._client() as c:
            t0 = time.time()
            resp = await c.get(
                f"/projects/{project_id}/repository/branches/{encoded}"
            )
            duration_ms = (time.time() - t0) * 1000
            logger.info(
                "GitLab API: GET 分支信息 项目=%d 分支=%s -> 状态码 %d (%.0f毫秒)",
                project_id, branch, resp.status_code, duration_ms,
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "GitLab API 失败: GET 分支信息 项目=%d 分支=%s 状态码=%d 响应=%s",
                    project_id, branch, resp.status_code, resp.text[:500],
                )
                raise exc
            return resp.json()

    async def create_branch(
        self, project_id: int, branch: str, ref: str,
    ) -> dict:
        """POST /projects/:id/repository/branches?branch=...&ref=...

        幂等：若分支已存在（400 + "already exists"），返回空 dict + info 日志。
        """
        async with self._client() as c:
            t0 = time.time()
            resp = await c.post(
                f"/projects/{project_id}/repository/branches",
                params={"branch": branch, "ref": ref},
            )
            duration_ms = (time.time() - t0) * 1000
            logger.info(
                "GitLab API: POST 创建分支 项目=%d 分支=%s ref=%s -> 状态码 %d (%.0f毫秒)",
                project_id, branch, ref, resp.status_code, duration_ms,
            )
            if resp.status_code == 400 and "already exists" in resp.text.lower():
                logger.info(
                    "GitLab API: 分支已存在，视为幂等: 项目=%d 分支=%s",
                    project_id, branch,
                )
                return {}
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "GitLab API 失败: POST 创建分支 项目=%d 分支=%s 状态码=%d 响应=%s",
                    project_id, branch, resp.status_code, resp.text[:500],
                )
                raise exc
            return resp.json()

    async def delete_branch(self, project_id: int, branch: str) -> None:
        """DELETE /projects/:id/repository/branches/:branch

        清理失败不抛异常，仅记录 warning。
        """
        encoded = quote(branch, safe="")
        async with self._client() as c:
            t0 = time.time()
            try:
                resp = await c.delete(
                    f"/projects/{project_id}/repository/branches/{encoded}"
                )
            except Exception:
                logger.warning(
                    "GitLab API: DELETE 分支网络异常 项目=%d 分支=%s",
                    project_id, branch, exc_info=True,
                )
                return
            duration_ms = (time.time() - t0) * 1000
            logger.info(
                "GitLab API: DELETE 分支 项目=%d 分支=%s -> 状态码 %d (%.0f毫秒)",
                project_id, branch, resp.status_code, duration_ms,
            )
            if resp.status_code >= 300:
                logger.warning(
                    "GitLab API: DELETE 分支失败 项目=%d 分支=%s 状态码=%d 响应=%s",
                    project_id, branch, resp.status_code, resp.text[:300],
                )

    async def create_merge_request(
        self,
        project_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
        remove_source_branch: bool = True,
        squash: bool = True,
    ) -> dict:
        """POST /projects/:id/merge_requests"""
        payload = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "remove_source_branch": remove_source_branch,
            "squash": squash,
        }
        async with self._client() as c:
            t0 = time.time()
            resp = await c.post(
                f"/projects/{project_id}/merge_requests",
                json=payload,
            )
            duration_ms = (time.time() - t0) * 1000
            logger.info(
                "GitLab API: POST 创建合并请求 项目=%d %s->%s -> 状态码 %d (%.0f毫秒)",
                project_id, source_branch, target_branch,
                resp.status_code, duration_ms,
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "GitLab API 失败: POST 创建合并请求 项目=%d 状态码=%d 响应=%s",
                    project_id, resp.status_code, resp.text[:500],
                )
                raise exc
            return resp.json()

    async def accept_merge_request(
        self,
        project_id: int,
        mr_iid: int,
        should_remove_source_branch: bool = True,
        merge_when_pipeline_succeeds: bool = False,
    ) -> dict:
        """PUT /projects/:id/merge_requests/:iid/merge

        405/406/409（冲突、未就绪、需审批）仅 warning 并返回空 dict，由上层决定降级。
        """
        payload = {
            "should_remove_source_branch": should_remove_source_branch,
            "merge_when_pipeline_succeeds": merge_when_pipeline_succeeds,
        }
        async with self._client() as c:
            t0 = time.time()
            resp = await c.put(
                f"/projects/{project_id}/merge_requests/{mr_iid}/merge",
                json=payload,
            )
            duration_ms = (time.time() - t0) * 1000
            logger.info(
                "GitLab API: PUT 接受合并请求 项目=%d MR=%d -> 状态码 %d (%.0f毫秒)",
                project_id, mr_iid, resp.status_code, duration_ms,
            )
            if resp.status_code in (405, 406, 409):
                logger.warning(
                    "GitLab API: 自动合并未就绪 项目=%d MR=%d 状态码=%d 响应=%s",
                    project_id, mr_iid, resp.status_code, resp.text[:300],
                )
                return {}
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "GitLab API 失败: PUT 接受合并请求 项目=%d MR=%d 状态码=%d 响应=%s",
                    project_id, mr_iid, resp.status_code, resp.text[:500],
                )
                raise exc
            return resp.json()
