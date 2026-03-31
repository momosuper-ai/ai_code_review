from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres@postgres:5432/obc_review"

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # GitLab
    GITLAB_URL: str = "https://gitlab.example.com/"
    GITLAB_TOKEN: str = ""
    GITLAB_WEBHOOK_SECRET: str = ""

    # vLLM
    VLLM_API_URL: str = "http://localhost:8000/v1"
    VLLM_API_KEY: str = ""
    VLLM_MODEL_NAME: str = "mistralai/Devstral-Small-2-24B-Instruct-2512"

    # App
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "text"  # "text" | "json"
    LOG_FILE: str = ""  # 留空则不写文件，如 "/data/logs/review.log"
    LOG_FILE_MAX_BYTES: int = 50_000_000  # 50MB per file
    LOG_FILE_BACKUP_COUNT: int = 5  # 保留 5 个轮转文件

    # Repo management
    REPOS_BASE_DIR: str = "/data/repos"

    # Agent
    AGENT_MAX_TOOL_ROUNDS: int = 10
    AGENT_FORMAT_RETRY_MAX: int = 3

    # Main branches to trigger review
    MAIN_BRANCHES: str = "main,master"

    # API Key 认证（逗号分隔，空值 = 开发模式免认证）
    API_KEYS: str = ""

    # 速率限制
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_WEBHOOK: str = "30/minute"
    RATE_LIMIT_REVIEWS: str = "60/minute"
    RATE_LIMIT_DEFAULT: str = "120/minute"

    # IP 白名单（逗号分隔，支持单 IP 和 CIDR，空值 = 开发模式免限制）
    IP_WHITELIST: str = ""

    @property
    def DATABASE_URL_SYNC(self) -> str:
        """同步数据库连接 URL，供 Celery 任务使用。"""
        return self.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")

    @property
    def main_branch_list(self) -> list[str]:
        return [b.strip() for b in self.MAIN_BRANCHES.split(",") if b.strip()]

    @property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.API_KEYS.split(",") if k.strip()]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
