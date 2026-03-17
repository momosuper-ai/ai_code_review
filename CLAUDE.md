# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI 代码审查服务：通过 GitLab Webhook 自动触发，利用 vLLM 大模型对代码变更进行审查，结果回写 GitLab。

## Build & Run

```bash
# 启动所有服务 (API + Celery + PostgreSQL + Redis)
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
6. 格式化 Markdown 评论，回写 GitLab（MR note 或 commit comment）
7. 持久化 Review + ReviewComment 到 PostgreSQL

### Key Modules

- **src/core/review_engine.py** — Agent 审查引擎：构建 system prompt、驱动 LLM tool-calling 循环、解析 JSON 结果、格式化 GitLab 评论
- **src/core/agent_tools.py** — 5 个沙箱工具（read_file, search_code, list_directory, find_definition, get_file_info），`AgentToolExecutor` 基类做路径越界检查和输出截断
- **src/core/llm_client.py** — AsyncOpenAI SDK 封装，连接 vLLM (OpenAI compatible API)
- **src/core/gitlab_client.py** — httpx async 客户端，4 个 GitLab API 方法
- **src/core/repo_manager.py** — Git 仓库生命周期管理，oauth2 token 注入 clone URL

### Webhook Trigger Logic

- **MR 事件**: `object_kind == "merge_request"` && `action == "merge"` && target_branch 在 MAIN_BRANCHES 中
- **Push 事件**: `object_kind == "push"` && ref 指向主分支 && before 非全零 && 有 commits

### Database

PostgreSQL + SQLAlchemy async。两张表：`reviews`（审查记录）1:N `review_comments`（问题明细）。启动时自动建表（`main.py` lifespan）。

## Tech Stack

Python 3.12, FastAPI, SQLAlchemy asyncpg, Celery + Redis, httpx, OpenAI SDK, filelock. Docker Compose 编排 4 个服务（api, celery, postgres, redis）。

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
