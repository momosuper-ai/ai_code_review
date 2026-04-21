# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 工作流要求

**每次会话开始时**：
1. 阅读本文件 (CLAUDE.md) — 了解项目架构、约定和最新配置
2. 阅读 `memory/MEMORY.md`（如果存在）— 了解稳定模式和已知注意事项
3. 查看 `git status` — 了解当前工作状态

**每次功能修改完成后**，检查以下文件是否需要同步更新：
- **CLAUDE.md** — 新增模块/配置/端点/流程变更时必须更新
- **.env.example** — 新增环境变量时必须追加
- **memory/MEMORY.md** — 发现新的稳定模式或架构决策时更新

## Project Overview

AI 代码审查服务：通过 GitLab Webhook 自动触发，支持本地 vLLM + 云端 LLM 双模型架构，对代码变更进行多维度并行审查（5个专业维度Agent），内置隐私过滤保护敏感数据，结果回写 GitLab。

## Build & Run

### 首次部署

```bash
# 1. 复制环境变量模板并填入实际值
cp .env.example .env
# 编辑 .env 填入数据库密码、GitLab Token、Redis 密码等

# 2. 启动所有服务（前台，可观察日志）
docker-compose up --build

# 3. 数据库迁移（在另一个终端，容器 postgres 启动后执行）
docker-compose exec api alembic upgrade head
```

### 功能更新部署（标准流程）

当代码或配置有变更时，必须按以下顺序操作：

```bash
# 1. 停止现有服务
docker-compose down

# 2. 重建镜像并启动（-d 后台运行）
docker-compose up --build -d

# 3. 执行数据库迁移（如果有新的 alembic 迁移脚本）
docker-compose exec api alembic upgrade head

# 4. 验证服务状态
docker-compose ps                      # 确认所有容器 running
curl http://localhost:8080/health       # 确认 API 健康
docker-compose logs -f celery --tail=20 # 观察 worker 日志
```

> **重要**：任何涉及数据库 schema 变更（新增/修改表或字段）的更新，都必须在容器启动后执行 `alembic upgrade head`。虽然 `main.py` lifespan 会自动 `create_all`，但 Alembic 迁移脚本能确保增量变更的正确性和版本追踪。

### 开发常用命令

```bash
# 运行全量测试
pip install ".[dev]"
pytest tests/

# 运行单个测试文件
pytest tests/unit/test_agent_tools.py

# 运行单个测试
pytest tests/unit/test_agent_tools.py::TestReadFile::test_read_existing_file

# 创建新迁移脚本
docker-compose exec api alembic revision --autogenerate -m "description"

# 查看当前迁移版本
docker-compose exec api alembic current

# 查看实时日志
docker-compose logs -f api celery --tail=50
```

## Architecture

**请求流**: GitLab Webhook → FastAPI (`webhooks.py`) → Celery task (`review_task.py`) → ReviewEngine → [影响面预分析 → 隐私预扫描 → 多维度Agent并行审查 → 结果反脱敏] → DB 持久化 → GitLab comment → （可选）触发 `generate_readme` 任务

**文档生成流**: 审查成功后 → `generate_readme` (Celery, queue=readme) → ReadmeGenerator → [Agent 工具循环或增量更新] → GitLab Commits API 批量提交 → DB 记录

### Core Pipeline (src/tasks/review_task.py)

1. 创建 Review 数据库记录（status: in_progress）
2. `RepoManager` clone/fetch 本地代码库（filelock 防并发）
3. **代码图增量更新**: `update_graph_incremental()` 基于 git diff 更新 SQLite 代码图（首次全量扫描，后续增量），失败不阻塞审查
4. `GitLabClient` 获取 MR diff 或 commit compare
5. `ReviewEngine.filter_diffs()` 过滤非代码文件
6. **影响面预分析**: `ReviewEngine._build_blast_radius_context()` 遍历变更文件，从 diff hunk header 解析变更行范围（`_parse_changed_lines`），通过区间重叠检测（`_ranges_overlap`）过滤出受影响的函数/方法/类节点，仅对这些节点调用 `get_blast_radius_for_nodes` 查询依赖关系（上游调用者/下游被调用者）；diff 无 hunk header 时 fallback 到全文件分析（`get_blast_radius`）；结果注入到 diff prompt 开头，让 Agent 从第一轮就了解变更的依赖拓扑；失败不阻塞审查
7. **隐私预扫描**（双模型模式）: `PrivacyFilter.pre_scan()` 规则引擎 + 本地LLM 混合扫描 diff（含影响面文本）中的敏感数据，构建脱敏映射表
8. `ReviewEngine.run_review()` — 两种运行模式:
   - **多Agent模式**（默认）: 5 个 `DimensionAgent` 通过 `asyncio.gather` + `Semaphore` 并行审查，每维度独立 tool-calling 循环（最多 `REVIEW_DIMENSION_MAX_ROUNDS` 轮），结果经去重 + 加权合并
   - **单Agent模式**: 单一 Agent 全维度审查（最多 `AGENT_MAX_TOOL_ROUNDS` 轮）
   - **Token 统计**: 每次 `chat()` 返回 `ChatResult`（含 `TokenUsage`），各 Agent 本地累加后汇总到 `ReviewResult`（分维度 + 分模型）；`cached_tokens` 从 `usage.prompt_tokens_details` 提取
   - **格式重试纠正**: JSON 解析失败时最多重试 `AGENT_FORMAT_RETRY_MAX` 次，温度递减 `[0.1, 0.05, 0.0]`
   - **降级 fallback 解析**: 重试仍失败时，用正则提取部分结构化信息
   - **工具结果隐私扫描**: 每轮工具调用后批量扫描结果中的新敏感模式
9. **结果反脱敏**（双模型模式）: 占位符 → 原始值还原
10. 持久化 Review + ReviewComment 到 PostgreSQL（**先DB后评论**，评论失败不影响数据持久化）
11. 格式化 Markdown 评论（支持多维度和单Agent两种格式），回写 GitLab（MR note 或 commit comment）
12. **文档生成触发**（可选）: `README_GENERATION_ENABLED=true` 时，审查成功后链式分发 `generate_readme.delay()` 到 readme 队列，失败不影响审查流程

### Key Modules

- **src/core/review_engine.py** — Agent 审查引擎：`ReviewEngine` 总控（单/多Agent模式切换）、`DimensionAgent` 维度Agent（独立 tool-calling 循环 + 本地 TokenUsage 累加）、结果解析/去重/加权评分、分维度+分模型 token 统计、隐私反脱敏、双格式 GitLab 评论；`_parse_changed_lines` 解析 diff hunk 变更行范围、`_ranges_overlap` 区间重叠检测，用于精确化影响面预注入
- **src/core/agent_tools.py** — 6 个沙箱工具（read_file, search_code, list_directory, find_definition, get_file_info, get_blast_radius），`AgentToolExecutor` 基类做路径越界检查和输出截断（20000 字符 / 500 行上限）；`find_definition` 支持 10+ 语言的定义正则匹配；`get_blast_radius` 利用代码图分析变更影响面（上游调用者/下游被调用者）
- **src/core/code_graph/** — 代码图模块（影响面分析）：`parser.py` 多语言 AST/正则解析器（Python/JS/TS/Java/Go）；`store.py` SQLite 持久化存储 + FTS5 全文检索；`query.py` `CodeGraph` 高层查询 API（影响面 BFS 追溯），`get_blast_radius` 全文件分析 + `get_blast_radius_for_nodes` 指定节点精确分析；`incremental.py` 基于 git diff 的增量更新（首次全量扫描，后续仅处理变更文件）
- **src/core/llm_client.py** — AsyncOpenAI SDK 封装，连接 vLLM (OpenAI compatible API)；`TokenUsage` dataclass（prompt/completion/cached tokens + `__iadd__`）、`ChatResult` dataclass（message + usage）；`chat()` 返回 `ChatResult` 支持逐次 token 统计；内置指数退避重试（RateLimitError/APIConnectionError/5xx 可重试）；集成上下文裁剪（`trim_messages` 前置调用）和隐私过滤（`sanitize_messages` 发送前脱敏）
- **src/core/readme_generator.py** — AI_README.md 文档自动生成引擎：`ReadmeGenerator` 类支持两种模式（首次生成用 Agent 工具调用循环，增量更新用单次 LLM 调用）；结构指纹计算（SHA-256）跳过无变化项目；复用 `AgentToolExecutor` 的 5 个只读工具；`FileAction`/`ReadmeResult` 数据结构；**根目录增量更新采用子目录摘要的增量语义**——`_collect_root_info` 仅把本次变化子目录的 `FileAction` 注入 `subdir_docs` 章节，未出现的子目录通过 prompt 显式指令（`_ROOT_SYSTEM_PROMPT_UPDATE` 硬规则 + blockquote 前导文案）引导 LLM 原样保留现有文档描述，避免"缺失即删除"误判
- **src/core/gitlab_client.py** — httpx async 客户端，6 个 GitLab API 方法（`get_project`, `get_mr_changes`, `get_compare`, `post_mr_note`, `post_commit_comment`, `create_commit`）
- **src/core/repo_manager.py** — Git 仓库生命周期管理，oauth2 token 注入 clone URL
- **src/core/privacy_filter.py** — 隐私过滤核心：`SanitizationMapping` 双向映射（original ↔ `<<TYPE_N>>` 占位符）、`PrivacyFilter` 3阶段过滤（`pre_scan` diff预扫描 → `scan_tool_results` 工具结果扫描 → `desanitize_text` 反脱敏）、规则引擎 + 本地LLM混合扫描、`token_usage` 属性跟踪本地模型 token 消耗
- **src/core/privacy_rules.py** — 敏感数据检测规则库：7类规则（KEY/SECRET/IP/DOMAIN/EMAIL/CERT/CUSTOM）、3个检测等级（minimal仅密钥密码/standard+IP邮箱/strict+高熵字符串）
- **src/core/dimensions.py** — 多维度审查定义：5个 `ReviewDimension`（bug 30%/security 25%/performance 15%/quality 15%/architecture 15%）、维度专属 System Prompt、加权评分算法（任一维度D或存在critical → 最终评级 ≤ C）
- **src/core/context_manager.py** — 上下文长度管理：Token 字符估算、4阶段消息裁剪（工具结果截断2000字符 → 参数感知摘要替换 → 历史工具组移除 → Diff截断60%预算），保护 system+user 头部和尾部4条消息
- **src/utils/security.py** — Webhook Token 验证（`verify_gitlab_token`）+ API Key 认证（`require_api_key`），未配置时默认放行（开发模式）
- **src/utils/rate_limit.py** — Redis 固定窗口速率限制器，`rate_limit_dependency()` 工厂函数返回 FastAPI 依赖；Redis 故障时 fail-open
- **src/utils/ip_whitelist.py** — `IPWhitelistMiddleware` IP白名单中间件，支持IPv4/IPv6 CIDR，`/health` 始终放行，未配置时全部放行（开发模式）
- **src/tasks/readme_task.py** — Celery 任务：`generate_readme` 文档生成主入口，编排 ensure_repo → ReadmeGenerator.run() → GitLab Commits API 批量提交 → DB 持久化
- **src/models/readme_record.py** — ORM 模型：`readme_records` 表，追踪文档生成历史和结构指纹
- **src/api/v1/reviews.py** — 审查记录查询 API（分页列表 + 详情），路由 `/api/v1/reviews`
- **src/schemas/webhook.py** — Webhook 请求体 Pydantic schema（`MergeRequestEvent`, `PushEvent`）
- **src/api/router.py** — API 路由聚合，前缀 `/api/v1`

### Webhook Trigger Logic

- **MR 事件**: `object_kind == "merge_request"` && `action in {"open", "update", "reopen", "merge"}` && target_branch 在 MAIN_BRANCHES 中
  - `open/update/reopen`: 审查源分支代码，结果写入 MR 评论（预防性审查）
  - `merge`: 审查合并后代码，结果写入 MR 评论（最终确认）
- **Push 事件**: `object_kind == "push"` && before 非全零 && 有 commits（所有分支均触发，不再限制主分支）
  - **防递归**: 所有 commit message 均以 `[AI-README]` 开头时跳过（防止文档自动提交触发无限循环）

### Database

PostgreSQL + SQLAlchemy 双引擎设计。三张表：`reviews`（审查记录）1:N `review_comments`（问题明细）、`readme_records`（文档生成历史）。启动时自动建表（`main.py` lifespan）。

- FastAPI 使用异步引擎（`asyncpg`）
- Celery Worker 使用同步引擎（`psycopg2`），通过 `DATABASE_URL_SYNC` property 自动转换
- `review_comments` 表新增 `dimension` 字段（String(50), nullable），记录问题所属审查维度
- `reviews` 表新增 `total_prompt_tokens`、`total_completion_tokens`、`total_cached_tokens` 字段（Integer, default=0），持久化 token 统计
- `readme_records` 表：追踪文档生成历史（project_id, branch, commit_sha, fingerprint, status, token 统计等）
- Alembic 迁移: `001_add_dimension_to_review_comments.py`、`002_add_token_columns_to_reviews.py`、`003_add_readme_records.py`

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
- IP 白名单：`IPWhitelistMiddleware` 实现，配置 `IP_WHITELIST`（逗号分隔 IP/CIDR），`/health` 始终放行；未配置时全部放行（开发模式）

### 双模型与隐私过滤

- **双模型模式**: `CLOUD_LLM_MODEL_NAME` 非空且 `PRIVACY_FILTER_ENABLED=true` 时启用；本地 vLLM 负责隐私扫描，云端 LLM 负责代码审查
- **单模型模式**: `CLOUD_LLM_MODEL_NAME` 为空时仅使用本地 vLLM，不启用隐私过滤
- 隐私过滤通过 `LLMClient.set_privacy_filter()` 注入，`chat()` 自动脱敏消息（含 tool_calls 参数）
- 占位符格式: `<<TYPE_N>>`（如 `<<KEY_1>>`、`<<IP_3>>`），同一值始终映射同一占位符
- 3阶段流程: pre_scan(diff) → scan_tool_results(每轮) → desanitize_text(最终结果)

### 多维度审查

- 5个维度: bug(30%)、security(25%)、performance(15%)、quality(15%)、architecture(15%)
- 每个 `DimensionAgent` 有独立 system prompt、categories 限制和 tool-calling 循环
- 并发: `asyncio.Semaphore(REVIEW_MAX_CONCURRENCY)` + `asyncio.wait_for(timeout)` 超时保护
- 结果合并: 跨维度去重（同位置+同分类保留高严重度）→ 加权评分（任一D或critical强制降至C）
- 评论格式: 多维度模式输出维度分组问题列表 + 可折叠详情表格
- `REVIEW_SINGLE_AGENT_MODE=true` 可回退到单Agent全维度审查

### LLM 调用配置

- **重试机制**: 指数退避 `base_delay * 2^attempt + random()`，可重试异常: RateLimitError、APIConnectionError、HTTP 500/502/503/529，最多 `LLM_RETRY_MAX` 次
- **上下文管理**: `chat()` 每次调用前自动 `trim_messages()`，预算 = `LLM_MAX_CONTEXT_TOKENS × LLM_CONTEXT_THRESHOLD - max_tokens`

## 部署注意事项

- 数据库密码统一管理：`POSTGRES_USER`/`POSTGRES_PASSWORD` 在 `.env` 中定义，postgres 容器和 `DATABASE_URL` 共用同一来源，避免密码不一致
- PostgreSQL `POSTGRES_PASSWORD` 环境变量仅在首次初始化（pgdata 卷为空）时生效，修改密码后需手动 `ALTER USER` 或删除 pgdata 卷重建
- `.env` 中包含敏感信息（Token、API Key），不应提交到公开仓库
- Docker 容器命名遵循 `{项目目录名}_{服务名}_{序号}` 格式（下划线分隔）

### 向用户提供部署指导时的完整性要求

当涉及部署或重部署操作时，**必须给出完整的端到端操作步骤**，不能遗漏任何环节。以下是需要逐一检查并告知用户的事项清单：

1. **停止现有服务**: 是否需要先 `docker-compose down` 停止旧容器
2. **环境变量同步**: `.env` 文件是否需要新增/修改配置项（对照 `.env.example` 差异）
3. **镜像重建**: `docker-compose up --build -d`
4. **数据库迁移**: 是否有新的 Alembic 迁移脚本需要执行 `alembic upgrade head`
5. **服务验证**: 提供验证命令（`docker-compose ps`、`curl /health`、日志观察等）
6. **回滚方案**: 如出现问题，如何回退（如 `alembic downgrade -1`）

遗漏任何一步都可能导致部署失败或数据不一致。宁可多写一行命令，不要假设用户知道隐含步骤。

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

## Configuration Reference

### 隐私过滤
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `PRIVACY_FILTER_ENABLED` | `false` | 启用隐私过滤 |
| `PRIVACY_FILTER_LEVEL` | `standard` | 检测等级: minimal/standard/strict |
| `PRIVACY_INTERNAL_DOMAINS` | `""` | 逗号分隔内部域名 |
| `PRIVACY_INTERNAL_CIDRS` | RFC1918 | 内部IP段 |
| `PRIVACY_PRE_SCAN_ENABLED` | `true` | 本地模型diff预扫描 |
| `PRIVACY_TOOL_SCAN_MODE` | `hybrid` | hybrid=规则+模型 / rules_only=仅规则 |
| `PRIVACY_CUSTOM_PATTERNS` | `""` | 自定义正则（JSON数组） |

### 代码图
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `CODE_GRAPH_ENABLED` | `true` | 启用代码图（影响面分析） |
| `CODE_GRAPH_LANGUAGES` | `python,javascript,typescript,java,go` | 启用的语言列表 |
| `CODE_GRAPH_MAX_FILES_FULL_SCAN` | `5000` | 全量扫描最大文件数，超过则跳过 |

### 影响面预注入
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `BLAST_RADIUS_PRE_INJECT` | `true` | 审查开始前预注入影响面分析到 prompt |
| `BLAST_RADIUS_MAX_ITEMS` | `30` | 预注入的最大符号数量 |
| `BLAST_RADIUS_MAX_CHARS` | `4000` | 预注入文本的最大字符数（~1300 tokens） |

### 云端LLM
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `CLOUD_LLM_MODEL_NAME` | `""` | 云端模型名，非空启用双模型模式 |

### AI_README 文档生成
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `README_GENERATION_ENABLED` | `false` | 功能总开关 |
| `README_MIN_CODE_LINES` | `200` | 子目录最小代码行数阈值 |
| `README_LLM_MAX_TOKENS` | `4096` | LLM 最大输出 token |
| `README_AGENT_MAX_ROUNDS` | `8` | 首次生成 Agent 工具调用最大轮数 |
| `README_MAX_SUBDIRS` | `15` | 最大处理子目录数量 |
| `README_CONCURRENCY` | `2` | 子目录文档并发生成数 |
| `README_COMMIT_MESSAGE_PREFIX` | `[AI-README]` | 提交信息前缀（防递归标识） |
| `README_AUTHOR_NAME` | `AI-README Bot` | Git 提交作者名 |
| `README_AUTHOR_EMAIL` | `ai-readme@noreply.local` | Git 提交作者邮箱 |

### 多维度审查
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `REVIEW_DIMENSIONS_ENABLED` | `bug,security,...` | 启用的维度列表 |
| `REVIEW_MAX_CONCURRENCY` | `3` | 维度Agent并发数 |
| `REVIEW_DIMENSION_MAX_ROUNDS` | `6` | 每维度最大工具轮次 |
| `REVIEW_DIMENSION_TIMEOUT` | `180` | 每维度超时秒数 |
| `REVIEW_SINGLE_AGENT_MODE` | `false` | 回退单Agent模式 |

### LLM重试
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `LLM_RETRY_MAX` | `3` | 最大重试次数 |
| `LLM_RETRY_BASE_DELAY` | `2.0` | 基础退避秒数 |
| `LLM_RETRY_MAX_DELAY` | `30.0` | 最大退避秒数 |

### 上下文管理
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `LLM_MAX_CONTEXT_TOKENS` | `256000` | 模型最大上下文token |
| `LLM_CONTEXT_THRESHOLD` | `0.9` | 触发裁剪阈值 |
| `LLM_CHARS_PER_TOKEN` | `3.0` | 字符/token估算比 |

### 速率限制
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `RATE_LIMIT_ENABLED` | `true` | 启用速率限制 |
| `RATE_LIMIT_WEBHOOK` | `30/minute` | Webhook端点限流 |
| `RATE_LIMIT_REVIEWS` | `60/minute` | Reviews API限流 |
| `RATE_LIMIT_DEFAULT` | `120/minute` | 默认限流 |

### Celery
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `CELERY_CONCURRENCY` | `2` | Worker并发数 |
| `CELERY_LOG_LEVEL` | `info` | Worker日志级别 |

## Tests

单元测试位于 `tests/unit/`，使用 pytest：

- `test_agent_tools.py` — AgentToolExecutor 沙箱工具测试
- `test_context_manager.py` — 上下文裁剪逻辑测试
- `test_dimensions.py` — 维度定义和格式重试提示测试
- `test_privacy_filter.py` — 隐私过滤规则和脱敏/反脱敏测试
- `test_review_engine.py` — 审查引擎解析和格式化测试
- `test_webhooks.py` — Webhook 路由和事件处理测试
- `test_code_graph.py` — 代码图解析器、存储、查询和影响面分析测试
- `test_readme_generator.py` — AI_README 文档生成引擎测试（指纹计算、行数统计、工具过滤、防递归等）
