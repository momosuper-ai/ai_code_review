# OBC AI Code Review - 三层代码质量保障平台

## Context

团队（30~50人）需要一套持续监控代码质量的系统，采用三层质量保障模型：
1. **SonarQube 静态规则** — 确定性问题检测
2. **AI 解释 & 优化建议** — 智能增强（私有化 LLM，已部署）
3. **人工 Reviewer** — 业务判断

**已有基础设施**：
- GitLab 私有部署
- Docker/K8s 环境
- PostgreSQL/MySQL
- vLLM + Devstral-Small-2-24B（已部署，OpenAI 兼容 API）

**团队特点**：多语言混合技术栈（Java/Python/Go/前端等），中文为主要语种

---

## 一、系统架构总览

```
┌──────────────────────────────────────────────────────────────┐
│                       GitLab (已有)                           │
│                                                              │
│  Developer 创建/更新 MR                                       │
│        │                                                     │
│        ├──────────────────┬──────────────────┐               │
│        ▼                  ▼                  ▼               │
│   GitLab CI           Webhook            人工 Review         │
│   sonar-scanner       (MR 事件)          (GitLab MR 原生)    │
│        │                  │                  ▲               │
│        ▼                  ▼                  │               │
│   SonarQube          FastAPI 服务       参考 SonarQube       │
│   Server             (AI 审查)         + AI 审查结果         │
│   (Docker)                │                                  │
│        │                  ▼                                  │
│        │          vLLM + Devstral                             │
│        │           (已有, 独立)                                │
│        │                  │                                  │
│        │     ┌────────────┘                                  │
│        ▼     ▼                                               │
│  结果回写 MR 评论（中文）                                      │
│        │     │                                               │
│        ▼     ▼                                               │
│     PostgreSQL (审查记录存储)                                  │
└──────────────────────────────────────────────────────────────┘

两条审查线并行运行，互不依赖：
  - SonarQube：确定性规则检测（Bug、漏洞、坏味道、覆盖率）
  - AI 审查：语义理解（设计合理性、命名、更优实现、业务逻辑）
```

## 二、技术选型

| 组件 | 技术方案 | 说明 |
|------|---------|------|
| 静态分析 | SonarQube Community + Branch Plugin (Docker) | 支持 MR 装饰，免费 |
| AI 引擎 | vLLM + Devstral-Small-2-24B（已有） | OpenAI 兼容 API，无需本项目管理 |
| 后端服务 | Python 3.12 + FastAPI | Webhook 接收、任务调度、API |
| 任务队列 | Celery + Redis | 异步处理 AI 审查任务，避免阻塞 |
| 数据存储 | PostgreSQL | 存储审查记录、指标数据、配置 |
| 缓存 | Redis | 任务队列 + 接口缓存 |
| 前端 | SonarQube 自带前端（初期） | 后续按需开发 Vue 3 Dashboard |
| 中文化 | SonarQube 中文插件 + AI 中文输出 | 全链路中文体验 |
| 容器化 | Docker Compose (开发) / K8s (生产) | 一键部署 |

## 三、核心模块设计

### 3.1 第一层：SonarQube 静态分析

**部署方式**：Docker Compose，使用 `mc1arke/sonarqube-with-community-branch-plugin` 镜像（支持多分支分析和 MR 装饰）

**中文化**：安装 [sonar-l10n-zh](https://github.com/xuhuisheng/sonar-l10n-zh) 中文语言包插件，将 SonarQube Web 界面汉化

**集成流程**：
1. GitLab CI 中配置 `sonar-scanner` 阶段
2. MR 触发时自动扫描变更代码
3. 结果通过 SonarQube GitLab 集成回写到 MR 评论
4. Quality Gate 状态同步到 MR Pipeline

**配置要点**：
- 为团队多语言栈配置统一 Quality Profile
- 自定义 Quality Gate：覆盖率 ≥ 80%，重复率 ≤ 3%，安全评级 A
- 通过 SonarQube Webhook 将分析结果推送到 FastAPI 服务，统一存储

**硬件需求**（30-50 人团队）：
- CPU：4-8 核
- RAM：8-16 GB
- 存储：SSD 50GB+
- 数据库：使用已有 PostgreSQL（独立 schema）

### 3.2 第二层：AI 智能审查

**模型**：对接已有的 vLLM + Devstral-Small-2-24B 服务（OpenAI 兼容 API）

**AI 审查输入**：MR 的 Diff（变更代码），与 SonarQube 并行独立运行
- SonarQube 关注：确定性规则（空指针、SQL注入、复杂度、覆盖率等）
- AI 关注：语义层面（设计合理性、命名清晰度、更优实现、潜在业务逻辑问题）
- 两者互补，不重复

**AI 审查流程**：
```
GitLab MR Webhook (merge_request open/update)
       │
       ▼
FastAPI 接收事件，验证签名
       │
       ▼
调用 GitLab API 获取 MR Diff
       │
       ▼
Celery 异步任务：
  1. 按文件拆分 Diff
  2. 过滤无需审查的文件（lock 文件、生成代码等）
  3. 结合项目 .code-review-rules.yml 构建 Prompt
  4. 调用 vLLM API（OpenAI 兼容格式）逐文件审查
  5. 聚合结果，生成结构化报告
       │
       ▼
通过 GitLab API 发布审查评论（中文）：
  - 行内评论（具体问题定位到代码行）
  - 总结评论（整体评价 + 优化建议）
       │
       ▼
存储审查记录到 PostgreSQL
```

**Prompt 设计策略**：
- 基于项目级别的 `.code-review-rules.yml` 配置审查规则
- 支持按语言定制审查重点（如 Java 关注空指针、Go 关注 goroutine 泄漏）
- 审查维度：安全性、性能、可维护性、代码风格、潜在 Bug
- **所有输出强制中文**，降低团队阅读门槛
- 输出格式：JSON 结构化，便于解析和回写 GitLab

### 3.3 第三层：人工 Reviewer

**基于 GitLab 原生 MR Review 流程增强**：
- AI 审查完成后自动添加 Label 标识审查状态（如 `ai-reviewed`）
- 人工 Reviewer 在 MR 评论区同时看到 SonarQube + AI 两层结果作为参考
- 支持对 AI 审查结果进行反馈（采纳/忽略），用于持续优化 Prompt

### 3.4 后端服务 (FastAPI)

**项目结构**：
```
obc_ai_code_review/
├── CLAUDE.md                    # 项目规范文档（每次修改必读必更新）
├── plan.md                      # 项目计划文件
├── docker-compose.yml           # 开发环境一键启动
├── docker-compose.prod.yml      # 生产环境配置
├── .env.example                 # 环境变量模板
├── pyproject.toml               # Python 项目配置
│
├── src/
│   ├── main.py                  # FastAPI 应用入口
│   ├── config.py                # 配置管理
│   │
│   ├── api/                     # API 路由层
│   │   ├── v1/
│   │   │   ├── webhooks.py      # GitLab/SonarQube Webhook 接收
│   │   │   ├── reviews.py       # 审查记录查询
│   │   │   ├── projects.py      # 项目管理
│   │   │   └── rules.py         # 审查规则管理
│   │   └── router.py
│   │
│   ├── core/                    # 核心业务逻辑
│   │   ├── review_engine.py     # AI 审查引擎
│   │   ├── sonar_client.py      # SonarQube API 客户端
│   │   ├── gitlab_client.py     # GitLab API 客户端
│   │   ├── llm_client.py        # vLLM API 客户端（OpenAI 兼容）
│   │   ├── prompt_builder.py    # Prompt 构建器
│   │   └── diff_parser.py       # Diff 解析器
│   │
│   ├── models/                  # 数据库模型 (SQLAlchemy)
│   │   ├── review.py            # 审查记录
│   │   ├── project.py           # 项目配置
│   │   └── rule.py              # 审查规则
│   │
│   ├── schemas/                 # Pydantic 数据模型
│   │   ├── webhook.py           # Webhook 请求体
│   │   └── review.py            # 审查相关
│   │
│   ├── tasks/                   # Celery 异步任务
│   │   └── review_task.py       # AI 审查任务
│   │
│   └── utils/                   # 工具函数
│       ├── security.py          # Webhook 签名验证
│       └── logger.py            # 日志配置
│
├── deploy/                      # 部署配置
│   ├── sonarqube/
│   │   └── docker-compose.yml   # SonarQube 独立部署
│   └── nginx/
│       └── nginx.conf           # 反向代理
│
├── tests/                       # 测试
│   ├── unit/
│   ├── integration/
│   └── conftest.py
│
└── alembic/                     # 数据库迁移
    └── versions/
```

**核心 API 设计**：

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/v1/webhooks/gitlab` | POST | 接收 GitLab MR 事件，触发 AI 审查 |
| `/api/v1/webhooks/sonarqube` | POST | 接收 SonarQube 分析结果，存储指标 |
| `/api/v1/reviews` | GET | 查询审查记录列表 |
| `/api/v1/reviews/{id}` | GET | 查看审查详情 |
| `/api/v1/reviews/{id}/feedback` | POST | 人工反馈 AI 审查结果（采纳/忽略） |
| `/api/v1/projects` | GET/POST | 项目管理 |
| `/api/v1/projects/{id}/rules` | GET/PUT | 项目审查规则配置 |
| `/health` | GET | 健康检查 |

### 3.5 数据库设计（核心表）

```
projects          - 项目配置（GitLab 项目 ID、SonarQube Key、审查规则）
reviews           - 审查记录（MR ID、触发时间、状态、AI 结论摘要）
review_comments   - 审查评论（文件、行号、问题类型、严重级别、建议内容）
review_feedbacks  - 人工反馈（采纳/忽略、反馈原因，用于优化 Prompt）
sonar_results     - SonarQube 分析结果快照（通过 Webhook 接收）
```

### 3.6 前端策略

**初期**：不开发独立前端
- SonarQube 自带 Web 前端（安装中文插件后）：查看静态分析指标、Quality Gate、问题列表
- GitLab MR 评论区：查看 AI 审查意见（中文）
- FastAPI 自带 Swagger UI：管理项目配置和审查规则

**后续按需**（Phase 5）：如果有聚合展示需求，开发 Vue 3 Dashboard
- 三层综合质量评分
- 按开发者/团队维度的统计
- AI 审查历史和反馈分析

## 四、部署架构

### 开发环境（Docker Compose 一键启动）

```yaml
services:
  api:         FastAPI (port 8000)
  celery:      Celery Worker
  postgres:    PostgreSQL 16
  redis:       Redis 7
  # SonarQube 独立 compose 部署（deploy/sonarqube/）
  # vLLM 已有，无需管理
```

### 生产环境

| 服务 | 部署方式 | 配置 |
|------|---------|------|
| SonarQube | Docker (独立主机) | 8C/16G/100G SSD |
| vLLM + Devstral | 已有，独立服务 | — |
| FastAPI + Celery | Docker / K8s | 4C/8G，可水平扩展 |
| PostgreSQL | 使用已有实例 | 独立 database |
| Redis | Docker | 2G 内存 |

## 五、实施路线

### Phase 1：基础框架搭建
- 初始化项目结构
- 创建 CLAUDE.md 项目规范文件
- FastAPI 项目脚手架（路由、数据库、配置管理）
- Docker Compose 开发环境（FastAPI + PostgreSQL + Redis + Celery）
- 数据库模型和 Alembic 迁移

### Phase 2：SonarQube 集成
- Docker 部署 SonarQube + Community Branch Plugin + 中文语言包
- GitLab CI 配置 sonar-scanner（.gitlab-ci.yml 模板）
- SonarQube ↔ GitLab 集成配置（MR 装饰、Quality Gate）
- SonarQube Webhook → FastAPI 接收分析结果并存储

### Phase 3：AI 审查引擎
- GitLab Webhook → FastAPI 接收 MR 事件
- Diff 解析器（按文件拆分、过滤无关文件）
- Prompt 构建器（加载项目规则、构建中文审查 Prompt）
- vLLM API 客户端（OpenAI 兼容格式）
- Celery 异步审查任务（含重试、超时机制）
- 审查结果回写 GitLab MR 评论（行内 + 总结）

### Phase 4：完善和优化
- 人工反馈接口（采纳/忽略 AI 建议）
- 审查规则管理 API
- 性能调优（大 MR 拆分策略、并发控制）
- 监控和日志完善

### Phase 5：按需扩展（可选）
- Vue 3 Dashboard（如果有聚合展示需求）
- 定时采集 SonarQube 指标做趋势分析
- Prompt 自动优化（基于反馈数据）

## 六、验证方案

1. **SonarQube 验证**：创建测试项目，提交包含已知问题的代码（如 SQL 注入、空指针），确认 SonarQube 能检出并装饰 MR，且界面为中文
2. **AI 审查验证**：提交包含典型问题（命名不清晰、冗余逻辑、缺少错误处理）的 MR，确认 AI 能用中文给出合理建议
3. **端到端验证**：MR 创建 → SonarQube 扫描 + AI 审查并行 → 结果回写 MR → 人工查看并反馈
4. **并发测试**：模拟多个 MR 同时提交，验证 Celery 任务队列和 vLLM 的稳定性

## 七、关键风险和应对

| 风险 | 应对策略 |
|------|---------|
| AI 审查延迟过长 | 按文件拆分并行审查；设置超时机制；大 MR 只审查关键文件 |
| LLM 幻觉/误报 | 结构化 Prompt + 审查规则约束；人工反馈持续优化 |
| SonarQube 社区版功能限制 | 使用 Community Branch Plugin 补齐多分支能力 |
| vLLM 服务不可用 | 审查任务重试机制；降级为仅 SonarQube 审查 |
| 中文输出质量不稳定 | Prompt 中强制要求中文 + 输出格式约束；持续调优 |
