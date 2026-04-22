"""Redis-based fixed-window rate limiter for FastAPI."""

import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from src.config import settings
from src.utils.logger import logger
from src.utils.redis_client import get_redis

_WINDOW_UNITS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
}


@dataclass(frozen=True)
class RateLimit:
    max_requests: int
    window_seconds: int


def parse_rate_limit(spec: str) -> RateLimit:
    """解析速率限制字符串，如 '30/minute'。"""
    parts = spec.split("/", 1)
    max_requests = int(parts[0])
    unit = parts[1].lower()
    if unit not in _WINDOW_UNITS:
        raise ValueError(f"不支持的时间单位: {unit}，可选: {list(_WINDOW_UNITS)}")
    return RateLimit(max_requests=max_requests, window_seconds=_WINDOW_UNITS[unit])


def check_rate_limit(key: str, limit: RateLimit) -> tuple[bool, int, int]:
    """检查速率限制，返回 (allowed, remaining, retry_after)。"""
    try:
        r = get_redis()
        window_id = int(time.time()) // limit.window_seconds
        redis_key = f"ratelimit:{key}:{window_id}"

        current = r.incr(redis_key)
        if current == 1:
            r.expire(redis_key, limit.window_seconds)

        remaining = max(0, limit.max_requests - current)
        if current > limit.max_requests:
            elapsed = int(time.time()) % limit.window_seconds
            retry_after = limit.window_seconds - elapsed
            return False, 0, retry_after
        return True, remaining, 0
    except Exception:
        logger.warning("Redis 速率限制检查失败，放行请求（fail-open）")
        return True, limit.max_requests, 0


def rate_limit_dependency(limit_spec: str):
    """工厂函数，返回 FastAPI 依赖，用于速率限制。"""
    limit = parse_rate_limit(limit_spec)

    async def _check(request: Request):
        if not settings.RATE_LIMIT_ENABLED:
            return

        client_ip = request.client.host if request.client else "unknown"
        path = request.url.path
        key = f"{client_ip}:{path}"

        allowed, remaining, retry_after = check_rate_limit(key, limit)
        if not allowed:
            logger.warning(
                "速率限制触发: 客户端=%s 路径=%s 限制=%s",
                client_ip, path, limit_spec,
            )
            raise HTTPException(
                status_code=429,
                detail="Too many requests",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit.max_requests),
                    "X-RateLimit-Remaining": str(remaining),
                },
            )

    return _check
