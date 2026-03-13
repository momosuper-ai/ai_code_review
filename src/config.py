from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    app_env: str = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/obc_review"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # GitLab
    gitlab_url: str = "https://gitlab.example.com"
    gitlab_token: str = ""
    gitlab_webhook_secret: str = ""

    # vLLM (OpenAI compatible)
    vllm_api_url: str = "http://localhost:8000/v1"
    vllm_model_name: str = "devstral-small-2-24b"

    # SonarQube
    sonarqube_url: str = "http://localhost:9000"
    sonarqube_token: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
