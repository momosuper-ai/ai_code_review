# Changelog

本项目的所有重要变更都记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-09-14

首个公开版本。基于 GitLab Webhook 的 AI 代码审查服务，本地 vLLM + 云端 LLM 双模型架构。

### Added

- **多维度并行审查引擎**：5 个专业维度 Agent（bug/security/performance/quality/architecture）经 `asyncio.gather` + Semaphore 并行审查，跨维度去重 + 加权评分（任一维度 D 或 critical 强制降至 C）；支持单 Agent 全维度回退模式
- **Agent 沙箱工具**：6 个受限工具（read_file、search_code、list_directory、find_definition、get_file_info、get_blast_radius），路径越界检查 + 输出截断
- **隐私过滤**：规则引擎 + 本地 LLM 混合扫描，3 阶段流水线（diff 预扫描 → 工具结果扫描 → 反脱敏），7 类敏感数据规则、3 个检测等级
- **双模型架构**：本地 vLLM 负责隐私扫描，云端 LLM 负责代码审查；按维度 + 按模型统计 token 用量并持久化
- **代码图（影响面分析）**：多语言 AST 解析（Python/JS/TS/Java/Go）+ SQLite 持久化 + FTS5 检索；基于 git diff 增量更新；基于 diff hunk 变更行精确追踪受影响符号的上下游依赖并预注入 prompt
- **AI_README 文档自动生成**：结构指纹（SHA-256）跳过无变化项目；首次 Agent 工具循环生成、后续单次 LLM 调用增量更新；push/MR merge 双触发路径；受保护主分支自动走"临时分支 → commit → 自动 MR → 自动合并"绕过；三层防递归过滤 + DB partial unique index 兜底
- **结构化输出强约束**：`response_format` 三档模式（json_schema/json_object/off），云端/本地模型独立覆盖；格式重试温度递减 + schema 透传 + 降级 fallback 解析
- **可靠性**：LLM 调用指数退避重试、上下文 4 阶段自动裁剪、Webhook 幂等三层防御（Redis 标记 + 任务去重 + DB 唯一索引）、MR merge 仅触发一次语义
- **安全防护**：Webhook Token 验证、API Key 认证、IP 白名单（CIDR）、Redis 固定窗口速率限制（超限 429 + Retry-After）
- **可观测性**：结构化日志（text/json 双格式 + 文件轮转）、`review_id` 全链路追踪、各阶段耗时统计、Docker 日志轮转
- **审查记录 API**：分页列表 + 详情查询（含问题明细与维度归属）
