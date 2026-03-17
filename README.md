# AI 代码审查服务

基于大语言模型的自动化代码审查服务。通过 GitLab Webhook 自动触发，利用 vLLM 提供的大模型能力对代码变更进行全面审查（潜在缺陷、安全漏洞、性能问题、代码质量、设计模式、代码规范），并将审查报告以评论形式回写到 GitLab 的 Merge Request 或 Commit 上。

## 功能特性

- **自动触发**：监听 GitLab Webhook，在 MR 合并或推送到主分支时自动启动审查
- **智能审查**：Agent 模式驱动 LLM 进行多轮分析，可自主查阅代码上下文以做出准确判断
- **安全沙箱**：LLM 只能通过 5 个受限工具访问被审查项目的代码，无法执行任意命令
- **结果回写**：审查报告格式化为 Markdown 评论，自动发布到 GitLab
- **历史记录**：所有审查记录和问题明细持久化到 PostgreSQL，支持通过 API 查询
- **异步处理**：基于 Celery + Redis 的异步任务队列，Webhook 即时响应不阻塞

## 系统架构

```
GitLab Webhook (MR merged / Push)
       │
       ▼
  FastAPI 接收 Webhook
  (验证 Token → 解析事件 → 过滤)
       │
       ▼
  Celery 异步任务队列
       │
       ├── 1. 克隆/更新本地代码库
       ├── 2. 获取代码变更 (diff)
       ├── 3. AI Agent 审查循环
       │      LLM 分析 diff
       │        ↕ 调用工具查看上下文
       │      输出结构化审查结果
       ├── 4. 格式化 Markdown 报告
       ├── 5. 回写 GitLab 评论
       └── 6. 持久化审查记录到数据库
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

编辑项目根目录下的 `.env` 文件，根据你的实际环境修改以下配置：

```ini
# ==================== 数据库 ====================
# Docker Compose 内部通信地址，通常无需修改
DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/obc_review

# ==================== Redis ====================
# Docker Compose 内部通信地址，通常无需修改
REDIS_URL=redis://redis:6379/0

# ==================== GitLab 配置 ====================
# GitLab 实例地址（末尾带斜杠）
GITLAB_URL=https://your-gitlab.example.com/

# GitLab Personal Access Token（需要 api 权限）
# 生成方式：GitLab → 用户设置 → Access Tokens → 创建新 Token，勾选 api scope
GITLAB_TOKEN=your-gitlab-token

# Webhook 验证密钥，用于验证请求来自 GitLab
# 你可以自定义一个强密码，后续在 GitLab Webhook 配置中需要填入相同的值
GITLAB_WEBHOOK_SECRET=your-webhook-secret

# ==================== vLLM 配置 ====================
# vLLM 服务地址（OpenAI 兼容 API），需要 /v1 后缀
VLLM_API_URL=http://your-vllm-host:port/v1

# vLLM API Key
VLLM_API_KEY=your-api-key

# 模型名称，需与 vLLM 部署的模型一致
VLLM_MODEL_NAME=mistralai/Devstral-Small-2-24B-Instruct-2512

# ==================== 应用配置 ====================
APP_ENV=development
LOG_LEVEL=INFO

# 日志格式："text"（开发阶段可读格式）或 "json"（生产环境结构化格式）
LOG_FORMAT=text

# 日志文件路径（留空则仅输出到 stdout，设置后同时写入文件并自动轮转）
LOG_FILE=/data/logs/review.log
LOG_FILE_MAX_BYTES=50000000
LOG_FILE_BACKUP_COUNT=5

# 本地代码库存储路径（容器内路径，通常无需修改）
REPOS_BASE_DIR=/data/repos

# Agent 最大工具调用轮数（每次审查中 LLM 最多调用工具的次数）
AGENT_MAX_TOOL_ROUNDS=10

# 触发审查的主分支名（逗号分隔，代码合并或推送到这些分支时才会触发审查）
MAIN_BRANCHES=main,master
```

#### GitLab Token 获取方法

1. 登录 GitLab，点击右上角头像 → **Edit profile**
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
| `api` | FastAPI Web 服务 | 8000 |
| `celery` | Celery 异步任务 Worker | - |
| `postgres` | PostgreSQL 数据库 | 5432 |
| `redis` | Redis 消息队列 | 6379 |

首次启动时，数据库表会自动创建，无需手动执行迁移。

### 第四步：验证服务状态

```bash
# 检查健康状态
curl http://localhost:8000/health
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

1. 进入 GitLab 项目页面，左侧菜单选择 **Settings → Webhooks**
2. 填写配置：
   - **URL**：`http://<你的服务器IP>:8000/api/v1/webhooks/gitlab`
   - **Secret token**：填入 `.env` 中 `GITLAB_WEBHOOK_SECRET` 的值
   - **Trigger**：勾选以下事件
     - ✅ **Merge request events**（MR 合并时触发审查）
     - ✅ **Push events**（推送到主分支时触发审查）
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
curl -X POST http://localhost:8000/api/v1/webhooks/gitlab \
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

## 触发审查的条件

并非所有 GitLab 事件都会触发审查，仅以下情况会启动 AI 审查：

### Merge Request 事件

- MR 被**合并**（action 为 merge）
- 目标分支是主分支（在 `MAIN_BRANCHES` 配置的分支列表中）

以下情况**不会**触发：MR 创建、更新、关闭、重新打开，以及目标分支非主分支的 MR 合并。

### Push 事件

- 推送目标是主分支
- 不是新建分支的推送（before SHA 不全为零）
- 包含至少一个 commit

以下情况**不会**触发：推送到非主分支、创建新分支、强制推送空变更。

## 审查报告说明

AI 审查完成后，会在 GitLab 的 MR 或 Commit 下方发布一条评论，内容包括：

- **评级**：A（优秀）/ B（良好）/ C（需改进）/ D（需重写）
- **整体评价**：对本次代码变更的总体评价
- **问题列表**：逐一列出发现的问题，每个问题包含：
  - 严重程度图标（🔴 Critical / 🟠 Major / 🟡 Minor / 🔵 Info）
  - 问题分类（bug / security / performance / maintainability / style / design）
  - 文件路径和行号
  - 问题描述和修改建议
- **亮点**：代码中值得肯定的部分

## API 接口

除了 Webhook 接收端点外，服务还提供以下查询接口：

### 健康检查

```
GET /health
```

返回：`{"status": "ok"}`

### 审查记录列表

```
GET /api/v1/reviews?project_id=1&status=completed&page=1&page_size=20
```

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
      "created_at": "2026-03-16T10:00:00",
      "completed_at": "2026-03-16T10:02:30"
    }
  ]
}
```

### 审查详情

```
GET /api/v1/reviews/{review_id}
```

返回审查记录的完整信息，包含所有问题评论明细（文件路径、行号、严重程度、分类、描述、建议等）。

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

日志涵盖全链路追踪：Webhook 接收 → 任务分发 → 仓库准备 → 变更获取 → Diff 过滤 → Agent 审查循环（含 LLM 调用和工具调用计时） → 评论发布 → 结果持久化。所有日志通过 `review_id` 串联。

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
├── .env                          # 环境变量配置
├── Dockerfile                    # 容器构建文件
├── docker-compose.yml            # 服务编排
├── pyproject.toml                # Python 项目依赖
├── alembic.ini                   # 数据库迁移配置
├── alembic/                      # 数据库迁移脚本
├── src/
│   ├── main.py                   # FastAPI 应用入口
│   ├── config.py                 # 配置管理
│   ├── api/                      # API 路由
│   │   └── v1/
│   │       ├── webhooks.py       # Webhook 接收端点
│   │       └── reviews.py        # 审查记录查询端点
│   ├── core/                     # 核心业务逻辑
│   │   ├── review_engine.py      # AI 审查引擎
│   │   ├── agent_tools.py        # Agent 沙箱工具
│   │   ├── llm_client.py         # vLLM 客户端
│   │   ├── gitlab_client.py      # GitLab API 客户端
│   │   └── repo_manager.py       # Git 仓库管理
│   ├── models/                   # 数据库模型
│   │   └── review.py             # Review / ReviewComment
│   ├── schemas/                  # 请求/响应 Schema
│   ├── tasks/                    # Celery 异步任务
│   │   ├── celery_app.py         # Celery 实例配置
│   │   └── review_task.py        # 审查任务编排
│   └── utils/                    # 工具模块
└── tests/                        # 测试
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
