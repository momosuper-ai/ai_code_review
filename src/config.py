import json
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres@postgres:5432/obc_review"

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # GitLab
    GITLAB_URL: str = "https://gitlab.example.com/"
    GITLAB_TOKEN: str = ""
    GITLAB_READ_TOKEN: str = ""
    GITLAB_BRAIN_PUBLISH_TOKEN_FILE: str = ""
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

    # LLM 最大输出 token（chat() max_tokens=None 时使用此默认值）
    # 8192 兼顾大多数云端模型默认上限；过低会导致长 JSON 被截断 → 解析失败
    LLM_MAX_TOKENS: int = 256000

    # LLM 扩展参数透传（JSON 字符串，原样作为 OpenAI SDK 的 extra_body 下发）
    # 用于主动控制 thinking/reasoning 等模型特性。示例：
    #   关闭 DeepSeek thinking:   '{"thinking": {"type": "disabled"}}'
    #   启用预算型 thinking:       '{"thinking": {"type": "enabled", "budget_tokens": 2048}}'
    #   OpenAI reasoning_effort:  '{"reasoning_effort": "low"}'
    LLM_EXTRA_BODY: str = ""

    # LLM 结构化输出（response_format 协议层约束）
    # 全局模式：json_schema(最强) / json_object(默认，最兼容) / off(关闭，仅 prompt 约束)
    LLM_RESPONSE_FORMAT_MODE: str = "json_object"
    # 云端模型独立覆盖（空 = 跟随全局）；DeepSeek 系仅支持 json_object
    LLM_RESPONSE_FORMAT_MODE_CLOUD: str = ""
    # 本地 vLLM 独立覆盖（空 = 跟随全局）；vLLM 0.8.5+/Qwen 系可设 json_schema
    LLM_RESPONSE_FORMAT_MODE_LOCAL: str = ""

    # 上下文长度管理
    LLM_MAX_CONTEXT_TOKENS: int = 256000       # 模型最大上下文 token 数
    LLM_CONTEXT_THRESHOLD: float = 0.9         # 触发裁剪的阈值（0.9 = 230,400 tokens）
    LLM_CHARS_PER_TOKEN: float = 3.0           # 字符/token 估算比（中英混合+代码）

    # 多Agent并行审查
    REVIEW_DIMENSIONS_ENABLED: str = "bug,security,performance,quality,architecture"
    REVIEW_MAX_CONCURRENCY: int = 3
    REVIEW_DIMENSION_MAX_ROUNDS: int = 6
    REVIEW_DIMENSION_TIMEOUT: int = 300
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

    # 影响面预注入
    BLAST_RADIUS_PRE_INJECT: bool = True    # 审查开始前预注入影响面分析到 prompt
    BLAST_RADIUS_MAX_ITEMS: int = 30        # 预注入的最大符号数量
    BLAST_RADIUS_MAX_CHARS: int = 4000      # 预注入文本的最大字符数（~1300 tokens）

    # AI_README 文档自动生成
    README_GENERATION_ENABLED: bool = False
    README_MIN_CODE_LINES: int = 200
    README_LLM_MAX_TOKENS: int = 4096
    README_AGENT_MAX_ROUNDS: int = 8
    README_MAX_SUBDIRS: int = 15
    README_CONCURRENCY: int = 2
    README_COMMIT_MESSAGE_PREFIX: str = "[AI-README]"
    README_AUTHOR_NAME: str = "AI-README Bot"
    README_AUTHOR_EMAIL: str = "ai-readme@noreply.local"
    README_TEMP_BRANCH_PREFIX: str = "ai-readme/"
    README_MR_TITLE_PREFIX: str = "[AI-README]"
    README_MR_AUTO_MERGE: bool = True
    README_MR_AUTO_MERGE_REMOVE_BRANCH: bool = True
    README_MR_DESCRIPTION: str = "Automated AI_README.md update. Safe to merge."

    # Project Brain（OpenViking 和审查消费在后续阶段接入）
    PROJECT_BRAIN_ENABLED: bool = False
    PROJECT_BRAIN_BASE_DIR: str = "/data/project_brain"
    PROJECT_BRAIN_WORK_DIR: str = ""
    PROJECT_BRAIN_MODEL_NAME: str = ""
    PROJECT_BRAIN_CONCURRENCY: int = Field(2, ge=1, le=16)
    PROJECT_BRAIN_PACKET_MAX_TOKENS: int = Field(32000, ge=2048)
    PROJECT_BRAIN_LLM_MAX_TOKENS: int = Field(4096, ge=256)
    PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS: int = Field(16384, ge=256)
    PROJECT_BRAIN_PRIVACY_PACKET_MAX_TOKENS: int = Field(6000, ge=1024)
    PROJECT_BRAIN_PRIVACY_MAX_FIELD_TOKENS: int = Field(32000, ge=1024)
    PROJECT_BRAIN_PRIVACY_MAX_RECORDS_PER_PACKET: int = Field(100, ge=1, le=100)
    PROJECT_BRAIN_PRIVACY_LLM_MAX_TOKENS: int = Field(4096, ge=256)
    PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT: int = Field(8, ge=1, le=16)
    PROJECT_BRAIN_PRIVACY_OUTPUT_MAX_CALLS_PER_UNIT: int = Field(64, ge=1, le=128)
    PROJECT_BRAIN_PRIVACY_CALL_TIMEOUT: int = Field(60, ge=5, le=120)
    PROJECT_BRAIN_MODEL_HTTP_RETRIES: int = Field(2, ge=0, le=4)
    PROJECT_BRAIN_PRIVACY_EXTRA_BODY: str = ""
    PROJECT_BRAIN_SEMANTIC_EXTRA_BODY: str = ""
    PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE: Literal["auto", "strict_tool", "source_bound"] = "auto"
    PROJECT_BRAIN_MAX_EXPLANATION_SLOTS: int = Field(24, ge=1, le=64)
    PROJECT_BRAIN_MAX_BUILD_TOKENS: int = Field(500000, ge=4096)
    PROJECT_BRAIN_MAX_SOURCE_FILES: int = Field(20000, ge=1)
    PROJECT_BRAIN_UNIT_TRANSIENT_RETRIES: int = Field(3, ge=0, le=6)
    PROJECT_BRAIN_TRANSIENT_BACKOFF_SECONDS: int = Field(60, ge=5, le=600)
    PROJECT_BRAIN_BREAKER_THRESHOLD: int = Field(6, ge=1, le=100)
    PROJECT_BRAIN_BREAKER_WINDOW_SECONDS: int = Field(600, ge=30)
    PROJECT_BRAIN_BREAKER_COOLDOWN_SECONDS: int = Field(600, ge=30)
    PROJECT_BRAIN_BREAKER_MAX_TRIPS: int = Field(3, ge=1, le=10)
    PROJECT_BRAIN_DEBOUNCE_SECONDS: int = Field(60, ge=0, le=300)
    PROJECT_BRAIN_RECONCILE_SECONDS: int = Field(300, ge=30)
    PROJECT_BRAIN_GITLAB_SYNC: bool = True
    PROJECT_BRAIN_MR_AUTO_MERGE: bool = True
    PROJECT_BRAIN_TEMP_BRANCH_PREFIX: str = "ai-brain/"
    PROJECT_BRAIN_RETENTION_BUILDS: int = Field(10, ge=1)

    @property
    def brain_model_name(self) -> str:
        """返回知识生成使用的模型，沿用审查的双模型选择规则。"""
        if self.PROJECT_BRAIN_MODEL_NAME:
            return self.PROJECT_BRAIN_MODEL_NAME
        if self.PRIVACY_FILTER_ENABLED and self.CLOUD_LLM_MODEL_NAME:
            return self.CLOUD_LLM_MODEL_NAME
        return self.VLLM_MODEL_NAME

    @field_validator("PROJECT_BRAIN_PRIVACY_EXTRA_BODY")
    @classmethod
    def validate_brain_privacy_extra_body(cls, value: str) -> str:
        """Fail closed on malformed privacy-model protocol parameters."""
        if value.strip():
            try:
                body = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("知识隐私模型扩展参数必须为 JSON 对象") from exc
            if not isinstance(body, dict):
                raise ValueError("知识隐私模型扩展参数必须为 JSON 对象")
        return value

    @field_validator("PROJECT_BRAIN_SEMANTIC_EXTRA_BODY")
    @classmethod
    def validate_brain_semantic_extra_body(cls, value: str) -> str:
        """Validate parameters scoped to Brain semantic compilation."""
        if value.strip():
            try:
                body = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("知识语义模型扩展参数必须为 JSON 对象") from exc
            if not isinstance(body, dict):
                raise ValueError("知识语义模型扩展参数必须为 JSON 对象")
        return value

    @model_validator(mode="after")
    def validate_brain_capacity_limits(self) -> "Settings":
        """Validate independent batch, complete-field and retry ceilings."""
        if self.PROJECT_BRAIN_PRIVACY_MAX_FIELD_TOKENS < self.PROJECT_BRAIN_PRIVACY_PACKET_MAX_TOKENS:
            raise ValueError("知识隐私单字段上限不能小于普通包上限")
        if self.PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS < self.PROJECT_BRAIN_LLM_MAX_TOKENS:
            raise ValueError("知识语义恢复上限不能小于初始输出上限")
        return self

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

    def response_format_mode_for(self, model_name: str | None) -> str:
        """根据模型名返回 response_format 模式。云端走 cloud override，本地走 local override，最后兜底全局。"""
        if not model_name:
            return self.LLM_RESPONSE_FORMAT_MODE
        if self.CLOUD_LLM_MODEL_NAME and model_name == self.CLOUD_LLM_MODEL_NAME:
            return self.LLM_RESPONSE_FORMAT_MODE_CLOUD or self.LLM_RESPONSE_FORMAT_MODE
        if model_name == self.VLLM_MODEL_NAME:
            return self.LLM_RESPONSE_FORMAT_MODE_LOCAL or self.LLM_RESPONSE_FORMAT_MODE
        return self.LLM_RESPONSE_FORMAT_MODE

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
