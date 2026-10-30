# ThinkRail 与 Project Brain 结合评估

核查日期：2026-10-26。本文区分官方实现、结合建议与尚未实测的能力，不代表已经安装或接入。

## 结论

ThinkRail 可以成为 Project Brain 知识的开发者使用入口，也值得借鉴其轻量规格图和按需读取方式。优先方案是保留 Project Brain 的固定版本、来源/隐私校验、账目和任务生命周期，通过只读适配层向 ThinkRail 提供已验收知识；需求、设计决策与自动生成事实各自保留来源和权威级别。直接替换当前后台生成流水线，或把切换 IDE 当作构建加速方案，都没有现成证据支持。

## 核查版本与证据边界

- 官方仓库为 [JetBrains/thinkrail](https://github.com/JetBrains/thinkrail)，产品由 pi 引擎、宿主与 Web/桌面界面组成。本文源码链接固定到 main 提交 [`d299f20d2ba014f382ddbcc9b49f2aa63fada132`](https://github.com/JetBrains/thinkrail/commit/d299f20d2ba014f382ddbcc9b49f2aa63fada132)，提交时间 2026-10-25T13:39:01Z；不能将 main 的全部能力视为稳定发布包已包含。
- 当前核查到的稳定发布为 [v0.2.0](https://github.com/JetBrains/thinkrail/releases/tag/v0.2.0)，发布于 2026-10-18，对应提交 `7641d7df9f78efd5ac8fe9345d52dea24779434e`。本文重点为上面的固定 main 源码审计，未逐项复验 v0.2.0 二进制行为。
- 已核查 spec-graph 的实际入口、解析/索引/查询/建档代码，以及 MCP 配置与调用审批代码、会话重启修复代码；未运行安装脚本、产品、模型调用或业务项目试验。
- ThinkRail 的 TypeScript/Bun 组件不能直接作为当前 Python 服务的同进程依赖。跨进程 MCP/HTTP 或生成文件适配需要另行实现和测试；本文没有宣称已有官方 Project Brain 集成。

## 已确认的 spec-graph 能力

规格不是单一大文档。Markdown 的 YAML frontmatter 以 `id` 和 `type` 识别节点；创作工具还要求 `title`。`parent` 构成层次，`depends-on`、`references`、`implements` 建立横向关系。状态包含 draft、active、stale、done、deprecated。节点表达设计意图、责任、边界和决策，不能等同于从源码自动抽取的调用图。依据：[解析器](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/spec-graph/core/parse.ts)、[官方技能说明](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/spec-graph/skills/spec-graph/SKILL.md)。

| 能力 | 实际行为 | 对本项目的意义 |
|---|---|---|
| `spec_grep` | 文本/正则搜索，按元数据筛选 | 可借鉴按模块定位知识，非向量检索 |
| `spec_get` | 返回元数据、路径、正反向关系，不返回正文 | 先查索引，再按需读取正文 |
| `spec_graph` | 有界子树、祖先或邻域切片 | 避免每次把整个项目知识塞入上下文 |
| `spec_create` | 创建 frontmatter 和标题骨架，正文由普通编辑工具补写 | 不是一键全仓语义建库 |
| `spec_update` | 只改 frontmatter，保留正文 | 结构维护与正文编辑分离 |
| `spec_delete` | 删除规格文件 | 默认 agent 工具集具有写能力 |
| `spec_validate` | 检查悬空链接、重复 id、parent 环 | 结构通过不代表事实正确、隐私通过或来源完整 |

依据：[工具注册](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/spec-graph/tools/index.ts)、[get 实现](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/spec-graph/tools/get.ts)、[create 实现](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/spec-graph/tools/create.ts)、[validate 实现](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/spec-graph/tools/validate.ts)、[查询实现](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/spec-graph/core/query.ts)。

加载策略是向 `before_agent_start` 增加“相关工作查相关规格、边界变化同步规格”的规则，再让 agent 按需查找。入口没有每轮拼接全部规格正文。官方模块明确把 semantic/embedding search 列为非目标。依据：[入口代码](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/spec-graph/index.ts)、[模块规格](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/spec-graph/SPEC.md)。

文件系统是规格权威，内存索引是派生缓存：每次读取重新遍历，按 mtime/size 判断是否重新解析，节点集变化后重建图。它有路径、符号链接与 Windows 路径边界保护，但该缓存不提供 Project Brain 的固定提交认证或 HMAC/隐私历史语义；不能拿文件时间替换认证。依据：[索引与路径实现](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/spec-graph/core/store.ts)。

独立复用也有包装限制：该固定版本中的 `pi-spec-graph` package 为 `private: true`、version `0.0.0`，虽然暴露 pi-free 的 `./core`，不能仅凭“portable”就承诺可通过公开 npm 包直接安装。依据：[package.json](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/spec-graph/package.json)。

## 只读接入与 MCP

ThinkRail MCP 支持 stdio 和 streamable HTTP，配置从用户 agent 目录的 `mcp.json` 与项目 `.pi/mcp.json` 读取。仓库配置需项目被信任，并且对应条目指纹得到批准；项目条目变化后旧批准不自动沿用。它为 MCP 暴露提供 direct/deferred/hidden 等策略，当前宿主把 codemode 规范化为 deferred。依据：[配置加载与校验代码](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/server/src/agent/mcp/config.ts)。

调用 guard 直接允许声明 `readOnlyHint: true` 的 MCP 工具；其余工具默认询问，可允许一次、当前聊天或整个项目；已有有效授权也会放行。该行为来自服务端声明和客户端策略，不是对远端工具实际副作用的证明。Project Brain 适配服务仍须独立校验身份、项目/版本、结果状态与读取范围，第一版仅暴露查询，不包含构建/恢复/暂停/发布等控制工具。依据：[MCP 调用 guard](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/server/src/agent/mcp/sessionHost.ts)。

只读 Specs viewer 不代表整个 agent 只读：默认 spec 扩展注册了 create/update/delete，普通 write/edit 也能改正文。因此有两种不同集成形式：

1. **优先：只读 MCP。** ThinkRail 查询已验收 Brain 版本及卡片，按需获取摘要与引用；关系邻域需要补充受控查询能力，当前没有独立关系 API。暂不复制完整源码，不暴露私有工作卷或模型账本。
2. **后选：生成文档查看入口。** 在隔离副本中展示安全的 Brain Markdown，保留版本、源码提交与未审阅标记；保持为描述性实现文档，不直接导入默认作为约束的 active spec 图。若以后提供专门投影，需额外定义生成文档的身份和权威规则。不能让 agent 修改导出文档后覆盖 Brain 规范 JSON；真正的文件只读需要权限或受控工具保证。

以上是建议的接口形式，尚无适配代码或兼容实测。

## 规格权威与生成事实必须分开

ThinkRail 的 always-on 规则把适用 specs 当作项目约束。Brain 自动生成解释仍可能是未审阅的推断。直接把所有生成卡片改名为 `SPEC.md` 并设 active，会把描述性输出抬高为规范性决策；这会影响后续 agent 如何改代码。

建议保留两类内容：维护者确认的需求/架构/决策作为设计意图，Brain 固定版本的事实与带证据解释作为实现知识。两者可交叉引用；发现冲突时报告差异，不能自动让源码推导覆盖需求，也不能把模型生成的“意图”当作维护者确认。官方 SDD 说明明确强调先维护规格、再实现与验证，规格不从代码生成，支持这个边界。依据：[官方 SDD 说明](https://thinkrail.ai/blog/thinkrail-sdd/)。对当前 `AGENT.md` 的运行历史，也不应原样转成 durable spec；可将稳定边界整理为精简设计记录，运行进度继续单独维护。这是结合建议，尚未落地。

## 与当前代码的具体映射

以下为本项目工作树代码核查，行号以本次阅读为准；说明已有工程能力，不表示当前真实试点已通过正式验收。

| 结合位置 | 当前入口与边界 | 需要新增的工作 |
|---|---|---|
| 版本与状态 | `src/api/v1/brain.py:267` 构建状态；`src/core/project_brain/lifecycle.py:352` freshness | 将查询锁定到已验收的 build/source_commit，并核对开发者工作区 HEAD；不同提交或有未提交修改时明确知识适用范围，不冒充最新事实 |
| 卡片与证据 | `src/api/v1/brain.py:318` 分页卡片，`:331` JSON/Markdown详情，`:349` ZIP；`schema.py:25` commit/blob/行范围 | 第一版包装 `brain_get_version`、`brain_list_cards`、`brain_get_card` 三个只读工具；名称仅为建议，尚未实现 |
| 产物可用性 | `brain.py:82` ready恢复依赖，`:111` DB清单认证，`:281` ready/保留状态，`artifacts.py:81,103` 文件认证 | 继续调用受控 API，不直接读取 Worker 私有卷或未完成检查点；ready没有冻结50题验收标记，适配层另核正式验收记录/版本准入白名单，不能把ready直接等同于业务质量通过 |
| 检索与图切片 | API 当前仅支持按类型/模块分页，尚无独立全文/自然语言或关系邻域查询 | 后续补确定性文本检索与有界关系切片，按版本返回证据与未知项；首版无需引入向量库或新付费探测 |
| 审查消费 | `src/tasks/review_task.py:219,244,247,257` 仍使用原 CodeGraph/ReviewEngine | 后续让现有审查 Agent 与 ThinkRail 共享相同知识读取适配层，不能称已经接入 Brain |
| 权限与隐私 | `brain.py:27` 统一 API key；`src/utils/security.py:14` 验证，没有独立读写角色凭据；公开来源使用 opaque locator | MCP服务端固定 GET 路由、项目和版本白名单并保管凭据；不能把全权限 API key 发给 Agent，也不能反解被遮蔽的原路径 |

ThinkRail 使用 Pi 的提供方配置，不会自动继承本服务的完整隐私模型、用量和授权边界。即使 Brain MCP 只返回安全投影，Agent 直接读取本地源码的其他路径也需独立评估；不能把 MCP 的只读标签当作源码经过隐私过滤的证明。现有 GitLab Webhook/发布链继续由本项目维护，ThinkRail 的 GitHub/gh PR 工作流不能视为 GitLab 集成已完成。

建议的组合是：维护者确认的规格约束与 Brain 的已验收实现知识一起交给 Agent 按需读取；Agent 的改动仍进入现有 GitLab MR 审查。后续可报告“规格有要求但实现未找到”“实现存在但规格未解释”“两者冲突或证据不足”，不自动覆盖任一方。可先用当前项目的暂停/继续、费用保护、隐私与发布边界整理少量规格，再推广到生产试点。

## 对构建速度的实际影响

ThinkRail 的按需读取主要减少交互阶段送入上下文的文档量，没有实测证明它能加速现有全仓生成。当前每单元 facts/来源/恢复认证、两个 Worker 执行槽和阶段屏障仍存在；schema2 设置 `incremental=False`（`src/tasks/brain_task.py:392`），finalize 建立 source_privacy 后提前返回（`:752–762`），绕过后面的旧跨提交编译缓存（`:764–774`）。源码索引的 mtime/size 缓存也不能代替本项目的来源与隐私认证。

因此性能优先级仍为安全分段测时、减少经认证作用域内的重复工作、接回安全的 schema2 普通增量，再评估并发。历史统计与推断限制见 [状态与性能报告](project-brain-status-performance-20261026.md)；该报告的旧运行版本/状态仅代表核查时刻，当前执行状态以根 `AGENT.md` 为准。不承诺接入 ThinkRail 后构建速度或准确率达到某个比例。

## 会话恢复不能替代 Brain 安全暂停

ThinkRail 确实保留会话并有重启修复。实际 `sessionRepair.ts` 检查 transcript 尾部未配对工具调用，对一般未完成工具补入“宿主在工具完成前重启”的错误结果；可继续回答的问题有专门的 ack。它修复对话协议，不会据此证明某次外部模型/数据库操作已经成功或未发生。依据：[重启修复代码](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/server/src/agent/sessionRepair.ts)。

官方 agent 规格区分显式 Stop、队列排空和 restart interrupted；还明确 Pi 的 abort 只作用于当前运行，队列可能触发新的 continuation。因此不能把聊天停止按钮映射成 Brain“已安全暂停”。Brain 仍应在服务端确认禁止新准入、在途单元完成隐私/记账/检查点后再确认 paused；ThinkRail 可以显示该状态和发起受控请求，但不能自行推导已安全暂停。依据：[agent 生命周期规格](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/server/src/agent/SPEC.md)。本节仅评价聊天会话恢复；仓库另有 pi-dag 后台编排模块，需单独审计，不能据此说整个项目没有持久任务能力。

## pi-dag 后台编排的可借鉴部分

固定 main 的 `packages/pi-dag` 是围绕 pi-delegation 的持久 DAG 包，`createDagService`/`createDagExtension` 是真实导出；包仍为 `private: true`、version `0.0.0`。官方边界声明它未被 ThinkRail 默认捆绑，也没有工作流 UI，不能把这些源码能力视为当前稳定桌面产品现成的后台任务功能。依据：[包与导出](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/pi-dag/package.json)、[入口](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/pi-dag/index.ts)、[模块规格](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/pi-dag/SPEC.md)。

其 pause 将 DAG mode 设为 paused，调度器只在 mode running 且输入就绪时准入新节点；这首先是停止新调度。恢复失去 owner 的任务时，旧 preparing/queued/running 节点转为 uncertain/held，并将图保持 paused，等待后续对账。它与 Brain 先 pausing、等付费在途隐私/结算/检查点排空再 ack paused 的含义不同。依据：[运行服务 pause/kick/owner 恢复](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/pi-dag/runtime/service.ts)、[恢复状态投影](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/pi-dag/runtime/reads.ts)。

持久层使用 expectedVersion 校验、临时文件替换和目录同步；值得参考意图先持久化、commandId 幂等、uncertain 对账与依赖失效规则。它没有提供外部副作用的通用 exactly-once 保证；不能据文件状态推断供应商调用账已结算。建议借鉴机制，继续保留本项目 Python/Celery/PostgreSQL 的授权、费用、隐私和认证链；没有证据支持为了它迁移整个后台。依据：[持久存储](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/pi-dag/persistence/store.ts)、[DAG 模块规格](https://github.com/JetBrains/thinkrail/blob/d299f20d2ba014f382ddbcc9b49f2aa63fada132/packages/pi-dag/SPEC.md)。该部分为父代理同一固定提交的只读源码复核，未运行外部测试。

## 建议的最小验证

先完成当前 Brain 正式产物验收，再在独立副本做只读消费试验。用同一组冻结问题对比“直接阅读 Brain 产物”和“ThinkRail 按需查 Brain”：记录回答正确性、证据定位、未知项处理、延迟、上下文量与新增模型调用；不预设加速比例。另验证错误/过期版本拒绝、隔离项目不能互读、生成事实不能覆盖人工规格、客户端取消后服务端状态仍准确。查询或开发体验收益不应当作全量生成时间已经缩短。
