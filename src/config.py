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

    # LLM 调用重试
    LLM_RETRY_MAX: int = 3                    # 最大重试次数
    LLM_RETRY_BASE_DELAY: float = 2.0         # 基础退避秒数
    LLM_RETRY_MAX_DELAY: float = 30.0         # 最大退避秒数

    # 上下文长度管理
    LLM_MAX_CONTEXT_TOKENS: int = 256000       # 模型最大上下文 token 数
    LLM_CONTEXT_THRESHOLD: float = 0.9         # 触发裁剪的阈值（0.9 = 230,400 tokens）
    LLM_CHARS_PER_TOKEN: float = 3.0           # 字符/token 估算比（中英混合+代码）

    # 多Agent并行审查
    REVIEW_DIMENSIONS_ENABLED: str = "bug,security,performance,quality,architecture"
    REVIEW_MAX_CONCURRENCY: int = 3
    REVIEW_DIMENSION_MAX_ROUNDS: int = 6
    REVIEW_DIMENSION_TIMEOUT: int = 180
    REVIEW_SINGLE_AGENT_MODE: bool = False

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

    # 云端审查模型（非空则启用双模型模式，与 VLLM_API_URL 共用同一中转服务）
    CLOUD_LLM_MODEL_NAME: str = ""

    # 隐私过滤
    PRIVACY_FILTER_ENABLED: bool = False
    PRIVACY_FILTER_LEVEL: str = "standard"       # minimal / standard / strict
    PRIVACY_INTERNAL_DOMAINS: str = ""           # 逗号分隔的内部域名
    PRIVACY_INTERNAL_CIDRS: str = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    PRIVACY_PRE_SCAN_ENABLED: bool = True        # 是否用本地模型做 diff 预扫描
    PRIVACY_TOOL_SCAN_MODE: str = "hybrid"       # hybrid=规则+本地模型 / rules_only=仅规则
    PRIVACY_CUSTOM_PATTERNS: str = ""            # 额外自定义正则（JSON 格式）

    # 代码图（影响面分析）
    CODE_GRAPH_ENABLED: bool = True
    CODE_GRAPH_LANGUAGES: str = "python,javascript,typescript,java,go"
    CODE_GRAPH_MAX_FILES_FULL_SCAN: int = 5000

    # SonarQube（预留）
    SONARQUBE_URL: str = ""
    SONARQUBE_TOKEN: str = ""

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

    @property
    def enabled_dimensions(self) -> list[str]:
        return [d.strip() for d in self.REVIEW_DIMENSIONS_ENABLED.split(",") if d.strip()]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
