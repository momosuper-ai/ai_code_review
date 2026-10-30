# AI 代码审查服务

基于大语言模型的自动化代码审查服务。通过 GitLab Webhook 自动触发，支持本地 vLLM + 云端 LLM 双模型架构，对代码变更进行多维度并行审查（缺陷/安全/性能/质量/架构 5 个专业维度 Agent），内置隐私过滤保护敏感数据，审查报告以评论形式回写到 GitLab 的 Merge Request 或 Commit 上。

## 功能特性

- **多维度并行审查**：5 个专业维度 Agent（缺陷/安全/性能/质量/架构）通过 `asyncio.gather` 并行分析，加权评分合并
- **Project Brain 项目知识**（默认关闭）：与审查共用 Webhook；主分支变动自动首建、后续增量维护，固定 SHA 的事实、六类证据卡片及版本下载。写回边界为 `PROJECT_BRAIN.md` 与 `docs/project-brain/`，目录内可自由命名；独立 Publisher 校验所有权、内容和差异。启用后接管旧 AI_README 生成。[配置、部署与回滚说明](docs/project-brain.md)
- **智能 Agent 模式**：每个维度 Agent 独立 tool-calling 循环，可自主查阅代码上下文以做出准确判断
- **安全沙箱**：LLM 只能通过 6 个受限工具（read_file, search_code, list_directory, find_definition, get_file_info, get_blast_radius）访问被审查项目的代码
- **隐私过滤**：规则引擎 + LLM 混合扫描，3 阶段流水线（预扫描 -> 工具结果扫描 -> 反脱敏）自动保护敏感数据
- **双模型架构**：本地 vLLM 负责隐私扫描，云端 LLM 负责代码审查（可选启用）
- **代码图分析**：多语言 AST 解析构建代码图（Python/JS/TS/Java/Go），基于 diff hunk 精确定位变更函数，仅追溯受影响符号的依赖关系
- **安全防护**：Webhook Token 验证、API Key 认证、IP 白名单、Redis 速率限制
- **结构化输出强约束**：`chat()` 终局调用按 `LLM_RESPONSE_FORMAT_MODE_*` 自动注入 OpenAI `response_format` —— 三档可配 `json_schema`（OpenAI strict 兼容）/ `json_object`（默认，最兼容）/ `off`（向后兼容仅 prompt），从协议层杜绝 Markdown/纯文本误输出
- **智能容错**：LLM 调用指数退避重试、上下文自动裁剪、JSON 格式重试全程透传 schema_builder、降级 fallback 解析、解析失败原文落盘（含 rf_mode 段便于关联）
- **结果回写**：审查报告格式化为 Markdown 评论，自动发布到 GitLab；先持久化 DB 后发评论，评论失败不影响数据
- **AI_README 自动生成**（可选）：仅在 push 到主分支 或 MR merge 到主分支 审查成功后链式触发；首次用 Agent 工具循环、后续用结构指纹 + 单次 LLM 调用增量更新；按 trigger_type 选择提交路径——push 直接 commit；merge 探测主分支 `protected` 字段，**受保护时走"临时分支 → create_commit → 自动 MR → 自动合并"绕过**；三层防递归（commit message 前缀 + 临时分支前缀 push 过滤 + 自动 MR 前缀过滤 + DB partial unique index）
- **历史记录**：所有审查记录和问题明细持久化到 PostgreSQL，支持通过 API 查询
- **异步处理**：基于 Celery + Redis 的异步任务队列，Webhook 即时响应不阻塞

## 系统架构

```
GitLab Webhook (MR action=merge / Push 任意分支)
       |
       v
  FastAPI 接收 Webhook
  (Token验证 -> IP白名单 -> 速率限制 -> 三层防递归过滤)
       |
       v
  Celery 异步任务队列 (run_review)
       |
       +-- 0. 入口去重: 查询近 30 分钟同 (project_id, commit_sha) 是否已有审查
       +-- 1. 克隆/更新本地代码库 (filelock 防并发)
       +-- 2. 代码图增量更新 (AST 解析变更文件; 首次全量, 后续增量)
       +-- 3. 获取代码变更 (GitLab API: MR diff 或 commit compare)
       +-- 4. 过滤非代码文件
       +-- 5. 影响面预分析 (基于 diff hunk 变更行精确定位受影响节点 → 注入 prompt)
       +-- 6. 隐私预扫描 (规则+本地 LLM 混合, 含影响面文本; 仅双模型模式)
       +-- 7. 多维度 Agent 并行审查 (默认) 或 单 Agent 模式
       |      5 个维度 Agent 经 asyncio.gather + Semaphore 并发分析 diff
       |        <-> 调用 6 个沙箱工具查看上下文
       |        <-> 协议层强约束输出 (response_format=json_object/json_schema)
       |      每轮工具结果批量隐私扫描
       |      JSON 解析失败时多档重试 + 降级 fallback
       +-- 8. 结果反脱敏 (占位符还原; 仅双模型模式)
       +-- 9. 跨维度去重 + 加权评分 (任一 D 或 critical → 强制降至 C)
       +-- 10. 持久化审查记录 + 问题明细 + token 统计到 PostgreSQL
       +-- 11. 格式化 Markdown 报告 -> 回写 GitLab 评论 (评论失败不影响数据持久化)
       +-- 12. (可选) 链式分发 generate_readme 任务到 readme 队列
              触发条件: push 到主分支 或 MR merge 到主分支
              ReadmeGenerator: 扫描子目录 -> 指纹比对 -> 首次 Agent 工具循环 / 增量单次调用
                push 路径: 直接 create_commit 到主分支
                merge 路径: 探测主分支 protected 字段
                  - 未保护: 直接 create_commit
                  - 受保护: 临时分支 (ai-readme/{ts}-{sha8}) -> create_commit
                            -> create_merge_request -> (可选) accept_merge_request 自动合并
              数据库 partial unique index 保证同一 commit 仅生成一次成功记录
```

## 环境要求

- Docker 和 Docker Compose
- 可访问的 GitLab 实例（支持自建 GitLab）
- 可访问的 vLLM 服务（OpenAI 兼容 API）

## 快速开始

### 第一步：克隆项目

```bash
git clone <仓库地址>
cd obc_ai_code_review
```

### 第二步：配置环境变量

复制模板并编辑：

```bash
cp .env.example .env
```

编辑 `.env` 文件，修改以下核心配置：

```ini
# 完整配置参考 .env.example，以下为核心必改项：

# ==================== 数据库 ====================
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your-secure-password
DATABASE_URL=postgresql+asyncpg://postgres:your-secure-password@postgres:5432/obc_review

# ==================== Redis ====================
REDIS_PASSWORD=your-redis-password
REDIS_URL=redis://:your-redis-password@redis:6379/0

# ==================== GitLab 配置 ====================
GITLAB_URL=https://your-gitlab.example.com/
GITLAB_TOKEN=your-gitlab-token
GITLAB_WEBHOOK_SECRET=your-webhook-secret

# ==================== vLLM 配置 ====================
VLLM_API_URL=http://your-vllm-host:port/v1
VLLM_API_KEY=your-api-key
VLLM_MODEL_NAME=mistralai/Devstral-Small-2-24B-Instruct-2512

# ==================== 可选：高级配置 ====================
# 更多配置项（安全/审查/隐私/代码图等）请参考 .env.example
```

#### GitLab Token 获取方法

1. 登录 GitLab，点击右上角头像 -> **Edit profile**
2. 左侧菜单选择 **Access Tokens**
3. 填写 Token 名称（如 `ai-code-review`），设置过期时间
4. 在 **Select scopes** 中勾选 **api**（完整 API 访问权限）
5. 点击 **Create personal access token**，复制生成的 Token 填入 `.env` 的 `GITLAB_TOKEN`

### 第三步：启动服务

```bash
docker-compose up --build -d
```

这将启动 4 个容器：

| 服务 | 说明 | 端口 |
|------|------|------|
| `api` | FastAPI Web 服务 | 8080->8000 |
| `celery` | Celery 异步任务 Worker | - |
| `postgres` | PostgreSQL 16 数据库 | 127.0.0.1:5433->5432 |
| `redis` | Redis 7 消息队列 | 127.0.0.1:6379 |

数据库迁移（在另一个终端，容器 postgres 启动后执行）
docker-compose exec api alembic upgrade head

### 第四步：验证服务状态

```bash
# 检查健康状态
curl http://localhost:8080/health
# 预期返回: {"status":"ok"}

# 查看容器运行状态
docker-compose ps

# 查看 API 日志
docker-compose logs -f api

# 查看 Celery Worker 日志
docker-compose logs -f celery
```

### 第五步：配置 GitLab Webhook

对每一个需要 AI 审查的 GitLab 项目，执行以下操作：

1. 进入 GitLab 项目页面，左侧菜单选择 **Settings -> Webhooks**
2. 填写配置：
   - **URL**：`http://<你的服务器IP>:8080/api/v1/webhooks/gitlab`
   - **Secret token**：填入 `.env` 中 `GITLAB_WEBHOOK_SECRET` 的值
   - **Trigger**：勾选以下事件
     - **Merge request events**（MR 事件触发审查）
     - **Push events**（推送时触发审查）
3. 点击 **Add webhook** 保存

> **注意**：服务器需要在 GitLab 能够访问的网络中。如果服务部署在内网，请确保 GitLab 能通过配置的 URL 访问到服务。

### 第六步：测试 Webhook

配置完成后可以通过 GitLab 的测试功能验证连通性：

1. 在 Webhook 列表中，点击刚创建的 Webhook 旁边的 **Test** 按钮
2. 选择 **Push events** 发送测试请求
3. 查看服务日志确认收到请求：

```bash
docker-compose logs -f api
```

你也可以用 `curl` 手动模拟一个 Push 事件来测试：

```bash
curl -X POST http://localhost:8080/api/v1/webhooks/gitlab \
  -H "Content-Type: application/json" \
  -H "X-Gitlab-Token: your-webhook-secret" \
  -d '{
    "object_kind": "push",
    "ref": "refs/heads/main",
    "before": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "after": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "total_commits_count": 1,
    "user_name": "test_user",
    "project": {
      "id": 1,
      "path_with_namespace": "group/project"
    },
    "commits": []
  }'
```

## 安全配置

### Webhook Token 验证

通过 `X-Gitlab-Token` header 验证请求来自 GitLab。配置 `GITLAB_WEBHOOK_SECRET` 环境变量，未配置时默认放行（仅限开发环境）。

### API Key 认证

Reviews API 通过 `X-API-Key` header 验证访问权限。

- 生成密钥：`python -c "import secrets; print(secrets.token_urlsafe(32))"`
- 将生成的密钥添加到 `.env` 的 `API_KEYS`（多个密钥逗号分隔）
- `API_KEYS` 未配置时默认放行（开发模式）

### IP 白名单

配置 `IP_WHITELIST` 环境变量（逗号分隔 IP/CIDR），限制可访问服务的 IP 地址。`/health` 端点始终放行。未配置时全部放行（开发模式）。

### 速率限制

基于 Redis 固定窗口计数器，按 `{client_ip}:{path}` 限流。超限返回 HTTP 429 + `Retry-After` header。Redis 故障时 fail-open（不阻塞请求）。

| 端点 | 默认限制 |
|------|----------|
| Webhook | 30 次/分钟 |
| Reviews API | 60 次/分钟 |
| 其他 | 120 次/分钟 |

## 触发审查的条件

并非所有 GitLab 事件都会触发审查，仅以下情况会启动 AI 审查：

### Merge Request 事件

- **仅 `action == "merge"` 触发**（合并到主分支时审查最终代码）
- `open` / `update` / `reopen` / `close` 等其他 action 一律忽略——精简为"merge 仅触发一次"严格语义，避免反复触发
- 目标分支必须在 `MAIN_BRANCHES` 配置中
- 触发后审查结果写入 MR 评论；同时设置 Redis 标记 (`mr_merge:{project_id}:{commit_sha}` TTL 300s) 抑制 GitLab 同步发送的目标分支 push 事件
- **防递归过滤**：source_branch 以 `README_TEMP_BRANCH_PREFIX`（默认 `ai-readme/`）开头，**或** title 以 `README_MR_TITLE_PREFIX`（默认 `[AI-README]`）开头的 MR 一律忽略（防止"文档自动 MR → 触发审查"递归）

### Push 事件

- **任意分支**的推送均触发审查（不限制主分支；但 README 文档生成另有主分支限制）
- 过滤条件：不是新建分支（before SHA 非全零）且包含至少一个 commit
- **三层防递归过滤**（按顺序判定）：
  1. 分支名以 `README_TEMP_BRANCH_PREFIX`（默认 `ai-readme/`）开头 → 忽略（受保护主分支 README 绕过流程产生）
  2. 本次推送所有 commit message 均以 `[AI-README]` 开头 → 忽略（文档自动提交）
  3. commit_sha 命中 MR merge 抑制标记（5 分钟内）→ 忽略（防止 merge + 同步 push 双触发）

以下情况**不会**触发：新建分支的推送、无 commit 的推送、命中任一防递归条件的推送。

## 审查报告说明

AI 审查完成后，会在 GitLab 的 MR 或 Commit 下方发布一条评论。

### 多维度模式（默认）

- **总览**：整体评级（A/B/C/D）+ 各维度评分概要
- **维度分组问题列表**：按维度分组展示发现的问题，每维度标注评分和问题数
- **可折叠详情表格**：维度名称、评分、工具调用次数、耗时
- **亮点**：代码中值得肯定的部分

评分规则：5 个维度加权评分（bug 30% / security 25% / performance 15% / quality 15% / architecture 15%），任一维度 D 或存在 critical 问题则最终评级强制降至 C。

### 单 Agent 模式（`REVIEW_SINGLE_AGENT_MODE=true`）

- 与多维度模式类似，但不分维度展示
- 适用于需要快速审查或模型能力较弱的场景

### 问题格式

每个问题包含：
- 严重程度图标（Critical / Major / Minor / Info）
- 问题分类（bug / security / performance / maintainability / style / design）
- 文件路径和行号
- 问题描述和修改建议

## API 接口

### 认证说明

- **Webhook 端点**：通过 `X-Gitlab-Token` header 验证（`GITLAB_WEBHOOK_SECRET` 未配置时免验证）
- **Reviews API**：通过 `X-API-Key` header 验证（`API_KEYS` 未配置时免认证）
- **速率限制**：Webhook 30 次/分钟，Reviews API 60 次/分钟，超限返回 429 + `Retry-After`

### 健康检查

```
GET /health
```

返回：`{"status": "ok"}`

### 审查记录列表

```
GET /api/v1/reviews?project_id=1&status=completed&page=1&page_size=20
```

请求头：`X-API-Key: your-api-key`

查询参数（均为可选）：
- `project_id`：按 GitLab 项目 ID 过滤
- `status`：按审查状态过滤（`pending` / `in_progress` / `completed` / `failed`）
- `page`：页码，默认 1
- `page_size`：每页数量，默认 20，最大 100

返回示例：

```json
{
  "total": 42,
  "page": 1,
  "page_size": 20,
  "items": [
    {
      "id": 1,
      "project_name": "group/project",
      "trigger_type": "merge_request",
      "merge_request_iid": 123,
      "merge_request_title": "Add user login feature",
      "commit_sha": "abc123...",
      "branch": "main",
      "author": "developer",
      "status": "completed",
      "files_reviewed": 5,
      "issues_found": 3,
      "total_prompt_tokens": 12340,
      "total_completion_tokens": 4560,
      "total_cached_tokens": 1280,
      "created_at": "2026-03-16T10:00:00",
      "completed_at": "2026-03-16T10:02:30"
    }
  ]
}
```

> **关于 token 字段**：`reviews` 表持久化 prompt/completion/cached token 用量，按维度 + 按模型（cloud/local）分别累加。详情接口同时返回每个 issue 的 `dimension` 字段，标记问题来自哪个维度 Agent。

### 审查详情

```
GET /api/v1/reviews/{review_id}
```

请求头：`X-API-Key: your-api-key`

返回审查记录的完整信息，包含所有问题评论明细（文件路径、行号、严重程度、分类、维度、描述、建议等）。

## 配置参考

所有配置通过 `.env` 文件管理，完整模板见 `.env.example`。

### 数据库

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `POSTGRES_USER` | `postgres` | PostgreSQL 用户名 |
| `POSTGRES_PASSWORD` | - | PostgreSQL 密码（必填） |
| `DATABASE_URL` | - | 异步数据库连接 URL（必填） |

### Redis

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `REDIS_PASSWORD` | - | Redis 认证密码 |
| `REDIS_URL` | `redis://redis:6379/0` | Redis 连接 URL |

### GitLab

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `GITLAB_URL` | - | GitLab 实例地址（必填） |
| `GITLAB_TOKEN` | - | GitLab Personal Access Token（必填） |
| `GITLAB_WEBHOOK_SECRET` | `""` | Webhook 验证密钥（空=开发模式免验证） |

### vLLM

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `VLLM_API_URL` | - | vLLM 服务地址（必填） |
| `VLLM_API_KEY` | - | vLLM API Key（必填） |
| `VLLM_MODEL_NAME` | - | 模型名称（必填） |

### 应用

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `APP_ENV` | `development` | 运行环境 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `LOG_FORMAT` | `text` | 日志格式：text / json |
| `LOG_FILE` | `""` | 日志文件路径（空=仅 stdout） |
| `REPOS_BASE_DIR` | `/data/repos` | 本地代码库存储路径 |
| `AGENT_MAX_TOOL_ROUNDS` | `10` | 单Agent模式最大工具轮数 |
| `AGENT_FORMAT_RETRY_MAX` | `3` | JSON 格式重试次数 |
| `MAIN_BRANCHES` | `main,master` | 触发审查的主分支（逗号分隔） |

### 安全

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `API_KEYS` | `""` | API 密钥（逗号分隔，空=免认证） |
| `IP_WHITELIST` | `""` | IP 白名单（逗号分隔 IP/CIDR，空=不限制） |

### 速率限制

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `RATE_LIMIT_ENABLED` | `true` | 启用速率限制 |
| `RATE_LIMIT_WEBHOOK` | `30/minute` | Webhook 端点限流 |
| `RATE_LIMIT_REVIEWS` | `60/minute` | Reviews API 限流 |
| `RATE_LIMIT_DEFAULT` | `120/minute` | 默认限流 |

### 多维度审查

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `REVIEW_DIMENSIONS_ENABLED` | `bug,security,performance,quality,architecture` | 启用的维度列表 |
| `REVIEW_MAX_CONCURRENCY` | `3` | 维度 Agent 并发数 |
| `REVIEW_DIMENSION_MAX_ROUNDS` | `6` | 每维度最大工具轮次 |
| `REVIEW_DIMENSION_TIMEOUT` | `180` | 每维度超时秒数 |
| `REVIEW_SINGLE_AGENT_MODE` | `false` | 回退单 Agent 模式 |

### LLM 重试

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `LLM_RETRY_MAX` | `3` | 最大重试次数 |
| `LLM_RETRY_BASE_DELAY` | `2.0` | 基础退避秒数 |
| `LLM_RETRY_MAX_DELAY` | `30.0` | 最大退避秒数 |
| `LLM_MAX_TOKENS` | `256000` | `chat()` 不传 max_tokens 时的输出上限；过低会导致长 JSON 被截断 |
| `LLM_EXTRA_BODY` | `""` | OpenAI SDK `extra_body` JSON 透传（thinking/reasoning_effort 等模型特性） |

### LLM 结构化输出（response_format）

通过协议层强制约束 LLM 输出的 JSON 合规性，配合 prompt 大幅降低解析失败率。三档模式可配，默认 `json_object`（最兼容）。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `LLM_RESPONSE_FORMAT_MODE` | `json_object` | 全局模式：`json_schema`（最强）/ `json_object`（默认）/ `off`（关闭） |
| `LLM_RESPONSE_FORMAT_MODE_CLOUD` | `""` | 云端模型独立覆盖（空 = 跟随全局）；DeepSeek 系仅支持 `json_object` |
| `LLM_RESPONSE_FORMAT_MODE_LOCAL` | `""` | 本地 vLLM 独立覆盖（空 = 跟随全局）；vLLM 0.8.5+ / Qwen 系可设 `json_schema` |

> 工具调用循环阶段（`tools` 非空）一律不挂 response_format（与 tools 并存会冲突）；终局调用（`tools=None`）才注入。README 文档生成显式传 `response_format=None` 禁用，保证 Markdown 输出不被强制 JSON。
>
> 切换前建议用最小请求探测当前 endpoint 对各模式的支持情况（vLLM 0.8.5+ / Qwen 系支持 `json_schema`，DeepSeek 系仅支持 `json_object`），再设置 LOCAL/CLOUD 覆盖。

### 上下文管理

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `LLM_MAX_CONTEXT_TOKENS` | `256000` | 模型最大上下文 token |
| `LLM_CONTEXT_THRESHOLD` | `0.9` | 触发裁剪阈值 |
| `LLM_CHARS_PER_TOKEN` | `3.0` | 字符/token 估算比 |

### Celery

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `CELERY_CONCURRENCY` | `2` | Worker 并发数 |
| `CELERY_LOG_LEVEL` | `info` | Worker 日志级别 |

### 代码图

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `CODE_GRAPH_ENABLED` | `true` | 启用代码图（影响面分析） |
| `CODE_GRAPH_LANGUAGES` | `python,javascript,typescript,java,go` | 启用的语言列表 |
| `CODE_GRAPH_MAX_FILES_FULL_SCAN` | `5000` | 全量扫描最大文件数 |

### 影响面预注入

审查开始前预计算变更文件的依赖关系（基于 diff hunk 变更行精确定位受影响节点的上游调用者/下游被调用者），将影响面文本注入 prompt 开头，让 Agent 从第一轮就理解变更的依赖拓扑。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `BLAST_RADIUS_PRE_INJECT` | `true` | 启用影响面预注入 |
| `BLAST_RADIUS_MAX_ITEMS` | `30` | 预注入的最大符号数量 |
| `BLAST_RADIUS_MAX_CHARS` | `4000` | 预注入文本的最大字符数（约 1300 tokens） |

### 云端 LLM（双模型模式）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `CLOUD_LLM_MODEL_NAME` | `""` | 云端模型名（空=仅本地 vLLM，非空=启用双模型） |

### 隐私过滤

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `PRIVACY_FILTER_ENABLED` | `false` | 启用隐私过滤 |
| `PRIVACY_FILTER_LEVEL` | `standard` | 检测等级：minimal / standard / strict |
| `PRIVACY_INTERNAL_DOMAINS` | `""` | 逗号分隔内部域名 |
| `PRIVACY_INTERNAL_CIDRS` | RFC1918 | 内部 IP 段 |
| `PRIVACY_PRE_SCAN_ENABLED` | `true` | 本地模型 diff 预扫描 |
| `PRIVACY_TOOL_SCAN_MODE` | `hybrid` | hybrid=规则+模型 / rules_only=仅规则 |
| `PRIVACY_CUSTOM_PATTERNS` | `""` | 自定义正则（JSON 数组） |

### AI_README 文档生成

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `README_GENERATION_ENABLED` | `false` | 功能总开关；`true` 时仅 push 到主分支 或 MR merge 到主分支 审查成功后链式触发 |
| `README_MIN_CODE_LINES` | `200` | 子目录最小代码行数阈值（低于则跳过） |
| `README_LLM_MAX_TOKENS` | `4096` | LLM 最大输出 token |
| `README_AGENT_MAX_ROUNDS` | `8` | 首次生成 Agent 工具调用最大轮数 |
| `README_MAX_SUBDIRS` | `15` | 最大处理子目录数量 |
| `README_CONCURRENCY` | `2` | 子目录文档并发生成数 |
| `README_COMMIT_MESSAGE_PREFIX` | `[AI-README]` | 提交信息前缀（防递归标识） |
| `README_AUTHOR_NAME` | `AI-README Bot` | Git 提交作者名 |
| `README_AUTHOR_EMAIL` | `ai-readme@noreply.local` | Git 提交作者邮箱 |

#### 受保护主分支处理（仅 MR merge 链路）

当主分支被 GitLab 设置为 protected 时，无法直接 commit。代码自动走"临时分支 → create_commit → create_merge_request → (可选) accept_merge_request 自动合并"绕过流程；相关分支前缀和 MR 标题前缀同时被 webhook 层识别忽略，防止递归。push 链路能成功 push 本身就意味着主分支未保护，因此无需此流程。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `README_TEMP_BRANCH_PREFIX` | `ai-readme/` | 临时分支前缀；webhook 层识别此前缀的 push 直接忽略 |
| `README_MR_TITLE_PREFIX` | `[AI-README]` | 自动 MR 标题前缀；webhook 层识别此前缀的 MR 直接忽略 |
| `README_MR_AUTO_MERGE` | `true` | 是否自动合并 AI-README MR（受保护分支场景） |
| `README_MR_AUTO_MERGE_REMOVE_BRANCH` | `true` | 自动合并后是否删除临时源分支 |
| `README_MR_DESCRIPTION` | `Automated AI_README.md update. Safe to merge.` | 自动 MR 的描述文案 |

## 运维指南

### 查看日志

```bash
# 查看所有服务日志
docker-compose logs -f

# 仅查看 API 服务日志
docker-compose logs -f api

# 仅查看 Celery Worker 日志（审查任务执行细节在此处）
docker-compose logs -f celery
```

日志系统支持两种输出格式，通过 `LOG_FORMAT` 环境变量切换：

**text 格式**（默认，适合开发阶段）：
```
2026-03-16 14:30:01 | INFO     | obc_review | [review:7] 审查完成: 评级=B 问题数=3 工具调用=2 (9.2秒)
```

**json 格式**（适合生产环境，便于日志采集和分析）：
```json
{"ts":"2026-03-16 14:30:15","level":"INFO","logger":"obc_review","msg":"[review:7] 审查完成: 评级=B 问题数=3","review_id":7,"duration_ms":9200}
```

日志涵盖全链路追踪：Webhook 接收 -> 任务分发 -> 仓库准备 -> 变更获取 -> Diff 过滤 -> Agent 审查循环（含 LLM 调用和工具调用计时） -> 评论发布 -> 结果持久化。所有日志通过 `review_id` 串联。

Docker 日志已配置自动轮转（max-size 50MB，保留 3 个文件），防止磁盘空间耗尽。

### 重启服务

```bash
# 重启所有服务
docker-compose restart

# 仅重启 API
docker-compose restart api

# 仅重启 Celery Worker（不影响 API 接收 Webhook）
docker-compose restart celery
```

### 更新部署

```bash
git pull
docker-compose up --build -d
```

### 数据库迁移

当数据库模型发生变更时，需要生成并执行迁移：

```bash
# 进入 API 容器
docker-compose exec api bash

# 生成迁移文件
alembic revision --autogenerate -m "描述本次变更"

# 执行迁移
alembic upgrade head
```

### 停止服务

```bash
# 停止所有容器（保留数据）
docker-compose down

# 停止所有容器并删除数据卷（清除数据库和缓存）
docker-compose down -v
```

## 项目结构

```
obc_ai_code_review/
├── .env                          # 环境变量配置（不提交）
├── .env.example                  # 环境变量模板
├── Dockerfile                    # 容器构建文件
├── docker-compose.yml            # 服务编排（4个服务）
├── pyproject.toml                # Python 项目依赖
├── alembic.ini                   # 数据库迁移配置
├── alembic/                      # 数据库迁移脚本
├── src/
│   ├── main.py                   # FastAPI 应用入口
│   ├── config.py                 # 配置管理（Pydantic Settings）
│   ├── api/                      # API 路由
│   │   ├── router.py             # 路由聚合（/api/v1 前缀）
│   │   └── v1/
│   │       ├── webhooks.py       # Webhook 接收端点
│   │       └── reviews.py        # 审查记录查询端点
│   ├── core/                     # 核心业务逻辑
│   │   ├── review_engine.py      # AI 审查引擎（单/多Agent模式）
│   │   ├── agent_tools.py        # 6个Agent沙箱工具
│   │   ├── llm_client.py         # vLLM 客户端（重试+上下文裁剪+隐私过滤）
│   │   ├── gitlab_client.py      # GitLab API 客户端
│   │   ├── repo_manager.py       # Git 仓库管理
│   │   ├── dimensions.py         # 多维度审查定义（5维度+加权评分）
│   │   ├── context_manager.py    # 上下文长度管理（4阶段裁剪）
│   │   ├── privacy_filter.py     # 隐私过滤核心（3阶段流水线）
│   │   ├── privacy_rules.py      # 敏感数据检测规则库（7类规则）
│   │   ├── readme_generator.py   # AI_README 文档自动生成引擎
│   │   └── code_graph/           # 代码图模块（影响面分析）
│   │       ├── parser.py         # 多语言 AST 解析器
│   │       ├── store.py          # SQLite 持久化 + FTS5
│   │       ├── query.py          # 高层查询 API
│   │       └── incremental.py    # 基于 git diff 增量更新
│   ├── models/                   # 数据库模型
│   │   ├── review.py             # Review / ReviewComment
│   │   └── readme_record.py      # ReadmeRecord (文档生成历史)
│   ├── schemas/                  # 请求/响应 Schema
│   │   └── webhook.py            # Webhook Pydantic 模型
│   ├── tasks/                    # Celery 异步任务
│   │   ├── celery_app.py         # Celery 实例配置
│   │   ├── review_task.py        # 审查任务编排
│   │   └── readme_task.py        # AI_README 生成任务
│   └── utils/                    # 工具模块
│       ├── logger.py             # 日志基础设施（JSON/Text双格式）
│       ├── security.py           # Webhook Token + API Key 认证
│       ├── rate_limit.py         # Redis 固定窗口速率限制
│       └── ip_whitelist.py       # IP 白名单中间件
└── tests/                        # 测试
    └── unit/
        ├── test_agent_tools.py
        ├── test_context_manager.py
        ├── test_dimensions.py
        ├── test_privacy_filter.py
        ├── test_review_engine.py
        ├── test_review_task.py
        ├── test_readme_generator.py
        ├── test_readme_task.py
        ├── test_llm_client.py
        ├── test_webhooks.py
        ├── test_code_graph.py
        └── test_gitlab_client.py
```

## 常见问题

### Webhook 返回 401 Unauthorized

检查 GitLab Webhook 配置中的 Secret token 是否与 `.env` 中的 `GITLAB_WEBHOOK_SECRET` 一致。

### 审查任务失败，日志显示 LLM API 调用错误

1. 确认 `VLLM_API_URL` 地址是否可从容器内部访问
2. 确认 `VLLM_API_KEY` 是否正确
3. 确认 `VLLM_MODEL_NAME` 与 vLLM 服务部署的模型一致
4. 检查 vLLM 服务是否正常运行

### 审查任务失败，日志显示 git clone 错误

1. 确认 `GITLAB_TOKEN` 拥有被审查项目的读取权限
2. 确认 `GITLAB_URL` 地址是否可从容器内部访问
3. 如果 GitLab 使用自签名证书，可能需要在 Dockerfile 中配置证书信任

### 审查报告没有出现在 GitLab 上

1. 确认 `GITLAB_TOKEN` 拥有项目的 API 写入权限（api scope）
2. 查看 Celery 日志确认任务是否执行成功
3. 通过审查记录 API 查看 status 和 error_message：`GET /api/v1/reviews`

### 如何调整审查的触发分支

修改 `.env` 中的 `MAIN_BRANCHES` 配置项，多个分支用逗号分隔：

```ini
MAIN_BRANCHES=main,master,develop
```

修改后重启服务：`docker-compose restart api celery`

### 审查只有部分维度结果

检查 `REVIEW_DIMENSION_TIMEOUT` 是否过短（默认 180 秒），查看 Celery 日志中的维度超时信息。可通过增大超时值或降低 `REVIEW_MAX_CONCURRENCY` 来缓解。

### 如何启用双模型模式

设置以下两个环境变量并重启服务：

```ini
CLOUD_LLM_MODEL_NAME=your-cloud-model-name
PRIVACY_FILTER_ENABLED=true
```

### 如何关闭多维度审查

设置 `REVIEW_SINGLE_AGENT_MODE=true` 并重启服务，将回退到单 Agent 全维度审查模式。

### API 返回 429 Too Many Requests

已触发速率限制，等待响应头中 `Retry-After` 指定的秒数后重试，或调整 `.env` 中的 `RATE_LIMIT_*` 配置。

### API 返回 401 Unauthorized（Reviews API）

检查请求中 `X-API-Key` header 是否正确，确认 `.env` 中 `API_KEYS` 配置包含该密钥。

### 如何启用 AI_README 文档自动生成

设置 `.env` 中 `README_GENERATION_ENABLED=true` 并重启服务：

```bash
docker-compose down
docker-compose up --build -d
docker-compose exec api alembic upgrade head   # 首次启用需确保 readme_records 表已迁移
```

启用后，每次审查成功会链式分发 `generate_readme` 任务：仅对代码行数 ≥ `README_MIN_CODE_LINES`（默认 200）的子目录生成/更新文档；已存在文档通过结构指纹（根目录名单+关键配置 / 子目录代码文件哈希）判断是否需要更新；文档通过 GitLab Commits API 批量提交，作者为 `AI-README Bot`、commit message 前缀 `[AI-README]`。

### AI_README 生成后触发了新一轮审查

不会。Webhook 层对 Push 与 MR 事件都做了**多层防递归过滤**：

**Push 事件三层过滤**：
1. 分支名以 `README_TEMP_BRANCH_PREFIX`（默认 `ai-readme/`）开头 → 忽略（受保护主分支绕过流程产生的临时分支推送）
2. 本次推送所有 commit message 均以 `[AI-README]` 开头 → 忽略（文档自动直接提交）
3. commit_sha 命中 MR merge 抑制标记（5 分钟 Redis TTL）→ 忽略

**MR 事件两层过滤**（任一命中即忽略）：
- source_branch 以 `README_TEMP_BRANCH_PREFIX` 开头
- title 以 `README_MR_TITLE_PREFIX`（默认 `[AI-README]`）开头

**任务层 + DB 层兜底**：
- `run_review` 入口按 (project_id, commit_sha) 查询近 30 分钟是否已有审查 → 命中则跳过
- `readme_records` 表 partial unique index 保证同一 commit 仅成功生成一次

如需自定义前缀，修改 `.env` 中对应配置项即可（webhook 从 settings 读取，无需改代码）。

### AI_README 配置受保护主分支后无法 commit

代码会自动处理：探测主分支 `protected` 字段，受保护时走"临时分支 → create_commit → create_merge_request → (可选) accept_merge_request"绕过。需要确保 `GITLAB_TOKEN` 拥有：

- 创建分支与 commit 的写入权限
- 创建 MR 的权限
- 若 `README_MR_AUTO_MERGE=true`：合并到主分支的权限

`README_MR_AUTO_MERGE=false` 时仅创建 MR 等待人工合并；合并失败（如 pipeline 未完成、冲突）会降级 warning 不阻塞审查流程。

## 参与贡献

欢迎任何形式的贡献！请先阅读：

- [CONTRIBUTING.md](CONTRIBUTING.md) — 开发环境、测试命令、代码与提交规范、硬纪律表
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — 社区行为准则
- [SECURITY.md](SECURITY.md) — 安全边界说明与漏洞报告方式
- [CHANGELOG.md](CHANGELOG.md) — 版本变更历史
- [LICENSE](LICENSE) — MIT License

**架构与设计决策记录**：详见 `CLAUDE.md` / `AGENTS.md`（两个文件内容同步），其中记录了多维度审查、隐私过滤流水线、影响面预分析、受保护主分支绕过流程、防递归三层防御、"README 链路不发邮件"结构不变量等关键设计决策。

## WSL 本机验证

Project Brain 可先在独立 WSL/Docker 环境验证，默认端口 18080、关闭 GitLab 写回和审查 Worker。仅在试点项目配置同一 Webhook 即可自动首建；见 [完整本机验证步骤](docs/project-brain-local-test.md)。

成本优化保留完整事实、关系和证据，使用语义字段复用、紧凑上下文及可恢复预算控制；实际 Token、费用估算与质量验证口径见 [成本与质量控制](docs/project-brain-cost.md)。正式知识需通过源码和证据校验，不能把结构通过当作业务理解正确。

知识概览按模块归并，再由完整模块集合形成项目概览；完整解释、未知项和来源同时保留，概览仍标注为待独立核验的 AI 推断。首层模块解释读取完整符号关系，上层使用明确标注的解析状态统计视图；具体依赖与调用路径仍需查阅详细知识和完整图。独立规划任务在已校验输出保存后推进下一层，规划失败不会回滚付费生成结果。任务恢复、综合协议和质量验收见 [分层汇总说明](docs/project-brain-hierarchy.md)。

隐私发现只在 Worker 私有卷保存不可逆来源。原提交含硬编码凭证及同值路径时，使用私有规范身份和安全公开投影，遮蔽显示路径并关闭原始直链；源码SHA、blob和行范围仍可认证。新构建先继承同项目已认证发现，规则发现也跨单元持久保存；缺失索引、历史材料或封存不一致时停止，不能以空结果撤销已有发现。汇总在完整来源认证后减少机器字段的重复分类，全部解释、未知项与展示路径继续扫描，完整原包仍重放规则和HMAC。详见[敏感来源设计](docs/project-brain-sensitive-source-design.md)、[历史发现继承](docs/project-brain-privacy-inheritance.md)、[汇总隐私投影](docs/project-brain-summary-privacy.md)及[验收记录](docs/project-brain-sensitive-source-validation.md)。

迁移`007_brain_usage`后，新构建提供`GET /api/v1/projects/{project_id}/brain/builds/{build_id}/usage`，按隐私/语义模型、阶段与用途查询实际或估算用量；旧记录保留总量，不推算模型占比。并发、未知预留与部署顺序见 [分模型用量](docs/project-brain-model-usage.md)。

2.1.0 通过迁移 `008_brain_http` 提供显式有界 HTTP 恢复。临时网关错误逐次计量和退避，失败费用上界保留，输出隐私扫描恢复时保留原语义响应；超时或响应丢失仍停止。见 [模型 HTTP 重试与封存约束](docs/project-brain-http-retry.md)。

2.2.0 增加迁移 `009_brain_recovery` 和认证检查点恢复。局部传输修复可继承兼容的成功扫描与编译结果，只重做失效或未完成部分；新旧调用、来源和费用分别保留。见 [恢复规则、历史证明与接口](docs/project-brain-checkpoint-recovery.md)。

2.2.1 修复恢复读取时的软超时误分类和当前隐私索引并发读取问题，保留原认证及费用门禁。见 [中断处理与运行边界](docs/project-brain-interrupt-recovery.md)。
