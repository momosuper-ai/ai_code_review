"""Initialize and inspect the isolated WSL Project Brain test environment."""

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parents[1]
_ENV = _ROOT / ".env.brain-local"
_REUSE_KEYS = {
    "GITLAB_URL", "GITLAB_TOKEN", "GITLAB_READ_TOKEN", "MAIN_BRANCHES",
    "VLLM_API_URL", "VLLM_API_KEY", "VLLM_MODEL_NAME", "CLOUD_LLM_MODEL_NAME",
    "LLM_EXTRA_BODY", "LLM_RESPONSE_FORMAT_MODE", "LLM_RESPONSE_FORMAT_MODE_CLOUD",
    "LLM_RESPONSE_FORMAT_MODE_LOCAL", "LLM_MAX_CONTEXT_TOKENS", "LLM_MAX_TOKENS",
    "PRIVACY_FILTER_ENABLED", "PRIVACY_FILTER_LEVEL", "PRIVACY_INTERNAL_DOMAINS",
    "PRIVACY_INTERNAL_CIDRS", "PRIVACY_PRE_SCAN_ENABLED", "PRIVACY_TOOL_SCAN_MODE",
    "PRIVACY_CUSTOM_PATTERNS", "PROJECT_BRAIN_MODEL_NAME",
}


def initialize(reuse_config: bool) -> None:
    """生成独立密码和认证参数，可复制读取/模型配置，绝不复制发布凭据。"""
    from dotenv import dotenv_values, set_key

    if _ENV.exists():
        raise SystemExit("本机配置已存在，保留原文件；修改配置后重新创建容器即可。")
    values = {
        "BRAIN_LOCAL_DB_PASSWORD": secrets.token_hex(24),
        "BRAIN_LOCAL_REDIS_PASSWORD": secrets.token_hex(24),
        "BRAIN_LOCAL_BIND_ADDRESS": "127.0.0.1",
        "BRAIN_LOCAL_API_PORT": "18080",
        "GITLAB_URL": "https://gitlab.example.com",
        "GITLAB_TOKEN": "",
        "GITLAB_READ_TOKEN": "",
        "GITLAB_WEBHOOK_SECRET": secrets.token_urlsafe(32),
        "API_KEYS": secrets.token_urlsafe(32),
        "MAIN_BRANCHES": "main,master",
        "VLLM_API_URL": "http://host.docker.internal:8000/v1",
        "VLLM_API_KEY": "",
        "VLLM_MODEL_NAME": "",
        "PROJECT_BRAIN_ENABLED": "true",
        "PROJECT_BRAIN_GITLAB_SYNC": "false",
        "PROJECT_BRAIN_MR_AUTO_MERGE": "false",
        "PROJECT_BRAIN_DEBOUNCE_SECONDS": "10",
        "LOG_FORMAT": "json",
        "LOG_LEVEL": "INFO",
    }
    if reuse_config:
        source = dotenv_values(_ROOT / ".env")
        values.update({key: value for key, value in source.items() if key in _REUSE_KEYS and value is not None})
    # 仅复制明确许可的配置，环境不接管现有数据库、Broker、发布身份和历史。
    with _ENV.open("x", encoding="utf-8") as stream:
        stream.write("# 本机测试配置，含凭据，不提交、不输出到日志。\n")
    _ENV.chmod(0o600)
    for key, value in values.items():
        set_key(str(_ENV), key, value)
    print("已生成 .env.brain-local；写回与审查 Worker 默认关闭，凭据未输出。")


def bootstrap() -> None:
    """只在隔离本机库建立旧表基线，然后执行真实 PostgreSQL Brain 迁移。"""
    from sqlalchemy import inspect

    from src.config import settings
    from src.models import Base, sync_engine
    from src.models.review import Review, ReviewComment
    from src.models.readme_record import ReadmeRecord

    parsed = urlsplit(settings.DATABASE_URL)
    if (os.environ.get("BRAIN_LOCAL_TEST") != "1" or parsed.hostname != "postgres"
            or parsed.path != "/obc_brain_local" or parsed.username != "brain_local"):
        raise SystemExit("拒绝初始化：此命令仅用于 compose.brain-local.yml 的独立测试库。")
    tables = set(inspect(sync_engine).get_table_names())
    if not tables:
        # 001/002 是旧系统的增量迁移，依赖已有 reviews 表。本机空库以当前
        # 旧 ORM 建立等价 004 基线，仅创建三张旧表，不抢建 Brain 表。
        Base.metadata.create_all(sync_engine, tables=[Review.__table__, ReviewComment.__table__, ReadmeRecord.__table__])
        subprocess.run(["alembic", "stamp", "004_readme_unique"], check=True)
    elif "alembic_version" not in tables:
        raise SystemExit("测试库已有未追踪表，拒绝自动 stamp；请核对本机测试库。")
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    subprocess.run(["alembic", "current"], check=True)
    print("本机 PostgreSQL 基线和 Brain 迁移完成。")


def api_request(path: str) -> None:
    """认证查询本机 API；从本地文件/容器环境取密钥，不放在命令参数中。"""
    from dotenv import dotenv_values

    values = {**dotenv_values(_ENV), **os.environ}
    if not path.startswith("/") or path.startswith("//"):
        raise SystemExit("API 路径必须为本机相对路径。")
    address = "http://api:8000" if os.environ.get("BRAIN_LOCAL_TEST") == "1" else f"http://127.0.0.1:{values.get('BRAIN_LOCAL_API_PORT', '18080')}"
    key = str(values.get("API_KEYS", "")).split(",")[0]
    request = Request(address + path, headers={"X-API-Key": key})
    with urlopen(request, timeout=15) as response:
        data = response.read()
    print(json.dumps(json.loads(data), ensure_ascii=False, indent=2))


def main() -> None:
    """本机测试命令入口。"""
    parser = argparse.ArgumentParser(description="Project Brain 独立本机测试辅助，不输出凭据")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="生成本机测试配置")
    init.add_argument("--reuse-config", action="store_true", help="沿用现有读取/评论及模型配置，不复制发布身份")
    commands.add_parser("bootstrap", help="仅初始化隔离测试库")
    api = commands.add_parser("api", help="认证查询本机 API")
    api.add_argument("path")
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.reuse_config)
    elif args.command == "bootstrap":
        bootstrap()
    else:
        api_request(args.path)


if __name__ == "__main__":
    main()
