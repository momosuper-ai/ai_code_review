"""IP whitelist middleware for FastAPI."""

import ipaddress
from ipaddress import IPv4Network, IPv6Network

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.utils.logger import logger


def _parse_whitelist(raw: str) -> list[IPv4Network | IPv6Network]:
    """解析逗号分隔的 IP/CIDR 字符串为 ip_network 列表。"""
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("IP 白名单配置无效，已跳过: %s", item)
    return networks


def _is_allowed(client_ip: str, networks: list[IPv4Network | IPv6Network]) -> bool:
    """检查客户端 IP 是否匹配任意白名单网段。"""
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(addr in network for network in networks)


class IPWhitelistMiddleware(BaseHTTPMiddleware):
    """IP 白名单中间件：白名单为空时全部放行（开发模式），否则仅允许白名单 IP 访问。"""

    def __init__(self, app, whitelist_raw: str = ""):
        super().__init__(app)
        self.networks = _parse_whitelist(whitelist_raw)
        if self.networks:
            logger.info(
                "IP 白名单已启用，允许网段: %s",
                ", ".join(str(n) for n in self.networks),
            )
        else:
            logger.warning("IP 白名单未配置，所有 IP 均可访问（开发模式）")

    async def dispatch(self, request: Request, call_next):
        # /health 始终放行（容器健康检查）
        if request.url.path == "/health":
            return await call_next(request)

        # 白名单为空时全部放行
        if not self.networks:
            return await call_next(request)

        client_ip = request.client.host if request.client else ""
        if not _is_allowed(client_ip, self.networks):
            logger.warning("IP 白名单拒绝访问: %s %s (来源 IP: %s)", request.method, request.url.path, client_ip)
            return JSONResponse(
                status_code=403,
                content={"detail": "Forbidden: IP not in whitelist"},
            )

        return await call_next(request)
