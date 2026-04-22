"""Shared synchronous Redis client singleton.

抽取自 rate_limit.py 的惰性初始化模式，供 webhook 抑制、幂等标记等场景复用。
故障时调用方应自行处理（通常 fail-open）。
"""

from src.config import settings

_redis_client = None


def get_redis():
    """惰性初始化同步 Redis 客户端（单例）。"""
    global _redis_client
    if _redis_client is None:
        import redis

        _redis_client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _redis_client
