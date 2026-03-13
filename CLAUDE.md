# OBC AI Code Review

三层代码质量保障平台：SonarQube 静态分析 + AI 智能审查 + 人工 Review。

## 项目概述

- **目标**：为 30~50 人团队提供持续代码质量监控
- **后端**：Python 3.12 + FastAPI + Celery + Redis
- **数据库**：PostgreSQL（SQLAlchemy + Alembic）
- **AI 引擎**：对接已有 vLLM + Devstral-Small-2-24B（OpenAI 兼容 API）
- **静态分析**：SonarQube Community + Branch Plugin（Docker 部署）
- **代码托管**：GitLab 私有部署

## 代码规范

### Python 风格

- 遵循 PEP 8，行宽 120 字符
- 使用 type hints 标注函数签名
- 使用 `async def` 编写 FastAPI 路由（I/O 密集型操作）
- 使用 Pydantic v2 做数据校验
- 环境变量通过 pydantic-settings 管理，禁止硬编码

### 项目结构约定

- `src/api/` — API 路由层，只做参数校验和调用 core 层
- `src/core/` — 核心业务逻辑，不依赖 FastAPI
- `src/models/` — SQLAlchemy ORM 模型
- `src/schemas/` — Pydantic 请求/响应模型
- `src/tasks/` — Celery 异步任务
- `src/utils/` — 通用工具函数

### 命名规范

- 文件名：snake_case
- 类名：PascalCase
- 函数/变量：snake_case
- 常量：UPPER_SNAKE_CASE
- API 路由：`/api/v1/resource_name`

### 安全要求

- Webhook 请求必须验证签名（GitLab Secret Token）
- 数据库连接串、API Key 等敏感信息通过环境变量注入
- 禁止在日志中输出完整的代码内容或 API Key

### 测试

- 使用 pytest
- 单元测试放在 `tests/unit/`
- 集成测试放在 `tests/integration/`

### Git 规范

- 分支命名：`feature/xxx`、`fix/xxx`、`refactor/xxx`
- Commit message 格式：`type(scope): description`
- type：feat / fix / refactor / test / docs / chore

## 依赖管理

- 使用 pyproject.toml + uv 管理依赖
- 核心依赖：fastapi, uvicorn, sqlalchemy, alembic, celery, redis, httpx, pydantic-settings, openai

## 环境变量

| 变量名 | 说明 | 示例 |
|--------|------|------|
| DATABASE_URL | PostgreSQL 连接串 | postgresql+asyncpg://user:pass@localhost:5432/obc_review |
| REDIS_URL | Redis 连接串 | redis://localhost:6379/0 |
| GITLAB_URL | GitLab 实例地址 | https://gitlab.example.com |
| GITLAB_TOKEN | GitLab API Token | glpat-xxxx |
| GITLAB_WEBHOOK_SECRET | Webhook 签名密钥 | your-secret |
| VLLM_API_URL | vLLM 服务地址 | http://gpu-server:8000/v1 |
| VLLM_MODEL_NAME | 模型名称 | devstral-small-2-24b |
| SONARQUBE_URL | SonarQube 服务地址 | http://sonar-server:9000 |
| SONARQUBE_TOKEN | SonarQube API Token | squ_xxxx |
