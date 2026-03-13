import httpx

from src.config import settings
from src.utils.logger import logger


class SonarClient:
    """SonarQube API 客户端。"""

    def __init__(self):
        self.base_url = settings.sonarqube_url.rstrip("/")
        self.auth = (settings.sonarqube_token, "") if settings.sonarqube_token else None

    async def get_project_status(self, project_key: str) -> dict:
        """获取项目 Quality Gate 状态。"""
        url = f"{self.base_url}/api/qualitygates/project_status"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params={"projectKey": project_key}, auth=self.auth, timeout=30)
            resp.raise_for_status()
            return resp.json()

    async def get_issues(self, project_key: str, branch: str | None = None, resolved: bool = False) -> list[dict]:
        """获取项目问题列表。"""
        url = f"{self.base_url}/api/issues/search"
        params = {"componentKeys": project_key, "resolved": str(resolved).lower(), "ps": 100}
        if branch:
            params["branch"] = branch
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, auth=self.auth, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data.get("issues", [])


sonar_client = SonarClient()
