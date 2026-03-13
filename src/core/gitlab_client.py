import httpx

from src.config import settings
from src.utils.logger import logger


class GitLabClient:
    """GitLab API 客户端，用于获取 MR Diff 和发布评论。"""

    def __init__(self):
        self.base_url = settings.gitlab_url.rstrip("/")
        self.headers = {"PRIVATE-TOKEN": settings.gitlab_token}

    async def get_mr_changes(self, project_id: int, mr_iid: int) -> list[dict]:
        """获取 MR 的变更文件列表。"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data.get("changes", [])

    async def post_mr_note(self, project_id: int, mr_iid: int, body: str) -> dict:
        """在 MR 上发布总结评论。"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes"
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=self.headers, json={"body": body}, timeout=30)
            resp.raise_for_status()
            return resp.json()

    async def post_mr_discussion(
        self, project_id: int, mr_iid: int, file_path: str, new_line: int, body: str, base_sha: str, head_sha: str
    ) -> dict:
        """在 MR 的具体代码行上发布行内评论。"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions"
        position = {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "start_sha": base_sha,
            "position_type": "text",
            "new_path": file_path,
            "new_line": new_line,
        }
        payload = {"body": body, "position": position}
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=self.headers, json=payload, timeout=30)
            if resp.status_code >= 400:
                logger.warning(f"行内评论发布失败: {file_path}:{new_line} - {resp.text}")
                return {}
            return resp.json()

    async def get_mr_info(self, project_id: int, mr_iid: int) -> dict:
        """获取 MR 基本信息（含 SHA）。"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            return resp.json()

    async def add_mr_label(self, project_id: int, mr_iid: int, labels: list[str]) -> dict:
        """为 MR 添加标签。"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}"
        async with httpx.AsyncClient() as client:
            resp = await client.put(url, headers=self.headers, json={"add_labels": ",".join(labels)}, timeout=30)
            resp.raise_for_status()
            return resp.json()


gitlab_client = GitLabClient()
