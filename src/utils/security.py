from fastapi import Header, HTTPException

from src.config import settings
from src.utils.logger import logger


def verify_gitlab_token(token: str | None) -> bool:
    """Verify the X-Gitlab-Token header matches the configured secret."""
    if not settings.GITLAB_WEBHOOK_SECRET:
        return True
    return token == settings.GITLAB_WEBHOOK_SECRET


def verify_api_key(api_key: str | None) -> bool:
    """校验 API Key，空配置时放行（开发模式）。"""
    if not settings.api_key_list:
        return True
    if not api_key:
        return False
    return api_key in settings.api_key_list


async def require_api_key(x_api_key: str | None = Header(None)) -> str | None:
    """FastAPI 依赖：要求有效的 API Key，否则返回 401。"""
    if not verify_api_key(x_api_key):
        logger.warning(
            "API Key 验证失败: 密钥=%s",
            x_api_key[:8] + "..." if x_api_key else "无",
        )
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return x_api_key
