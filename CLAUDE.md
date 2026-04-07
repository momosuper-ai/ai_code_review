# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI 代码审查服务：通过 GitLab Webhook 自动触发，利用 vLLM 大模型对代码变更进行审查，结果回写 GitLab。

## Build & Run

```bash
# 启动核心服务 + 基础设施（共 7 个容器）
docker-compose up --build

# 仅重建并启动
docker-compose up --build -d

# 运行测试
pip install ".[dev]"
pytest tests/

# 运行单个测试文件
pytest tests/unit/test_agent_tools.py

# 运行单个测试
pytest tests/unit/test_agent_tools.py::TestReadFile::test_read_existing_file

# 数据库迁移
alembic revision --autogenerate -m "description"
alembic upgrade head
```

## Architecture

**请求流**: GitLab Webhook → FastAPI (`webhooks.py`) → Celery task (`review_task.py`) → ReviewEngine agent loop → GitLab comment

### Core Pipeline (src/tasks/review_task.py)

1. 创建 Review 数据库记录
2. `RepoManager` clone/fetch 本地代码库（filelock 防并发）
3. `GitLabClient` 获取 MR diff 或 commit compare
4. `ReviewEngine.filter_diffs()` 过滤非代码文件
5. `ReviewEngine.run_review()` — Agent tool-calling 循环（最多 10 轮），LLM 可调用 5 个沙箱工具探索代码上下文
   - **格式重试纠正**: LLM 输出 JSON 解析失败时，最多重试 `AGENT_FORMAT_RETRY_MAX` 次（默认 3），使用递减温度 `[0.1, 0.05, 0.0]` 引导格式修正
   - **降级 fallback 解析**: 重试仍失败时，用正则提取部分结构化信息
6. 格式化 Markdown 评论，回写 GitLab（MR note 或 commit comment）
7. 持久化 Review + ReviewComment 到 PostgreSQL

### Key Modules

- **src/core/review_engine.py** — Agent 审查引擎：构建 system prompt、驱动 LLM tool-calling 循环、解析 JSON 结果、格式化 GitLab 评论
- **src/core/agent_tools.py** — 5 个沙箱工具（read_file, search_code, list_directory, find_definition, get_file_info），`AgentToolExecutor` 基类做路径越界检查和输出截断（20000 字符 / 500 行上限）；`find_definition` 支持 10+ 语言的定义正则匹配
- **src/core/llm_client.py** — AsyncOpenAI SDK 封装，连接 vLLM (OpenAI compatible API)
- **src/core/gitlab_client.py** — httpx async 客户端，5 个 GitLab API 方法（`get_project`, `get_mr_changes`, `get_compare`, `post_mr_note`, `post_commit_comment`）
- **src/core/repo_manager.py** — Git 仓库生命周期管理，oauth2 token 注入 clone URL
- **src/utils/security.py** — Webhook Token 验证（`verify_gitlab_token`）+ API Key 认证（`require_api_key`），未配置时默认放行（开发模式）
- **src/utils/rate_limit.py** — Redis 固定窗口速率限制器，`rate_limit_dependency()` 工厂函数返回 FastAPI 依赖；Redis 故障时 fail-open
- **src/api/v1/reviews.py** — 审查记录查询 API（分页列表 + 详情），路由 `/api/v1/reviews`
- **src/schemas/webhook.py** — Webhook 请求体 Pydantic schema（`MergeRequestEvent`, `PushEvent`）
- **src/api/router.py** — API 路由聚合，前缀 `/api/v1`

### Webhook Trigger Logic

- **MR 事件**: `object_kind == "merge_request"` && `action in {"open", "update", "reopen", "merge"}` && target_branch 在 MAIN_BRANCHES 中
  - `open/update/reopen`: 审查源分支代码，结果写入 MR 评论（预防性审查）
  - `merge`: 审查合并后代码，结果写入 MR 评论（最终确认）
- **Push 事件**: `object_kind == "push"` && before 非全零 && 有 commits（所有分支均触发，不再限制主分支）

### Database

PostgreSQL + SQLAlchemy 双引擎设计。两张表：`reviews`（审查记录）1:N `review_comments`（问题明细）。启动时自动建表（`main.py` lifespan）。

- FastAPI 使用异步引擎（`asyncpg`）
- Celery Worker 使用同步引擎（`psycopg2`），通过 `DATABASE_URL_SYNC` property 自动转换

### API Endpoints

- `GET /health` — 健康检查
- `POST /api/v1/webhooks/gitlab` — GitLab Webhook 接收端点（Token 验证 via `verify_gitlab_token`，速率限制 30/min）
- `GET /api/v1/reviews` — 审查记录列表（分页、按 project_id/status 过滤，需 API Key，速率限制 60/min）
- `GET /api/v1/reviews/{review_id}` — 审查详情（含关联 comments，需 API Key，速率限制 60/min）

## Tech Stack

Python 3.12, FastAPI, SQLAlchemy asyncpg, Celery + Redis, httpx, OpenAI SDK, filelock. Docker Compose 编排 7 个服务（api, celery, postgres, redis, sonarqube, sonar-db, gitlab-runner）。

### Logging

结构化日志系统，支持 text/json 双格式输出 + 可选文件持久化。

- **src/utils/logger.py** — 日志基础设施：`JSONFormatter`（JSON Lines 格式）、`RotatingFileHandler` 文件轮转、`with_context()` 上下文辅助函数
- 配置项：`LOG_FORMAT`（text/json）、`LOG_FILE`（文件路径）、`LOG_FILE_MAX_BYTES`（50MB）、`LOG_FILE_BACKUP_COUNT`（5）
- 全链路追踪：`review_id` 贯穿 webhook → Celery task → ReviewEngine → GitLab API 的所有日志
- 关键模块均有性能计时：LLM 调用、GitLab API、Git 操作、各审查阶段
- FastAPI HTTP 请求日志中间件（`src/main.py`）
- Docker 日志轮转：api/celery 服务配置 json-file 驱动，max-size 50MB，保留 3 个文件

日志级别约定：
- `DEBUG`：工具参数详情、被忽略的 webhook 事件、git 命令细节
- `INFO`：关键流程节点、每个阶段耗时、API 调用
- `WARNING`：JSON 解析失败回退、max rounds 达到、diff 源缺失
- `ERROR`：任务失败、API 调用失败、git 操作失败

## Conventions

- 所有面向用户的输出（LLM prompt、审查报告、评论、日志消息）使用中文
- Agent 工具必须在 `AgentToolExecutor` 沙箱内执行，不允许路径越界
- Celery 任务配置：soft_time_limit=600s, max_retries=2, concurrency=2
- 配置通过 `.env` + Pydantic Settings 管理
- SonarQube 已部署但尚未与应用代码集成（仅基础设施预留），`.env` 中有 `SONARQUBE_URL`/`SONARQUBE_TOKEN` 配置
- `config.py` 中 `REDIS_URL` 默认值无密码，生产环境必须通过 `.env` 配置带密码的 URL
- Webhook 安全：通过 `X-Gitlab-Token` header 验证，由 `src/utils/security.py` 实现；未配置 `GITLAB_WEBHOOK_SECRET` 时默认放行（仅限开发环境）
- API Key 认证：Reviews API 通过 `X-API-Key` header 验证，未配置 `API_KEYS` 时默认放行（开发模式）；生成密钥：`python -c "import secrets; print(secrets.token_urlsafe(32))"`
- 速率限制：基于 Redis 固定窗口计数器，按 `{client_ip}:{path}` 限流；Redis 故障时 fail-open；超限返回 429 + `Retry-After` header

## 部署注意事项

- 数据库密码统一管理：`POSTGRES_USER`/`POSTGRES_PASSWORD` 在 `.env` 中定义，postgres 容器和 `DATABASE_URL` 共用同一来源，避免密码不一致
- PostgreSQL `POSTGRES_PASSWORD` 环境变量仅在首次初始化（pgdata 卷为空）时生效，修改密码后需手动 `ALTER USER` 或删除 pgdata 卷重建
- `.env` 中包含敏感信息（Token、API Key），不应提交到公开仓库
- Docker 容器命名遵循 `{项目目录名}_{服务名}_{序号}` 格式（下划线分隔）

## Code Style Guide

### Import 规范
- 使用**绝对导入**（`from src.core.xxx import ...`），不使用相对导入
- Import 分组顺序：标准库 → 第三方库 → 本地模块，组间空一行
- 避免循环导入：必要时在函数内部延迟导入

### 类型标注
- 使用 Python 3.10+ 语法：`str | None`（不用 `Optional[str]`）、`list[str]`（不用 `List[str]`）
- 所有公开函数/方法必须有参数和返回值类型标注

### 命名规范（PEP 8）
- 函数/方法/变量：`snake_case`
- 类：`PascalCase`
- 常量：`UPPER_CASE`，模块私有常量加 `_` 前缀（如 `_SKIP_PATTERNS`）
- 私有方法：单下划线前缀（如 `_safe_resolve`）

### 字符串
- 统一使用**双引号** `"..."`
- 字符串拼接优先使用 **f-string**
- logger 调用使用 **%-formatting**（`logger.info("消息: %s", var)`），避免 f-string 在日志中的性能开销

### Docstring
- 模块级 docstring：简要描述模块职责（英文）
- 类和公开函数：必须有 docstring
- 风格：单行摘要 + 可选详细说明，中英文皆可
- 私有方法（`_xxx`）：简要注释即可

### 异步模式
- FastAPI handler 一律 `async def`
- Celery 任务函数为同步，内部通过 `asyncio.run()` 调用异步逻辑
- 阻塞 I/O（如 Git 命令）使用 `asyncio.to_thread()` 包装
- FastAPI 用 `AsyncSession`，Celery 用同步 `SyncSession`

### 错误处理
- 优先捕获具体异常类型（如 `httpx.HTTPStatusError`），避免裸 `except Exception`
- 工具执行结果统一使用 `ToolResult` dataclass 封装
- 关键操作必须有 logger 记录（含耗时信息）
