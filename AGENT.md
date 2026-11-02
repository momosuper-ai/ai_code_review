# Project Brain 会话总结与新会话交接

整理日期：2026-10-26（北京时间）；同日追加实现状态与历史耗时核查。本文件是本会话的交接入口；`AGENTS.md` 保留仓库规范和历史记录，并指向本文件。**历史日志、计划、离线测试、实际运行与正式验收必须分开。**

## 1. 后续记录规则

用户明确要求：**以后每完成一次重要修改，或发现一个关键结论，都必须在本文件更新后再向用户报告完成。**

- 先更新“当前状态”和“待推进”，再在末尾追加简短变更记录；不要只不断前插互相矛盾的“最新”长段落。
- 每条重要记录至少写明日期、改动/结论、依据、验证结果及限制、下一步；涉及真实执行时记录本机控制目录、构建身份、固定 SHA/配方、额度与旧账是否保留。
- 区分“提出方案”“代码已实现”“离线通过”“已部署”“真实构建已运行”“正式质量验收通过”。未完成的事项不能写成已完成。
- 运行状态必须带实际检查时间与来源。读取失败时写“无法确认”，不能以缓存、容器存在、Worker 存活或观察器退出代替数据库事实。
- 不写入密钥、Token、Webhook Secret、源码正文、模型原始响应、可逆脱敏映射或个人身份。私有证据保留在被忽略的 `secrets/` 中，只在这里记录安全摘要和位置。
- `AGENTS.md` 中保持本文件的入口及持久规则；`memory/MEMORY.md` 只补充必要索引，避免复制整份运行史成为另一套状态真值。

## 2. 用户目标与已确定的产品选择

在原 AI 代码审查服务中增加 Project Brain，逐步替代旧 AI_README：从固定 Git 提交构造事实、关系和知识卡片，经过隐私、结构、证据及版本校验后生成文档，提供查询/ZIP 下载和可控 GitLab 写回。

- **共用原 GitLab Webhook**：目标分支 push / MR merge 自动触发；产品目标为无有效基准则首建、有兼容基准则增量。2.4.1加入schema2跨提交compile检查点复用；privacy/source_privacy仍完整重跑，离线实现不等同于生产验收。首次接入不要求用户调用 API 或预先手填 Project ID。
- 审查和 Brain 共用事件入口，但独立登记、去重、队列和生命周期；审查/评论失败不能阻止知识更新。MR merge 解析目标分支 HEAD，再固定完整 SHA。
- JSON 是规范知识，Markdown 为受控渲染结果。六类卡片为 Project、Architecture、Module、Interface、Data、Flow。
- 五种语言提供通用结构；Python/Java 优先适配 FastAPI、SQLAlchemy、Spring、JPA、MyBatis。调用关系明确区分 `resolved/candidate/unresolved`，不按同名符号猜测确定关系。
- GitLab 写入范围仅为根 `PROJECT_BRAIN.md` 与 `docs/project-brain/` 的安全后代。**目录内保留命名和层次自由，不为迎合某个固定文件强迫模型输出。** 协议清单路径仍由服务保留。
- 不执行试点业务代码，不自动修复业务代码，不跨仓库分析。OpenViking、审查上下文消费和 Codex 工具消费后置。
- 当前没有前端，查看通过 GitLab 文档、REST API 和 Swagger。旧 AI_README 文件与历史保留；当前 Brain 总模式会阻断旧 README 分发及最终写入，paused 不自动恢复旧模式。

## 3. 已完成的工程工作

以下为工作树中已有实现，**不代表下述真实场景均已验收**。

| 领域 | 已实现内容 | 主要入口 |
|---|---|---|
| 固定源码 | 独立 Git 缓存、完整 SHA、只读快照、blob/物理行范围、跳过链接/子模块/LFS 等并报告缺口 | `src/core/repo_manager.py`、`src/core/project_brain/snapshot.py` |
| 事实与知识 | 静态提取、模块划分、受控上下文包、六类卡片、关系可信度、有限静态 Flow | `extractor.py`、`context.py`、`schema.py`、`compiler.py` |
| 汇总 | 大包确定性分片、模块汇总树、项目概览；保留完整细节与未知项 | `summary.py`、`planning.py` 及相关模块 |
| 任务和版本 | PostgreSQL 生命周期、单元租约/检查点、预算、Beat 恢复、原子不可变产物、保留策略 | `lifecycle.py`、`artifacts.py`、`src/tasks/brain_task.py` |
| API | 当前/历史/进度/卡片/Markdown/ZIP/用量/阶段重试/恢复计划和认证恢复 | `src/api/v1/brain.py` |
| 自动维护 | 共用 Webhook、DB 先登记、Broker 故障补发、事件合并、独立去重、防机器人递归、README 接管 | `src/api/v1/webhooks.py`、Brain `webhook.py` |
| 发布边界 | 不可变清单认证、路径/目标树/实际 diff 校验、机器人文件所有权、并发保护、提交/MR 与发布对账 | `publisher.py`、`write_policy.py`、`src/tasks/brain_publication_task.py` |
| 权限隔离 | API/Publisher 产物只读，Worker 私有工作卷；发布身份和读取/评论身份分离 | Compose、`scripts/gitlab/project_brain_pre_receive.py` |
| 本机验证 | 独立 WSL/Docker/PG/Redis 栈，模拟边界测试、一次性数据库回归、受授权真实模型验证、Cloudflare Webhook 测试 | `compose.brain-local.yml`、`compose.brain-public.yml`、`scripts/`、`tests/` |

表中未带目录的 Python 文件位于 `src/core/project_brain/`。工作树存在大量本会话新增和修改文件，尚未形成一份干净的已提交交付；**不要 reset/clean 或删除未跟踪文件。** 本次交接没有提交或推送代码。

## 4. 关键修复与应保留的结论

1. **早期隐私故障包含明确的工程问题，不能概括为 local_model 能力不足。** 大批输出截断、串行整阶段扫描超过600秒、空结构重复扫描、失败从头扫描、机械切片破坏完整敏感值，均曾通过合成对照/故障注入验证。后续已改为完整字段分包、输出容量规划、独立 privacy 单元、HMAC 收据及正常让出任务后续跑。
2. **输入、语义输出、必要 reasoning、汇总和最终产物都需隐私校验。** 私有工作区只保存必要的不透明身份、指纹、MAC/封存和收据，不保存原始上下文、响应或可逆映射；知识产物不做通用反脱敏。
3. **必须支持含硬编码凭证的原提交。** 不能要求团队先清理所有源码。2.0 系列增加私有规范来源和公开安全投影，脱敏后仍保持事实/关系/引用身份一致，并继承历史已知发现，防止后续模型遗忘已知敏感值。
4. **JSON 合法、strict 标记或 finish_reason 不等于事实正确。** 曾实际出现伪造证据引用。已增加应用侧严格 JSON/Pydantic、证据枚举、完整槽位协议与来源绑定；不删除坏结论凑正式版本。
5. **`source_bound/source_scope` 不是语义正确性证明。** 服务绑定主体/关系/证据，模型仅填解释；推断仍是 `AI_INFERRED`、`semantic_support_status=unreviewed`。有效引用率不能宣传为业务回答准确率。
6. **成本按实际调用分角色/阶段记账，不存在固定模型占比或每项目固定费用。** 文件/事实/关系规模、包数、汇总层次、隐私命中、重试和输出长度都会影响成本。无价格资料不换算人民币；无 usage 时保留估算标记和上界。
7. **预算锁后必须刷新 ORM 数据。** 已修复并发锁前旧值覆盖额度；Brain 禁用 SDK 隐藏重试，每次真实发送独立 UUID 和预留。`settled`、真正 `unknown`、完整关闭 HTTP 错误的费用上界分开，未知不能当0费用或成功来源。
8. **2.1 系列实现显式有界 HTTP 重试和 httpx/httpx2 兼容。** 完整读取并关闭的429/500/502/503/504可重试；真正丢失响应/断连/超时仍按 unknown 保护。完整错误重试成功也不能补结算另一条旧 unknown。
9. **2.2 系列实现认证检查点恢复，避免每次重新调用所有模型。** 完整审计配方与六阶段内容指纹分开；固定源码、完整输入/结果/证据、父私有 MAC/history/seal/收据及真实成功调用均认证后才复用。旧账和 unknown 留在原构建，汇总/组装重新检查。祖先上限8层，不截断或跳过坏链。
10. **2.2.1修复中断误分类与私有索引双读竞态。** 有界遍历真实异常链、透传真实任务中断；索引一致锁读，未降低来源或隐私门禁。
11. **2026-10-26记录的诊断缺口已由2.4.0代码实现处理。** `src/tasks/brain_task.py` 在 unknown 保护路径仍以 `ModelCallsIncomplete()` 作为终态，但替换前会保存受限异常类别/标量字段及 `unknown_guard`；不记录异常正文、URL、请求头、模型内容或响应。不能据此确诊模型能力、网关500或具体网络超时；unknown 本身也不证明请求已到网关。
12. **待机确实会中断长任务，但时间重叠不等于唯一直接根因。** WSL 保活不防整机睡眠；线程临时唤醒请求不保证阻止手动待机/合盖/所有 Modern Standby 情形。观察器结束也不等于构建失败。
13. **2026-10-26核查的schema2普通增量缺口由2.4.1代码接回。** 仅按完整`input_hash`复用compile检查点，privacy/source_privacy仍重跑；父构建/父结果在完成时复核。与同SHA认证recovery分开；离线代码与测试不代表已部署或生产验收。
14. **2026-10-26历史时序核查：慢不能全归给模型。** 离线解析r4登记前隔离dump（SHA与第6节备份一致），六目标构建调用Token与build账一致，v203/r3调用数与原报告吻合。v203壁钟3小时7分1秒，已结算调用区间去重union仅45分52秒（24.5%），北京时间12:01:07至14:04:36存在2小时3分28秒无任何账内调用在途的空洞；扣此空洞后的约63分32秒仍是未完成构建的其余壁钟，不是纯运行或全流程基线。r3壁钟3小时24分47秒，union59分12秒（28.9%）；首次compile派发前78分29秒，新模型调用仅69秒；source_privacy首末派发跨度30分20秒且新模型调用0。unknown延长至终态仍分别至少141分8秒/142分22秒无账内调用覆盖。无调用区间可能包含认证、图/文件处理、调度或环境停顿，尚未精确归因。
15. **2026-10-26性能代码结论与估时限制。** 恢复多处反复全父链/收据认证，source_privacy/compile每单元读取认证整facts、全仓blob及图结构；本机仅2个worker槽，有阶段/汇总轮次屏障。尚无profiler占比，不可宣称空洞均由认证导致；来源绑定会覆盖discovery_check，不能笼统写“一次check固定认证父链两遍”。旧冷构建与r3单次隐私P50为0.46/5.75秒、语义10.91/24.43秒，非受控A/B。当前仍无完整成功耗时；稳定同规模冷首建1.5～3小时只是低置信排期估计，当前多父恢复无可信剩余ETA。完整分析见`docs/project-brain-status-performance-20261026.md`，私有安全统计/只读解析脚本在`secrets/brain-performance-audit-20261026/`。
16. **2026-10-26 ThinkRail结合调研（仅研究，未接入）。** 官方稳定发布核为v0.2.0；源码固定main `d299f20d2ba014f382ddbcc9b49f2aa63fada132`。其spec-graph是设计意图图与按需读取，不是现成全仓知识生成或语义检索；默认有写工具，mtime/size索引不等于Brain来源/隐私认证。建议保留人工规格与固定SHA实现知识的权威差异，正式验收后优先只读MCP消费已有卡片API；当前无MCP、全文/自然语言或独立关系查询API，统一API key也非读写角色隔离。另核pi-dag真实源码有持久编排/幂等/uncertain恢复，但未默认捆绑ThinkRail、private/0.0.0；其pause停止新准入不等于本模块排空后的安全paused。没有接入提速实证，性能主线仍为分段测时/减少重复认证/普通增量。完整来源、代码映射与最小验证方案见`docs/project-brain-thinkrail-assessment-20261026.md`；未安装、运行外部程序/测试、改当前配方或额外探测模型。
17. **2026-10-26 v230失败只读诊断。** 实际完成时间17:02:18，compile10508首attempt固定码`model_http_retry_window_exhausted`。`transport.py:64-66`在首次prepare/预留/发送前也会因剩余窗口或隐私次数不足抛此码，另有预留后与完整HTTP错误后分支；任务文案“临时HTTP错误”不能覆盖所有真实触发条件。本轮163调用全settled、无HTTPerror/unknown/live，失败单元4语义+6隐私均transport_attempt1，与发送前准入失败相符；拒绝分支/Guard起点/remaining_seconds未持久化，未真实复现，不确诊HTTP500/网关或睡眠。16:50～17:05指定System电源事件查询成功且0，容器无重启/OOM。正式启动至失败2小时38分18秒，首新模型前1小时32分34秒，调用预留至结算区间union25分55秒（约16.4%），无账内请求覆盖2小时12分23秒；不把区间当纯推理或无覆盖全归认证，仍无完整成功耗时/逐段profile。安全闭合证据在v230 `terminal-{evidence,log-evidence,power-evidence}.json`，阶段报告在`secrets/brain-quality/242db042-a2ae-4685-9d20-81971186a4c8/assessment.{json,txt}`；50题未评分，不自动重跑。

## 5. 当前版本、环境和验证边界

**10-30 09:51当前状态（实际DB及闭合诊断）：** 唯一3fc8f0f6已于10-29 15:47:02 failed；scan1/1、extract1/1、privacy152/165、运行0、复用compile0，无正式manifest/ZIP、冻结50题不评分。执行24分34秒，仅为失败前用时。857模型调用均settled543,865，unknown/live/closedHTTP均0；十旧账12,967,029完整保留，整轮13,510,894/20M，账面余6,489,106。失败unit12055/privacy经历3次单元尝试；DB固定异常与compiler.py原868行唯一字面量匹配，证明返回value为空或不属于实际发送文本。原始响应未保存，不能进一步断言具体值、提示词误解或模型能力。实际容器running/restart0/OOMfalse；旧awake文件active但PID/start已失效，WSL保活身份也失效，不宣称保活有效。用户本次“继续”后正在离线修复诊断与有界纠正，不盲目重发submitted journal或付费失败任务。

- 本次用户授权从原20M剩余继续一次：十旧账12,967,029（实结12,886,859/unknown19,031/closedHTTP上界61,139）全保留，新cap7,032,971，不再追加额度、清账或额外探测模型；同两模型/完整隐私/写回关闭。十旧owner私有历史、六组件、全部单元/调用指纹实核通过，部署前后旧表一致，最新隔离备份保存，GitLab项目/固定提交认证GET通过。
- 先前fresh c226a797于15:05:24启动、15:05:56终止：privacy0/165、scan1/1、extract1/1，首单元11741初始及两重试均完整HTTP500；占17,115费用上界，实结/unknown/live均0。指定电源窗口无事件、容器无重启/OOM，旧awake终态正常释放；失败报告`secrets/brain-quality/c226a797-67b6-48b8-83b5-ae6a19bf0e0d/`，不评分。用户“现在修好了，再试一下”是本次新授权来源，不是自动失败重试。
- 已核2.3.3镜像203文件/2237断网回归通过，真实汇总修复与业务质量仍待完整验证。旧636的127编译/103汇总和所有更早账、submitted journal均保留，不重复POST。20分钟观察经工具保持ACTIVE，本地目标已切唯一3fc8f0f6。

- 候选代码迁移链：005 Brain基础 → 006自动维护 → 007模型用量 → 008显式HTTP记账 → 009_brain_recovery → 010_brain_control → **011_brain_diagnostics**。当前试点库仍以实际检查到的版本为准；本次未部署迁移。新表/字段只走 Alembic。
- 旧r3/r4的认证父镜像：`sha256:8d6a52dbade2ffcd16fa79fac3e3f3806a01a816a7534be63a3756ea5feebb92`。
- 试点：GitLab项目480/main，固定提交 `7d89860efe1113bcb1480bac2f1aaa9b1045bd8d`；原配方 `aec96ef005d49cb06882bc873d75c033da460e6e25e81cd54d050e3edc717cca`。
- Windows仓库为 `D:\MO\code\obc_ai_code_review`；WSL Ubuntu 内为 `/mnt/d/MO/code/obc_ai_code_review`。
- 隔离 Compose 项目 `obc-brain-local`，显式 `.env.brain-local` + `compose.brain-local.yml` + 对应控制目录 overlay；本机API `127.0.0.1:18080`。不要改生产 `.env` 或共享DB/Redis。
- 语义模型 `deepseek-v4-flash`；隐私模型 `local_model`；沿用审查网关，地址/密钥只从本机配置读取。当前试点 Brain 两角色扩展参数为 `reasoning_effort=none`，不改原审查参数。模型别名/目录登记不证明后端或实际可调用性。
- 最近配置边界：API Brain入口false、项目paused/auto_update=false、GitLab同步false、review/publisher关闭。**这是最近已核配置，不是今日服务健康证明。**
- 历史2.2.1离线证明记录 Linux 1,957项、一次性PostgreSQL 39项及镜像189文件核验通过；r4评估门禁107项合成测试通过。这些是对应时间/范围的测试，不是本次10/26重新跑出的全量结果。
- `docs/project-brain.md` 部署段仍有旧006、成本/用量文档仍有旧007和旧预算说明；**实际当前版本以代码、迁移和对应控制记录为准，文档同步仍待做。**
- 2026-10-26早前状态/性能核查时：桌面`docker ps`未能连接默认Docker named pipe，未启动或切换WSL/服务，因此实时DB仍无法确认。代码和备份核查不代表今日运行健康；本次无新模型、retry、POST、预算/写回或自动化变更。

### 先前离线修复与执行证据（历史时点）

**2.3.3改进最终离线完成（10-29 09:54核验）：** 最终候选镜像ac7bee8002fd75b63e63abce310fce5a8ff9e0ac58604e74f56c5d402e335467与203个源码/测试文件哈希一致，Linux断网2237/2237/0skip通过；Windows定向242、原4红已转绿。新增概览字段预算默认title40、texts最多3×115、两处unknown各1×54、path≤160/null，按最坏JSON转义保证满足字段约束的输出不超过原4,000字节；有界纠正量化实际大小、持久收紧到75%/50%、字段超限原因和summary_output_capacity固定码。工具兼容层仍移除不支持的长度关键字，本地独立完整校验，未改原重试次数/32k包/隐私/源证据/总额度。固定636真实叶127+最坏转义合成producer于首候选a397…0668只读完成5轮108节点/max31,990；最终候选只多类型标注，断网AST一致性已核，不假称今日DB重放。全部叶与未知项保留；旧127编译/103汇总均保留，现协议可认证导入compile0，summary不导入，未绕过指纹或注入旧paid单元。未部署/未付费重跑/未冻结50题评分；代码与说明docs/project-brain-summary-field-budget.md，证据secrets/brain-v233-summary-budget-20261028/{validation-summary,unit-proof,final-identity-proof}.json。


**10-29继续核验（09:50）：** 用户再次明确继续后，同一离线build重新送审通过，最终2.3.3候选ac7bee80…335467已构建，尚未部署，正在跑最终镜像全量回归。10-28最后一次重建曾被自动审批服务以workspace out of credits拒绝、未执行，未换路径绕过。09:50实际五核心容器API/Worker/Beat/PG/Redis均exited、无OOM/restart0，故DB查询失败，当前进度无法实时确认；最后确认仍10-28 failed/编译127/127/模块103/104/运行0/复用0，不能把缓存称为今日实况。原临时awake/WSL PID身份失效，未重开；定时观察实际PAUSED，按本次继续请求恢复同20分钟通用任务。离线单测不依赖这些服务，不自动启动付费工作或重启共享环境。


**10-28改进离线证据（15:25）：** 首版2.3.3候选断网Linux2237项全通过/0skip，Windows定向242通过。固定636的127叶结果+最坏JSON转义有界合成producer，5轮108汇总节点完整收敛，maxwire31,990<32,000，全叶与历史未知项保持。真实组件比对scan/extract相同，但privacy/compile/summary/render不同，现协议可导入compile0，summary本就不支持导入；旧127/103原样保留不能伪称已复用。此为只读合成规划验证，模型0、未部署/未评分；正在完成类型标注收尾后的候选重建和核验。


**10-28用户明确开始改进（离线实施中）：** 用户在确认4,000字节为服务概览输出契约后要求“可以，那开始改进吧”。本次落实字段级预算、可量化的有界纠正和容量分类，先做离线回归与旧成果兼容性核查；控制brain-v233-summary-budget-20261028。旧636bd04e的127编译/103汇总与12,949,914整轮账保留，不追加额度、不额外探测、未部署或重跑。尚无修复通过或真实质量结论。


**10-28同配方续跑准备实核通过（09:54）：** 八旧owner私有来源/历史/收据及全单元/调用指纹实际认证，直接父59e11245六组件与当前2.3.2全部相同、202服务文件一致、可候选compile0；只读实核原阶段retry因未恢复HTTP组拒绝。隔离备份已保存，API同244e镜像/原recipe、cap7,802,111/健康200/入口false，专用Worker/Beat停止，全部旧DB表部署前后摘要一致。GitLab固定项目/提交GET通过，真实模型0。新gateway-recovery-13门禁92项Windows及断网Linux合成通过，冻结50题不变；八旧账与一父恢复历史账分别核验、预期导入0、父HTTP上界不伪造成新成功。原WSL保活PID/start身份有效，不重复创建或承诺剩余时限。当前尚无新journal/child/模型调用，下一步一次recovery登记、pin实际UUID/保活核验后启专用W/B。


**10-28网关调整后明确继续授权（准备中）：** 用户明确“继续吧，网关已经调整好了”。09:44实时原59e11245仍failed/privacy0/165/running0/新增0/复用0、费用23,525闭合HTTP上界不变，七服务运行/无新增错误；10-27 18:08只读请求曾因自动审批工作区额度不足未执行，本次用户继续后同一读取已复审通过，不绕过审批。原同recipe阶段retry会被未成功收尾HTTP组门禁拒绝，普通POST会复用failed记录；因此按本次继续请求走唯一同2.3.2认证恢复子执行，直接父59e11245，组件实际待核，可候选compile0。新控制secrets/brain-v232r1-validation-20261028，八旧占8,197,889/实结8,134,834/unknown19,031/closedHTTP上界44,024保留，新cap7,802,111+八旧仍原16M；同SHA/两模型/完整隐私/写回关闭，无额外probe，不清账、不改服务代码或配方。此时未登记、未启新模型；下一步八旧owner与同配方认证、备份/预算配置更新、唯一O_EXCL recovery POST、门禁和保活后正式启动。


**用户最新明确修复并验证授权（10-27 17:23后）：** 用户“那先修复汇总收敛及异常分类，再继续验证”。据此开始离线修复和回归，随后在现有16M总上限/余7,825,636内继续一次固定SHA/同两模型/完整隐私/写回关闭的完整验证；不追加额度、不清账。修复必须保留完整叶结论/来源/历史未知项和真实32,000包门禁，不仅改错误名或无限重试。拟对模型新增概览施加明确、有界输出预算及有界纠正；CompilationLimitError在汇总包装层透传。生产代码变更导致新recipe时，禁止注入当前失败paid单元，先实际认证可复用范围，再选择合法新配方恢复/完整执行；旧127编译/87汇总、全部账/journal/报告保留。此时尚未改生产实现或启动新真实生成。

**2.3.2离线实现进度（10-27）：** 新`summary_budget.py`对模型撰写title/texts/unknowns/document_path的完整JSON UTF-8总字节施加packet_limit/8（上限8192/下限128）约束；默认32,000包对应4,000字节概览。系统提示明确合并去重/保留关键边界与重要未知，不把实现约束塞入产品coverage。compiler在现有有界格式纠正内处理完整但过长概览，必要reasoning仍完整隐私扫描，正文不回传；次数用尽为CompilationLimitError，隐私处理后再次验预算，wrap_checkpoint也独立拒绝过长产物。所有叶结论、来源及历史未知项保留，不截断任何输入/输出字符串。summary三包装层透传容量异常；32,000原包/24子/8深度/480工作窗均不扩大。最初真实调用链3个红回归已证明旧实现错误（3 failed/3.61秒），修复后3绿；已增加49长叶结论多层收敛、完整未知项、UTF8/JSON转义、机器来源与正文预算分离、先来源校验、隐私膨胀后停止等回归。定向范围初始155绿/1旧fixture容量假设失效、之后159绿/1新fixture缺必填unknowns；仅修合成fixture和原覆盖限制样本，不改生产门禁。新recipe将包含summary_budget依赖，compiler/隐私内容组件需真实重核兼容，不能承诺复用旧127。尚未打最终镜像、部署或启动真实新验证。

**2.3.2离线验证闭合（10-27）：** 不可变候选`sha256:244e100959f322fd3a2a67d6ff63ddbe888d5579857ea3771a887f72cc6e9632`，202服务/测试/脚本文件绑定；断网只读Linux容器2099/2099 passed，0失败/错误/跳过，真实模型0/实际库改动0。161定向通过，不与全量相加。已有固定bundle和127叶结果只读验证哈希后，在候选内用合成有界producer完成4轮94汇总节点、最大层级2、最大wire31,919/32,000，全部127叶结果和历史未知项原样保留；旧超长summary按容量而非来源错误拒绝，不把合成overview当真实产物或质量评分。证明`secrets/brain-v232-summary-fix-20261027/{candidate.json,unit-proof.json,fixed-input-convergence-proof.json,targeted.xml}`。下一步实际核7旧owner/私有材料与新组件兼容，必要隔离部署后一次固定SHA完整验证，仍原16M/余7,825,636；不放宽门禁或自动追加额度。

**2.3.2真实兼容预检（10-27）：** 新控制`secrets/brain-v232-validation-20261027/`已由最新用户修复后继续验证请求唯一准备，旧7个owner实占8,174,364/实结8,134,834/unknown19,031/closedHTTP上界20,499保留，新cap7,825,636+旧占仍16M。实际私有认证/无活动调用和租约/202文件通过，但新增系统输出约束改变共享compiler及summary_budget依赖，privacy/compile内容指纹均不兼容。因此不伪造127旧编译或收据导入；按既有授权选择一次新recipe的fresh historical/full验证，保留旧成果与全部journal，继续使用Git缓存及认证隐私历史。尚未POST或付费生成，下一步隔离API-only部署、冻结七旧owner证明和正式新作用域后唯一登记；失败不自动再建或加额。

**2.3.2 API-only部署（10-27 17:53）：** 隔离API同010已实际更新244e…9632/健康200/入口false，Worker/Beat已停；新私有dump保存，全部旧表摘要部署前后一致。实际新recipe`86ce3a7b62d6bd1b8c0189f9bd1603a54fa31831f6873076ba64d213d664a652`，cap7,825,636，写回关闭。候选内七旧owner私有材料/单元/调用指纹已冻结，GitLab认证项目与固定提交GET通过（模型0/写入0）。新summary-fix-12正式scope准备含七旧账、原+8M授权和唯一旧retry证据、冻结50题；尚未登记新build或启动真实模型。后续唯一journal提交后再pin实际UUID/通过合成门禁，核原生awake后开专用W/B。

**2.3.2新轮终态闭合（10-27 18:06）：** 实际18:01:54.594186 failed，正式W/B启动至终态29.829秒，不是全流程成功耗时。SELECT-only确认privacy11046的同一HTTP组attempt1/2/3全部完整关闭500，时间18:01:42.848～18:01:54.587；并发11045另有首attempt500，故共4条closedHTTP、上界23,525，真实实耗无法确认，不把它写成unknown或实结。4条均privacy/local_model，无semantic或summary调用、有效运行租约0，164随终止取消。18:00～18:03 Kernel-Power指定事件查询成功且0，七容器restart0/OOMfalse；原生awake按failed正常释放、进程退出、不重开，WSL38336身份仍匹配。不能据现有闭合元数据确定网关内部/下游根因，更不能归因模型能力或休眠；后续需查上述时段隐私上游日志，付费恢复需明确继续并实核。正式门禁实时waiting/build_state=failed，冻结50题不评分；阶段报告secrets/brain-quality/59e11245-c86e-4538-801e-db44ec9adf5d/assessment.{json,txt}，证据v232 terminal-evidence.json/terminal-power-evidence.json。因已终态不启动4小时观察器，既有20分钟heartbeat仍ACTIVE且仅观察该UUID，当前privacy0/165/running0/新增0/复用0基线按实际报告持久。


**本次续跑新终态（10-27 16:59实时补核）：** 追加额度已按授权生效、同UUID重试已执行；构建于16:58:27.953再次failed，module_summary11024/3attempt/ValueError，该单元模型调用0，尚不能只凭泛化stage_failed确定具体校验位置。编译127/127保持，module_summary87/106（续跑累计+13；相对刚才16:58实际报告无新增）、运行0，另18单元随终止取消；阶段总数106是上层合并计划展开，不是原84全部完成即ready。当前2092调用全部settled4,341,710，无unknown/live/HTTP上界，续跑新增调用71/费用203,195；六旧占3,832,654不变，整轮8,174,364/16M，余7,825,636，因此不是本次预算不足。已通过真实混合新调用审计，原497成功单元和2021旧调用仍原指纹。失败后不自动再retry/加额/另建，不清理快照/账或评分；下一步只读定位上层汇总本地校验并保存新终态未评分报告，原16:27预算报告先归档。

**本次追加额度续跑已实际执行（10-27 16:51:33）：** 用户“加800万额度续跑”的明确新授权已通过唯一O_EXCL retry journal落实；16:49:48仅POST一次原UUID`05e5b67e`失败阶段retry，未新建build。重试后实际核10个失败/取消单元转pending、497成功单元（含127compile/74module_summary）和2021旧调用/4,138,515均原指纹不变，原注册授权及journal原字节保留。API入口已关闭/健康200；API/Worker/Beat实际同2.3.1镜像及cap12,167,346，原轮上限16M、六旧3,832,654/全部unknown和HTTP上界不变。16:51:33原生临时awake11832/thread41300确认后启W/B；16:52:19实际module_summary75/84/running2（较停止时+1），compile127/127保持，跨构建恢复导入0，同构建保留127+74是完成子集而非新增。budget-retry-11门禁实际waiting/compiling，Windows与断网Linux各90合成通过，50题原字节不变，尚未正式验收。20分钟automation实际ACTIVE，控制目录已切`secrets/brain-v231-budget-resume-20261027/`。新原生awake有效；16:54旧WSL helper身份已不匹配，正在按授权只替换自有保活客户端，不重启共享WSL/Docker。之后失败不自动重复retry或加额。

**10-27用户明确追加额度并续跑（准备中）：** 用户直接要求“加800万额度续跑”。本次授权增加8,000,000 Token，原轮总上限8M→16M；六旧账3,832,654不变，当前同UUID新build cap4,167,346→12,167,346（含本构建已结算4,138,515，不清零）。目标仍2.3.1/05e5b67e/原固定SHA/recipe/image/同两模型/完整隐私/写回关闭；优先实际同配方门禁后一次原UUID失败阶段retry，保留127compile及74module_summary。准备新控制目录`secrets/brain-v231-budget-resume-20261027/`，保留原登记journal与原授权；新O_EXCL retry journal与额度扩展证据单独保存，不再POST新build、不反复retry。此段写入时尚未改实际cap或执行retry；后续失败仍停下，不自动继续加额。

**追加额度续跑实核通过（10-27）：** 同2.3.1不可变镜像/200文件/原recipe实核，当前私有key/index/history/收据认证通过；2021调用全settled、4,138,515旧实结不变，127compile和74module_summary及所有497个已完成单元指纹已冻结；无活动调用/租约、无Recovery后代。六旧账与checkpoint/call指纹均等于原部署前证明。下一步停专用W/B、备份、只改环境cap并核全DB行不变，然后唯一原UUID失败阶段retry；未授权清账或新建。

**追加额度API配置部署已通过（10-27）：** 专用Worker/Beat已停，新隔离dump和摘要已保存，API同镜像cap已实际12,167,346/健康200/入口false；只改环境预算不改recipe、模型或隐私，部署前后全部DB表摘要、497成功单元和2021旧调用指纹一致。budget-retry-11正式门禁已固定原4,167,346注册证据、本次精确+8M/整轮16M与原UUID唯一retry，增加最终保留检查点/调用审计；90纯合成测试通过，冻结50题不变。此时尚未retry/恢复模型，下一步唯一重试后关闭入口、核保活再启W/B。

**10-27 16:30终态告警及只读补证：** 当前v231已于16:27:04.789 failed，独立失败为module_summary11011首attempt/budget_exhausted，固定BudgetExhausted消息精确匹配build reservation cap保护；不是unknown/HTTP或工作窗口错误。编译127/127、模块汇总74/84（较16:10已报告+49）、运行0、另9个单元随终止取消、实际复用0。2021新调用全部settled4,138,515，live/unknown/HTTP上界0；原轮占7,971,169/8M，账面余28,831不足后续预留。失败单元此前1次已结算调用8,233保留；没有正式manifest/ZIP、不评50题。正式Worker至失败5,145.607秒（1小时25分46秒），是失败耗时不是完整成功基线；compile账内首预留至末结算跨度约40分8秒、module_summary约24分23秒（未完成）、privacy约17分43秒，区间不是逐阶段纯CPU/I/O或推理时间。调用去重覆盖须裁到正式执行窗口，允许原UUID迟到结算，不把终态后结算当新发送或自动续跑。证明在v231 terminal-evidence.json；未评分报告secrets/brain-quality/05e5b67e-61f1-4e05-9931-641e2d700707/assessment.{json,txt}。临时awake按failed释放、请求inactive，不重开；容器仍healthy/restart0/OOMfalse，日志新增错误类别0不覆盖实际预算终态。保留所有检查点、六旧账和submitted journal；下一步只诊断与估剩余成本，任何追加额度/失败阶段retry须用户明确授权，不自动加额/重试/另建。

**当前权威状态（10-30 09:43:02北京时间，实时只读DB）：** 唯一观察对象`3fc8f0f6-556c-4a93-8c73-7fd2db6023f0`，2.3.3同版本认证续跑/full，status=failed；已failed终止；execution_state残留running不代表仍执行或安全暂停。extract1/1（较上次已报告+1）、privacy152/165（较上次已报告+152）、scan1/1（较上次已报告+1），运行0，复用0。新实结543,865/unknown0/live0/closedHTTP上界0，占543,865；十旧占12,967,029保留，整轮13,510,894/20M。容器运行且无重启/OOM=True；awake有效=False/状态active，WSL保活身份有效=False。manifest存在=False、发布0、未正式评分。控制secrets/brain-v233r1-validation-20261029，API前缀/tmp/brain-v233r1-，94项gateway-recovery-15门禁已pin；20分钟观察保持。failed仅诊断保留账/检查点，不评分、不自动重试或另建；用户本次已明确继续；当前进行闭合诊断与离线修复。

**10-27后续明确部署/验证授权（准备中）：** 用户明确“重新部署并继续验证吧，如果能复用之前的成果就尽量复用，如果无法复用就开启新的一轮验证”。据此准备`secrets/brain-v231-validation-20261027/`，先实际认证六个旧费用所有者/私有材料并比较2.3.1组件；可兼容才复用，不降指纹门禁。不兼容时唯一登记固定原SHA的historical/full新验证，仍同两模型/完整隐私/写回关闭，原轮8M减六旧账3,832,654后新cap4,167,346；保留旧unknown19,031、closedHTTP上界20,499、全部检查点/journal及r4零费用失败。此时尚未部署或POST，新授权未消耗；失败不自动再建。原v230于14:51实时仍failed123/127/97复用/0运行/新增0。首个本地准备脚本因Windows默认GBK读中文控制脚本失败，尚未外部执行；仅补显式UTF-8后从未完成步骤续准备，不重写授权或旧journal。

**2.3.1实际预检/API-only部署（10-27 14:57）：** 六旧构建私有材料和父链、账目与无活动调用/租约实核通过；候选200文件核验通过。实际配置下scan/extract兼容，但privacy/compile及下游组件均不兼容，不能导入旧123编译或隐私收据；选择新的historical/full执行，正常路径仍继承认证隐私历史与复用已有只读Git缓存。新完整recipe`91a40ac22d50c266ed5823985a01136d70e3b68c0d3e5e83f229ce248b45c4eb`。隔离库私有dump已保存，API已部署b66f…93160/健康200、010不需迁移，部署前后全部旧表摘要一致，Worker/Beat确实停、API入口false。还未POST或启动新模型；下一步固定新验收门禁和旧账指纹，然后唯一登记、关闭入口、临时保活核验后开专用Worker。

**2.3.1唯一登记（10-27 14:59:31）：** 按本次明确新授权，O_EXCL journal先落盘后仅一次POST到隔离API，登记`05e5b67e-61f1-4e05-9931-641e2d700707`，fresh historical/full、固定原SHA、新cap4,167,346、六旧账3,832,654保留，总上限8M不变。未采用不兼容的recovery父，不导入旧compile/收据；认证隐私历史仍走正常继承。API新建入口已恢复false/健康200，Worker/Beat未开，14:59:54实际pending/scan0/1/running0、调用0/费用0；本地active-trial/monitor目标已实际切新UUID，旧v230控制副本保留。submitted journal不可重发，失败不自动另建。临时原生awake新helper36448/thread29520已实际成功，不改全局电源计划；正式Worker启动尚待下一步。

**2.3.1正式启动（10-27 15:01:19）：** 隔离Worker/Beat均实际新镜像b66f…93160和新cap4,167,346，API入口false/写回false/原生临时awake成功后正式启动。15:01:37只读DB确认scan/extract各1/1、privacy0/165/running2，compile尚未开始、实际复用0；不是旧123/127继续计数。观察器/20分钟进度沿本聊天继续。WSL保活启动脚本自身发现已有owned helper而拒绝重复创建；没有新建或重启共享环境。最初手工查询未指定JSON日期String导致时间身份比较不一致，已按原脚本显式字符串规则核验并留`wsl-keepalive-check.json`，不依据旧12小时元数据承诺剩余寿命。正式产物/质量验收尚未完成。

**旧轮终态（2026-10-27 14:41:05北京时间，非当前新轮）：** 原构建 `242db042-a2ae-4685-9d20-81971186a4c8` 仍为 `failed`，compile123/127（阶段96.9%，较上次已报告新增0）、running0；其中97实际复用（未增加），1个compile10508首attempt真正失败、固定码`model_http_retry_window_exhausted`，另3个随构建终止取消。scan/extract/finalize均1/1、privacy165/165、source_privacy127/127，recovery frozen五父。`execution_state=running`残留不覆盖failed终态，不是安全暂停。163调用全部settled520,718 Token，unknown/live/HTTP上界0；五父旧占3,311,936完整保留，整轮3,832,654/8M，均未增加。7容器running/restart0/OOMfalse，无新增错误或读取错误。临时awake保持终态释放，未自动重开；原WSL保活仅PID/start身份匹配，不据旧元数据承诺当前保活时限。无正式manifest/ZIP、50题不评分。来源为v230 `report-candidate.json`/`run-state.json`与`log-check-state.json`。2.3.1修复只在工作树/隔离候选，未部署、未重试/另建或探测模型。

**终态补证（10-26 17:19只读SELECT，历史诊断阶段）：** completed_at实际为17:02:18；所有调用终态闭合、有效运行租约0，正式Worker起点14:24至失败约2小时38分18秒。16:50～17:05电源事件查询成功且0。终态/安全日志/电源和阶段报告已保存，费用与检查点未变；诊断细节与限制见第4节第17项。当时尚未修代码；其后已完成下方2.3.1离线修复验证，不能再按本段历史待办判断当前工程状态。20分钟观察按用户要求继续，失败后不自动续跑。

**用户后续明确请求：找根因并解决（2026-10-26授权，10-27离线修复已验证）。** 用户在失败监测后明确“你需要帮我找到失败的根因所在并将其解决”。本次据此建立确定性合成红测试、定位实际调用链、修复代码并回归验证；不再停留在“离线复现尚未启动”的历史阶段。此授权不清理现有123完成记录/97复用、费用、unknown和submitted journal，不自行追加真实模型探测/新构建或扩大额度。保持原600/660任务时限与完整输出隐私边界，已付费未检查输出不能简单让出后重生成。代码修复和断网回归已完成，部署及正式质量验收仍未完成。

**2026-10-27早前红测试依据（以下09:43/09:49为修复过程，最终结果见下方）：** 上轮子任务因工作区额度不足中断；用户明确“继续”后已重读实际文件。`tests/unit/test_brain_work_window.py`两例通过真实`CompilationGuard.clean`/`compile_packet`调用链，在剩余60.5→59.5秒的边界稳定复现`transport.py:66`把未发送请求工作准入误标`ModelTransportFailed`：输入应可让出、已付费输出仍须按输出窗口终态保护。命令为`python -c "import os; os.environ['LOG_FILE']=''; import pytest; raise SystemExit(pytest.main(['tests/unit/test_brain_work_window.py','-q','-p','no:cacheprovider','--basetemp=secrets/brain-window-fix-20261026/repro-temp-02']))"`，实际2 failed/4.07秒；零真实模型，不改运行服务。Windows测试须进程内关闭LOG_FILE，避免原.env指向D:/data的权限错误；不改.env。安全`semantic-events.json`确认失败单元前三次语义为invalid_json、第四后进入6次隐私；每次隐私结算到下次预留间隔约16秒，精确生产性能归因仍需分段profile，不能仅修错误名就宣称解决所有输出时间耗尽。

**2026-10-27 09:43离线进度：** 已将工作准入与HTTP恢复分为不同异常，新增prepare后/预留前时限检查，compiler按“未预留输入可让出/paid输出正确终态/预留后保留未知费用保护”处理，task与API同步固定类别。原2红及准入相位、HTTP恢复、诊断闭合等合计11例已实际通过（2.23秒）；这仅证明分类修复，尚不能称解决了输出扫描耗时。性能计数红测试同时证实3次未变化父认证重复解析3次（1红7绿），正实现完整新鲜字节校验快路，所有发送/结果/封存检查边界保留，同size/mtime篡改仍须拒绝。尚未全量回归、部署或重跑真实构建。

**2026-10-27 09:49离线进度：** 102项工作窗口/完整输出/晚发现/预算/HTTP/费用终态定向已通过（33.70秒），分类红测试已变绿；新增语义初始让出、格式重试不得重置及预留后unknown保留回归。五父×64合成收据基准确认单次已缓存输入仍触发35次父assert（schema2来源认证省略下界）/完整旧回调60次；这是合成计数，不是生产16秒间隙的精确归因。父解析快验正在独立审查，发现必须防住A→B→A变化使缓存字节与实际认证解析不一致，正在补该红测；尚未最终验证，不能发布候选。没有删现有认证围栏、改时限/额度、部署或真实模型调用。

**2.3.1最终离线修复验证（2026-10-27）：** 新不可变候选`sha256:b66f7d154972194874a2c7c291d52d13c633c56d75b49485ddbe23d59ca93160`，200份源码/测试/脚本绑定；Linux断网/只读容器**2091/2091 passed、0失败/错误/跳过**，补齐Windows符号链接场景。修复包括工作准入与HTTP分离、prepare后预留前检查、未发送输入正常让出/paid输出不重生成、预留后unknown保护；每个原认证边界fresh完整内容双读且绑定真正认证材料，防同size/mtime、回滚、late discovery与ABA；父查找只传未命中记录，跨父/批revision变化清空旧hit。原600/660任务时限、480工作窗、配置/模型/隐私/费用上限不变，无新迁移或配置。最初断网全量因未给SDK构造合成key出现64测试环境失败，原证明保留；只加入无账号权限的测试字面量key后2091全绿，没有真实模型。Windows定向102、认证85/3平台skip、lookup7红→7绿等不与全量重复相加。相同5父×64收据合成下界3.337→2.647秒，lookup5→1、assert35→27、read4700→3674；不能宣传实际全流程提速20.7%或历史16秒间隙已精确归因。旧拒绝分支未记录，修复针对确定性工程缺陷，不伪称完整现场重放。证明在`secrets/brain-window-fix-20261026/{validation-summary.json,candidate.json,unit-proof.json,results/unit.xml}`，说明`docs/project-brain-work-window-fix.md`。**未部署、未登记真实新构建、未追加模型费用，旧123完成记录和全部账/journal保留；新组件指纹变化，后续可复用范围须实际认证，不伪造兼容。**

独立只读复核未发现准入、paid输出、格式重试或unknown保护的阻塞问题；该复核未补跑测试或访问真实模型/DB，不增加2091测试数量。14:41实际已报告的进度已通过原`observe.py --record-report`持久到控制目录及全局观察基线，后续新增数以该报告为准。后续20分钟观察继续；真实新配方部署/生成与正式质量验收仍未完成。

- 用户新请求原话包含“写完之后直接帮我开始进行完整验证，每隔20分钟汇报一次进度”，并多次明确“继续”。授权必要隔离部署及一次有界完整认证恢复；不加额、不改原SHA/两模型/完整隐私、不写回、不额外probe、不在失败后自动另建。
- 本机控制目录 `secrets/brain-v230-validation-20261026/`。唯一O_EXCL POST已于11:21:06提交，journal为submitted，**绝不可再次运行submit_once.py/start_once.py或旧准备脚本**。新配方不得原地retry旧r4；直接父r3，旧r4失败0费用记录和全部旧submitted journal保留。
- API/Worker/Beat实际均最终2.3.0 image `sha256:ab88f28a239cf0d133537c9842eeb0859e46f4ec4402341e12ceb61607427d5e`，recipe `bb3f3ea7f9ee7207c7950b34de1b8b4fae7e358159bfddb9816ffc571e2a03cb`，运行库010。原固定SHA/两模型/reasoning_effort=none/隐私不变；新cap4,688,064+五父旧占3,311,936仍原8M，unknown/HTTP上界不清零。API Brain入口false、health200、main maintenance paused/auto_updatefalse/syncfalse，review/publisher停、publication0。
- **14:24:00正式启动Worker/Beat**，scan/extract后已进入privacy。登记11:21至正式启动之间含准备、等待和零模型控制演练，不能把这3小时算作生成或模型耗时。`worker-start-proof.json`记录实际起点。
- 隐藏Windows临时awake43312/native thread6520曾在启动前确认active，已按本轮failed终态释放，当前原进程不存在；不自动重开、不改电源计划、不保证阻止主动睡眠。WSL自有保活37708在18:13仍核pid/start身份有效，元数据已保留，最多12h。前台只读observer exec-session45535约4h窗口，结束不等于构建失败；每20分钟heartbeat继续实际只读监测。
- 既有automation `gitlab-project-brain` 已ACTIVE、每20分钟并绑定本聊天，覆盖旧5分钟和旧聊天绑定；每一次人工或定时检查均汇报具体北京时间、阶段completed/total及阶段百分比、running、比上次实际已报告新增及复用子集；不能只说正常，也不伪造整体百分比。全局active/monitor已切真实新UUID，原文件另存previous副本；`observe.py --snapshot-report`冻结待报告快照，发出报告后`--record-report`持久该精确基准，普通观察不能冒充已报告。

**实现与验证依据：**

- 构建独立`execution_state=running/pausing/paused`，阶段status保留。暂停立即禁新claim/dispatch，让已claim单元完成语义→输出隐私→结算→检查点，再ack paused；只有确认paused后才可安排休眠。继续同UUID/配方/检查点/attempt/账，检查当前调用及依赖；不复用项目maintenance paused代替。未知调用、过期付费租约仍失败保留账，不伪装正常暂停。
- 新增`execution.py`、API pause/resume、`scripts/brain_execution_control.py`、010迁移及任务门禁；过期/已完成租约不能再预留，Broker重投先于Beat时也不能重新领取未知付费工作。pause遇unknown持久pausing停发，task/Beat依保护终止。说明`docs/project-brain-pause-resume.md`。
- 最终不可变镜像Linux2044/2044、一次性PG44/44、定向249/249全部通过，真实模型0；实际原配置五父×六组件兼容及197文件hash/import/镜像无秘密检查通过。首次旧测试假设失败证据保留offline-attempt-01，生产门禁未放松。见offline-validation-summary/candidate-image-proof及results。
- 运行库010/API-only于11:12:54部署通过；迁移后/API启动后9旧表原列全行摘要一致，新四字段默认running/null通过。隔离dump49,181,723字节，SHA256 `9c965de0bed373e9c734a87b629cc31ec6fbb632e6cf0cb39c3e6f9f4b103115`。旧账/unknown和源码未清改。
- 14:20～14:22真实零模型演练：API pause→paused；新Worker启动仍不claim旧排队scan、attempt0/调用0；停该无在途Worker后API resume→同UUID running/原账不变。pause-real/pause-worker-proof/resume-real为真实证据。未物理休眠、未强杀付费模型，不宣称突然断电无损。
- 正式评估gate `pause-recovery-9` 已pin实际UUID/recipe/image、要求010和完整来源/隐私/五父账/ZIP；113纯合成再次通过，冻结50题哈希45f6…72cd9不变。尚未执行正式ready门禁或业务评分。
- GitLab故障已复现为同目标误走docker0；按当前DNS/default eth3仅加单目标IPv4 /32后，WSL/容器TCP及认证项目GET/固定提交GET均通过。未重启共享WSL/Docker、改防火墙或探测模型。
- 首个登记工具请求曾因旧交接授权被自动审批拒绝且未执行。先关闭API窗口并只读确认无journal/新构建/在途，再依据真实新用户请求重审**同一原命令**通过；未换工具或间接绕过。原拒绝、authorization-recheck及两个已过期历史running行（非进程/有效租约）均保留。

**后续操作边界：**

- 用户说暂停时，读取本机当前UUID与实时DB，使用正式`pause` API或Worker内`python scripts/brain_execution_control.py pause BUILD_UUID --project-id 480`。等待pausing排空为paused，确认无运行/在途，再报告；不要使用仅供零模型演练的run_control.py，也不先杀Worker。
- 用户明确继续且构建确为持久paused时保持同UUID；不得以resume恢复failed构建。若临时awake已释放，先在持久paused状态停止/保持停止专用Worker（此时无在途），以API容器内仅该CLI进程`PROJECT_BRAIN_ENABLED=true`执行resume（不改变HTTP服务入口），刷新真实running状态，重新确认临时awake后再启动原镜像W/B。若服务未运行，先恢复隔离依赖再核状态；recipe变化/未知费用/依赖坏则不强行继续。修复版新配方不能注入本failed构建，后续真实认证恢复的登记/预算范围应另行具体化，所有旧submitted journal不可重发。监测绝不能自动解除用户暂停。
- 本次已failed，后续只读诊断并保留完成检查点、全部账和submitted journal；不执行ready验收/50题评分，不自动retry/新建/加额/probe/写回。用户明确授权下一步前不得重新启动生成；普通增量、真实性能优化及发布验收仍未完成。

**10-28本轮终态根因已闭合（14:54～14:59只读诊断）：** 636bd04e于11:44:38 failed，module_summary11737/level1/4子输入/首任务attempt；4次语义格式尝试的固定日志依次为summary_prose_too_large、invalid_json、summary_prose_too_large、summary_prose_too_large，最终capacity_limit。DB确认该时窗无其他单元调用，4次全settled26,387；不是HTTP/unknown/预算或已证实睡眠。本地输入/依赖校验同镜像只读重放两次通过（9.80/9.49秒），与上轮无模型调用的输入合并失败不同。概览4,000 UTF-8字节门禁正确拒绝不合格输出，但2000输出Token+提示和现有有界纠正不能保证遵守字节约束；原响应未留存，准确大小/字段未知。compile127/127、module103/104、running0/复用0，全部2052调用settled4,752,025，原轮占12,949,914/16M、余3,050,086，旧账不变。正式启动至失败1h48m02s；privacy/compile/module调用跨度25m26s/42m50s/34m24s，去重调用覆盖73m34s、其余34m29s，非完整成功/纯推理耗时。11:40～11:46电源指定事件0、容器无重启/OOM。未评分/未改服务代码或付费重跑；证明v232r1 terminal-diagnostic.json及terminal-call-logs.json，未评分报告secrets/brain-quality/636bd04e-ffe0-4f01-940d-9112a4318728/assessment.{json,txt}。后续需离线验证结构化概览字段预算、再认证可复用范围；不放宽原32k包边界、不删叶详情或自动重试。

## 6. 最近执行结果：不要再把旧 r3 当成最新执行

### v230：最新唯一登记，暂停/继续真实演练通过，正式生成已失败

- 构建`242db042-a2ae-4685-9d20-81971186a4c8`，直接父r3，控制`secrets/brain-v230-validation-20261026/`。
- 2026-10-26 11:21:06（北京时间）O_EXCL journal后唯一POST，`request-attempt.json`已submitted，不可重发。
- image `ab88…27d5e`、recipe `bb3f…a03cb`、010、原固定SHA/两模型/完整隐私，cap4,688,064+原五父占3,311,936仍8M。119compile仅候选，不是当前完成数。
- 14:20～14:22实际持久paused、Worker启动禁入和同UUID继续通过，API入口关闭；14:24正式启动W/B。14:28:50实时scan1/1、extract1/1、privacy12/165/running2，recovery实际frozen五父、compile复用0，新调用0/费用0/无错误；正式产物和质量未验收。

### r3：保留了最多的成功编译检查点

- 构建 `27a6778c-9352-4185-b510-3bd046dac0a5`，目录 `secrets/brain-v221r3-validation-20261023/`。
- 2026-10-23 17:51:05（北京时间）失败：compile单元10083、首attempt、`model_calls_incomplete`。
- privacy **165/165**、source_privacy **127/127**、compile **119/127** 完成；119中包含 **59个实际复用**，不能另加59。另1失败、7随终止取消。
- 未确认隐私调用在17:47:52登记；Windows17:47:54进入Modern Standby，17:56:16恢复。容器当时无重启/OOM。支持按中断保守恢复，但原异常丢失，不能证明唯一直接因果。
- 阶段诊断：`secrets/brain-quality/27a6778c-9352-4185-b510-3bd046dac0a5/assessment.{json,txt}`。无正式manifest/ZIP，未评50题。

### r4：上一次历史恢复，扫描即遇网络故障

- 用户确认待机并要求续跑后，已于 **2026-10-23 18:23:02** 唯一POST登记构建 **`982b3de1-30a1-4a64-8431-de130d885970`**，直接父为r3，目录 **`secrets/brain-v221r4-validation-20261023/`**。
- 同SHA/配方/镜像/两模型/完整隐私；新调用上限 **4,688,064 Token**。五层父账和私有材料已在登记前认证，119编译仅为复用候选，包含其中59个继承结果，不保证全部命中。
- 登记前隔离数据库备份49,181,346字节，SHA256 `65f836ac82f60342cb9b7361613d292bda29eabd1b48762bc8edac201e10d41b`，文件为r4目录 `isolated-before-v221r4.dump`。
- 最后有效DB检查为 **2026-10-23 18:26左右**：r4于18:25:47失败，scan单元10090、3次尝试；安全诊断类别为 **`ConnectTimeout`**。模型调用0、新费用0、publication0、无正式manifest；recovery仍pending、已冻结父数0、实际导入0。
- 扫描顺序为 GitLab项目信息GET → 固定SHA快照/必要fetch → 导出 → 冻结恢复父链。不能把登记前五父预检通过写成r4实际完成冻结。
- 当日18:29对同一目的地做TCP对照：**Windows可连接GitLab443，API容器超时**。Windows结果已保存 `windows-gitlab-network.json`；容器超时结果当时只在会话工具输出。网络尚未证明修复，不能归因模型故障。
- r4临时防待机助手50768已随failed释放；不要沿用旧PID当作有效助手。WSL保活也需重新核身份，不能假定仍在。
- r4 `request-attempt.json` 已submitted，**不可再次执行 `start_recovery.sh`、`submit_once.py` 或旧的准备/生成脚本**。当前授权登记已经执行；本次整理不追加构建、重试或模型费用授权。
- r4 `recovery-8`评估门禁已pin该唯一UUID，冻结50题哈希 `45f6c603fc02fb7fd84e7265e7bd4bac3e9c7aa4950b46f0958f226eb0072cd9`，107项纯离线合成通过；没有执行正式ready门禁或业务评分。

### 本次10/26交接核查发现的状态缺口

- `secrets/brain-active-trial.json`、全局监控target和schedule仍指向旧r3；r4只记在 `pending_v221r4_recovery_preparation`，并残留过期的 `submitted_awaiting_worker_freeze`。
- 因r4在扫描即失败，常规 `record_launch.py` 要求active/frozen五父的条件未满足，未完成正常控制记录切换。**不能伪造frozen、改DB状态或降低门禁来让脚本通过。**
- 2026-10-26尝试只读查询时，API容器未运行，未启动服务；今日实时DB状态无法确认。10/23的健康日志不能当作今日健康。该核查未探测网络/模型、未改服务或预算。
- 2026-10-26 10:07（北京时间）定时只读核查：Ubuntu已在运行；API、Brain Worker、Beat、PostgreSQL、Redis实际均为exited/255、restart0、OOM=false，结束时间集中于01:48:24Z（北京时间09:48:24）。代理/隧道运行不代表核心服务健康。当前DB进度、执行中数与新增完成数无法确认；未启动服务、重试或探测模型。旧临时防待机/WSL保活进程身份均失效，退出原因未确定。闭合元数据见r4目录heartbeat-runtime-20261026.json；没有读新日志，日志游标未推进。

## 7. 费用账目必须保留

下表是r4登记前已确认的父链账；不是所有历史轮次的总费用，也不是人民币金额。

| 构建（简称） | 已结算Token | 真正unknown预留 | 已关闭HTTP错误费用上界 | 占用合计 |
|---|---:|---:|---:|---:|
| r3 / 27a6778c | 1,201,687 | 6,719 | 0 | 1,208,406 |
| r2 / 4a63506b | 1,231,000 | 5,286 | 0 | 1,236,286 |
| 3effa39c | 1,267 | 0 | 20,499 | 21,766 |
| 980d439b | 4,060 | 0 | 0 | 4,060 |
| 32ae7934 | 834,392 | 7,026 | 0 | 841,418 |
| 合计 | **3,272,406** | **19,031** | **20,499** | **3,311,936** |

- 原轮硬上限8,000,000；账面剩余4,688,064。r4扫描失败未调用模型，因此当时未消耗这部分；余额不是自动重试/另建/探测许可。
- unknown必须保留原UUID/所属构建/金额：`a7563bc1-e9e3-4d94-b8bc-423097c794df`（r3，6,719）、`d005319b-81e1-4c77-8697-bf8109e05c48`（4a，5,286）、`fd97ce66-1201-446b-81ee-e6cd8d564a9b`（32ae，7,026）。
- 更早轮次占用4,555,276、旧结算7,993,236及旧unknown653,732等历史记录仍保留；它们范围不同，不能不查ledger就混加成“全部实耗”或清零。
- 收据hit不是独立覆盖率、节省比例或质量分；实际复用编译是completed的子集。

## 8. 待推进事项（按顺序）

### P0：先恢复可诊断、可信的运行状态

1. 新会话先读取本文件、AGENTS和r4实际journal/授权/状态，再只读核容器、DB、固定版本、租约与账。服务未运行时，不用旧缓存冒充实时结果；启动或恢复应承接用户明确的执行请求及既有边界。
2. 本次GitLab出站修复已完成并通过WSL/容器TCP及认证固定提交GET；未来若复发，按实际DNS/route重核单目标，不盲写旧eth3或扩大规则，不重启共享环境/关闭防火墙。
3. 本次全局观察目标已切实际v231；旧r4失败/未冻结及v230终态事实和所有submitted journal保留。每次实际报告显式保存当前构建报告基线，不拿缓存或旧构建当当前进度。
4. 2.4.0诊断、固定分段计时及有界瞬时单元重试/构建熔断器已在当前代码实现；unknown-call仍终态，预算、证据、隐私、身份/检查点及封存门禁未放宽。异常诊断只保留安全标量，HTTP耗尽与工作窗口不足使用不同固定文案。
   - 全单测2274通过、定向集合188通过、隔离PostgreSQL测试10通过、`compileall`通过；4条现有deprecation/configuration warnings。无ruff配置，未运行。仅代表离线代码验证，未部署、未真实运行模型或完成生产分段profile。
   - 分段覆盖source catalog、bundle、execute、recovery auth、model call、checkpoint与claim；报告脚本只读查询。离线计时不能解释r3/v230历史耗时空洞，也不构成生产性能结论。2.3.1的HTTP窗口分类修复及unknown费用保护保留。

### P1：经授权完成恢复与正式质量验收

最近一次实际运行状态记录（10-30 09:51）为3fc8f0f6失败，152/165隐私单元与全部旧账保留；本次只做离线代码工作，未重新读取DB，不能据此声称运行状态更新。2.4.1包含2.4.0诊断、计时、瞬时重试和熔断器，以及schema2跨提交compile复用；privacy/source_privacy仍完整重跑。本次修复为旧ready增量构建保留兼容读取，并把新构建生成器版本记入diagnostic；`brain_version`仍只是整数构建序号。定向376项、全量单测2293项、`compileall`通过；PostgreSQL集成未在本次重跑。未部署、未调用真实模型或观察CI；离线通过不代表生产配方兼容或正式质量验收。生产环境验证和冻结50题只在真正ready后进行。

5. 当前唯一跟进2.3.2认证恢复子636bd04e，父59e11245；新控制brain-v232r1-validation-20261028，request-attempt submitted不可重发。用户10-28网关调整后继续的授权已执行，一父实际同配方/私有认证frozen，旧59没有已完成compile所以导入0；八旧账保留、新cap7,802,111，总仍16M。同两模型/完整隐私/写回关闭，14:51实查privacy165/165、source_privacy127/127、compile127/127，module_summary103/104，failed/capacity_limit、运行0。用户明确暂停/继续时控制该子UUID；失败只诊断保留检查点，不自动再建/探测/加额。
6. 实际恢复时继续保留固定SHA、完整隐私、暂停/写回关闭和所有旧账；更新临时防待机与WSL保活的真实身份/状态。不能承诺阻止主动睡眠；失败即停下诊断，不靠无限重试或加额凑完成。
7. 只有ready后，先按对应 `assessment-readiness.md` 验正式manifest、全部仓库/服务文件和ZIP哈希、私有seal/history/恢复链、固定Git树/blob；门禁waiting/blocked时不评分。
8. 门禁通过后完成冻结50题独立评估：用固定源码确认真值，分别统计事实图可答、文档可定位、仅靠知识回答正确率；接口/模型/配置/测试的精确率、召回率、未知项分开。部分/错误/无法回答仍保留50分母，不用词项命中或同一生成模型自评代替核实。输出 `secrets/brain-quality/<build_id>/assessment.json` 和中文 `assessment.txt`。

### P2：完整产品验收与后续能力

9. 验收2.4.1普通增量的删除/重命名、公共依赖变化、force push/目标删除、并发与Broker/Worker恢复；单次认证恢复不代表普通增量生产路径已验收。
   - 离线实现按完整输入摘要认证compile复用，当前privacy/source_privacy仍全量重跑；须在授权环境验证父链、隐私投影、受影响汇总和真实耗时，不能据此承诺日常小改动分钟级完成。
10. 单独授权真实GitLab发布测试：未保护分支提交、受保护分支MR/自动合并条件、审批/CI、并发人工编辑、响应丢失对账、机器人防递归、越界拒绝。当前测试一直关闭写回，不能宣称已经真实发布成功。
11. 管理员安装并验证服务端目录hook。GitLab Token scope没有目录权限；应用白名单与服务端强制边界不同，脚本交付不等于安装完成。
12. 同步部署文档中的迁移、配置、预算和历史验证标记；选择稳定交付版本，检查未提交工作树并整理提交。质量优先，之后再据真实分模型/阶段用量优化成本。
13. 正式质量通过后，再接OpenViking、审查Agent和Codex消费；继续以PostgreSQL及版本产物为生命周期权威。
14. ThinkRail研究已形成文档，尚未实施接入：正式产物和冻结50题完成后，优先离线验证只读MCP三工具/版本匹配/公开安全投影，再比较按需知识读取；适配层还需正式验收记录/版本准入白名单，现有ready API并不包含50题业务质量通过标记。人工SPEC与自动实现事实分别维护。检索、关系切片、规格偏离审查和pi-dag机制仅为后续候选，不改变当前运行、额度或发布边界。

**截至本交接，没有足够证据宣称已生成满足验收的完整试点知识、50题正确率达标，或已经足以替代AI_README。**

## 9. 新会话阅读和操作索引

1. 本文件 → `AGENTS.md` 的稳定规范 → `memory/MEMORY.md`（长历史只用于追溯，日期更老的状态不覆盖本文件）。
2. 当前同版本续跑控制：`secrets/brain-v233r1-validation-20261029/{authorization.json,request-attempt.json,run-state.json,assessment-readiness.md,worker-start-proof.json,launch-summary.json}`；observe.py --snapshot-report冻结报告，实际报告后--record-report保存基线。当前新cap7,032,971+十旧12,967,029=20M；全部历史submitted journal保留不可重发。
3. 旧r3阶段证据与历史r4控制仅用于追溯；全局观察已修正为新UUID，但仍须实际DB核查，不独以缓存当实时。
4. 核心设计文档：`docs/project-brain.md`、`project-brain-checkpoint-recovery.md`、`project-brain-sensitive-source-design.md`、`project-brain-privacy-inheritance.md`、`project-brain-http-retry.md`、`project-brain-interrupt-recovery.md`、`project-brain-hierarchy.md`、`project-brain-local-test.md`、`project-brain-public-test.md`、`project-brain-server-hook.md`。
5. 当前v233r1目录只读脚本：status.py、observe.py、log_check.py、monitor.py、check_power.ps1、record_observation_note.py；API临时前缀`/tmp/brain-v233r1-`，门禁目录`/tmp/brain-v233r1-assessment/`。容器重建后只在核submitted授权后复制脚本，缺文件不得重复登记/retry；旧v233/c226及v232r1/636都不是当前目标。

监测偏好为**每20分钟具体报告进度，即使未变化或已暂停**：北京时间、阶段completed/total、running、比上次实际已报告新增数、下一步；复用数明确为completed子集。分别持久保存日志UTC游标与 `last_reported_progress`，普通观察不代替已报告基准。PowerShell用 `ConvertFrom-Json -AsHashtable -DateKind String` 保留时间原串，避免隐式时区转换导致重复读8小时前日志。

已有自动化ID为gitlab-project-brain，本聊天每20分钟。10-29 15:04读到PAUSED后依用户继续及原20分钟要求，经automation_update恢复ACTIVE；保持generic prompt/名称/周期/线程，不创建重复项或手改配置。细节仅本机active-trial指向3fc8f0f6/v233r1；实际状态必须读DB，不追加内部账目/UUID到外部提示词。

## 10. 重要记录

- **2026-10-29 10:13｜fresh部署准备实核通过**：已按用户fresh及+4M授权准备新控制目录，九旧账占12,949,914原样保留；旧/新镜像私有来源、history和调用/单元指纹均只读认证。89,578,783字节隔离备份，API-only更新2.3.3健康200，部署前后全旧表指纹相同，Worker/Beat仍停。固定203文件/2237单测不变，新84项正式门禁在Windows及network-none Linux通过，冻结50题字节未变；尚未唯一POST及付费启动。

- **2026-10-29 09:54｜2.3.3离线修复完成**：最终镜像与源码哈希一致，2237断网单测/0skip及242定向通过，真实固定叶+最坏转义合成规划5轮108节点/max31,990完成；最终与重放镜像仅类型标注差异已核。字段预算、数值纠正、独立校验和专用输出容量码完成，旧检查点原样保留；编译内容兼容失败，不能自动复用。无部署/真实模型/新收费；20分钟观察经工具恢复ACTIVE。运行库不可读，未拿旧127/103当今日实时进度。

- **2026-10-29 09:50｜离线继续及实时读取限制**：同一被拒build重新送审通过，最终候选已构建；五核心服务退出，DB当前状态无法确认，不以旧缓存报告实时增量。无部署/新模型/共享环境重启。

- **2026-10-28 15:00｜新终态诊断与观察恢复**：第1/3/4次响应概览超4,000字节，第2次invalid_json；输入重放两次通过，未扩大额度/探测/重新生成。正式失败于11:44:38，耗时1h48m02s，编译127/127、模块103/104/运行0；已保存未评分报告与耗时分段。原20分钟automation实查PAUSED，用户继续后通过正式工具恢复ACTIVE，原generic prompt和目标不变。修正本机记录器只有身份匹配且元数据新鲜才可写awake有效，旧active文件不等同进程存活；生产服务未改。

- **2026-10-28 14:51～14:55｜继续检查与审批恢复**：10:38实际检查已完成，但随后保存已报告基线的原WSL动作被自动审批服务以workspace out of credits拒绝，动作未执行、10:38无实际通知；旧报告基线为10:18。用户再次明确继续后，同范围WSL检查及原record-report重新送审通过，14:51新发现636bd04e已failed/module_summary capacity_limit、127/127编译及103/104汇总、运行0、复用0，已通知并持久新基线。未换路径绕过审批，未重启或提交新模型调用。原awake与WSL保活PID/start均不匹配，旧active文件已过时，不作为有效防睡眠证明；failed后不重开。下一步只读闭合终态时序并重放本地容量校验。

- **2026-10-28 14:51:25｜定时只读检查**：compile127/127（较上次已报告+127）、privacy165/165、source_privacy127/127，运行中0，实际复用0。module_summary103/104/running0（较上次已报告+103）。独立失败module_summary/attempt1/capacity_limit；当前为failed终态，execution_state残留running不代表仍执行或安全暂停；需用户明确继续并完成实核后才能付费恢复。新实结4,752,025、live0、unknown0、closedHTTP上界0，占4,752,025；八旧占8,197,889保留，原轮占12,949,914/16M。七容器健康=True、新增日志/读取错误为空=False；awake旧文件active但实际未确认有效，身份匹配=False，元数据14576.4秒；WSL helper身份匹配=False，不承诺剩余寿命。正式manifest存在=False、业务质量未评分；未自动重试/另建/模型探测/加额/写回，旧submitted journal和账目保留。

- **2026-10-28 10:39｜新增日志只读补核**：10:39只读补核本轮新增ValueError对应privacy11361已在第2次尝试completed；此前11356也保持completed，当前failed单元0。新增日志不是构建失败，无观察器retry或模型探测；证据v232r1 retry-metadata-latest.json。

- **2026-10-28 10:38:43｜定时只读检查**：compile32/127（较上次已报告+32）、privacy165/165、source_privacy127/127，运行中2，实际复用0。新实结1,226,122、live78,897、unknown0、closedHTTP上界0，占1,305,019；八旧占8,197,889保留，原轮占9,502,908/16M。七容器健康=True、新增日志/读取错误为空=False；awake active=True，身份匹配=True，元数据18.2秒；WSL helper身份匹配=True，不承诺剩余寿命。正式manifest存在=False、业务质量未评分；未自动重试/另建/模型探测/加额/写回，旧submitted journal和账目保留。

- **2026-10-28 10:20｜新增日志只读补核**：10:20只读补核新增ValueError对应privacy11356在第2次尝试已completed，当前failed单元0；只是观察已有内部重试结果，未由观察器调用retry或模型。闭合元数据在v232r1 retry-metadata-latest.json，不保存错误正文。

- **2026-10-28 10:18:49｜定时只读检查**：compile0/0（较上次已报告+0）、privacy147/165、source_privacy0/0，运行中2，实际复用0。新实结491,743、live7,512、unknown0、closedHTTP上界0，占499,255；八旧占8,197,889保留，原轮占8,697,144/16M。七容器健康=True、新增日志/读取错误为空=False；awake active=True，身份匹配=True，元数据25.0秒；WSL helper身份匹配=True，不承诺剩余寿命。正式manifest存在=False、业务质量未评分；未自动重试/另建/模型探测/加额/写回，旧submitted journal和账目保留。

- **2026-10-28 09:55～09:58｜网关调整后同配方认证续跑**：唯一O_EXCL recovery POST登记636bd04e（父59e11245），09:56:35新nativeawake40024确认后启动专用W/B；API入口关闭/同244e镜像与recipe/202文件/010，新cap7,802,111+八旧8,197,889仍16M。预检八旧私有来源/账/指纹、六组件相同，备份及全旧表前后摘要通过，候选compile0。gateway-recovery-13已pin实际UUID，Win/Linux各92合成通过、冻结50题不变。09:58:46实读scan/extract各1/1、privacy19/165/running2/本次新增19/compile复用0，62正式privacy调用全settled27,931、unknown/live/HTTP上界0，一父frozen、18receipt hits不另加完成数。七服务正常/无新增错误，临时awake与WSL身份有效；observer6079只读。原20分钟自动化PAUSED已按用户继续经工具恢复ACTIVE，原通用提示不变。无正式manifest/50题评分，失败不自动重试/另建/加额；旧全部journal/账保留。

- **2026-10-27 18:06:09｜定时只读检查**：compile0/0（较上次已报告+0）、privacy0/165、source_privacy0/0，运行中0，实际复用0。独立失败privacy/attempt1/model_http_retry_exhausted；当前为failed终态，execution_state残留running不代表仍执行或安全暂停；需用户明确继续并完成实核后才能付费恢复。新实结0、live0、unknown0、closedHTTP上界23,525，占23,525；七旧占8,174,364保留，原轮占8,197,889/16M。七容器健康=True、新增日志/读取错误为空=True；awake active=False，身份匹配=False，元数据207.7秒；WSL helper身份匹配=True，不承诺剩余寿命。正式manifest存在=False、业务质量未评分；未自动重试/另建/模型探测/加额/写回，旧submitted journal和账目保留。

- **2026-10-27 17:53～18:02｜2.3.2修复部署与授权验证**：输出正文总预算+有界纠正、容量异常透传已通过2099断网单测/161定向；固定输入合成4轮94节点/31,919<32,000、全叶/未知保留。七旧owner认证、旧表部署前后摘要/备份、GitLab只读固定提交和82门禁通过；新配方不兼容旧compile/privacy，按授权唯一fresh登记59e11245-c86e-4538-801e-db44ec9adf5d，新cap7,825,636+旧占8,174,364仍16M。原生awake脚本准备时数值替换误改uint常量，实际启动失败后已修正并核active，Worker启动围栏正确阻止提前运行；此为ignored控制文件修正，正式服务202文件未变。18:01:24真实开跑，18:02实际privacy0/165/model_http_retry_exhausted，4个closedHTTP上界23,525，未到compile或汇总，不评分/不自动重试。新临时awake终态释放和HTTP组闭合继续核验，20分钟观察保留。

- **2026-10-27｜续跑新失败根因已只读定位**：同UUID+8M已执行，16:58:27因level2汇总合并不收敛停止，87/106/0运行，较续跑前+13，额度仍余7,825,636。纯重放2.8秒、模型0，实际wire34,747>32,000，CompilationLimitError误被ValueError包装导致3次任务尝试。497旧成功单元/2021旧调用在混合新调用下指纹不变；旧预算阶段报告先归档，新未评分诊断已保存，不自动第二次retry或新建，生产修复尚未实施。一次新增日志读取长时间无返回而终止编排，不据此推断构建/WSL失败，不重复该读取或用旧日志当实时。

- **2026-10-27｜用户追加800万，原UUID续跑**：明确新授权后固定16M整轮/12,167,346 build cap，不清账。当前私有认证、200文件/同镜像/recipe、无恢复后代/活动调用、497成功checkpoint与2021全settled调用实核，cap部署前后全DB摘要一致/新私有dump保存。16:49:48唯一O_EXCL原build retry，10失败单元转pending、127compile/74module_summary和旧账指纹不变，原登记journal不动；16:51:33新nativeawake成功后正式同UUIDW/B续跑。16:52实际module75/84/running2/新增1、compile127完成保留；API入口/写回关闭。budget-retry-11新旧注册证据/精确+8M/同构建保留审计，Win/Linux各90合成通过，实际gate waiting/compiling。自动化仍ACTIVE20分钟；未新增build/额外模型probe/改模型或隐私；后续失败不自动重复加额/retry。

- **2026-10-27 16:30｜预算终态补证**：v231于16:27:04.789因build预留预算保护failed；compile127/127、module_summary74/84（本次+49）、运行0、1失败9取消、复用0，2021调用全settled4,138,515，旧账保留，整轮7,971,169/8M、余28,831非新授权。原生awake按终态释放未重开，容器/日志无新增异常不覆盖DB预算失败。正式执行至失败1小时25分46秒，阶段调用时序和无评分报告已保存；去重覆盖裁执行窗以区分迟到结算，原127+74检查点/journal保留。50题不评分，不自动加额/retry/另建，后续须具体授权。

- **2026-10-27 16:30:54｜定时只读检查**：compile127/127（较上次已报告+0）、privacy165/165、source_privacy127/127，运行中0，实际复用0。module_summary74/84/running0（较上次已报告+49）。新实结4,138,515、live0、unknown0、closedHTTP上界0，占4,138,515；六旧占3,832,654保留，原轮占7,971,169/8M。七容器健康=True、新增日志/读取错误为空=True；awake active=False，身份匹配=False，元数据191.0秒；WSL helper身份匹配=True，不承诺剩余寿命。尚未正式产物/质量评分；未自动重试/另建/模型探测/加额/写回，旧submitted journal和账目保留。

- **2026-10-27 16:10:52｜定时只读检查**：compile127/127（较上次已报告+32）、privacy165/165、source_privacy127/127，运行中2，实际复用0。module_summary25/84/running2（较上次已报告+25）。新实结3,404,044、live74,201、unknown0、closedHTTP上界0，占3,478,245；六旧占3,832,654保留，原轮占7,310,899/8M。七容器健康=True、新增日志/读取错误为空=True；awake active=True，身份匹配=True，元数据10.8秒；WSL helper身份匹配=True，不承诺剩余寿命。尚未正式产物/质量评分；未自动重试/另建/模型探测/加额/写回，旧submitted journal和账目保留。

- **2026-10-27 15:51:17｜定时只读检查**：compile95/127（较上次已报告+67）、privacy165/165、source_privacy127/127，运行中2，实际复用0。新实结2,367,197、live49,171、unknown0、closedHTTP上界0，占2,416,368；六旧占3,832,654保留，原轮占6,249,022/8M。七容器健康=True、新增日志/读取错误为空=True；awake active=True，身份匹配=True，元数据4.4秒；WSL helper身份匹配=True，不承诺剩余寿命。尚未正式产物/质量评分；未自动重试/另建/模型探测/加额/写回，旧submitted journal和账目保留。

- **2026-10-27 15:31｜定时进度**：实时compile28/127/running2，privacy165/165、source_privacy127/127、scan/extract/finalize完成；对15:12已报告分别+28/+39/+127，实际复用0。新占1,069,867（实结1,062,594+live7,273）、unknown0/HTTP上界0，六旧占3,832,654不变。七容器健康/新增错误0、临时awake新鲜active及两helper身份匹配，未修改服务/模型/预算或重发journal；正式产物/评分仍未完成。

- **2026-10-27｜2.3.1已部署并启动新完整验证**：承接用户明确重新部署、能兼容则复用否则新轮请求，实际认证六旧构建后确认privacy/compile不兼容，未伪造旧结果复用；扫描/提取本来重跑，正常路径保留Git缓存和21父认证隐私历史。API-only备份51,294,864字节/SHA31d4…01fd、全部旧表摘要一致/010不变/健康200；新recipe91a4…c4eb、镜像b66f…93160、2091断网单测及200文件验证。14:59:31唯一O_EXCL登记05e5b67e，API入口恢复false，15:01:19新W/B正式执行；新cap4,167,346+六旧占3,832,654仍8M，旧unknown/HTTP上界/旧成果/journal全保留。15:07实际privacy85/165/running2/较上次+85、scan/extract完成/compile未开始/复用0，已持久已报告基线。原生awake36448有效；旧WSL37708用显式UTC字符串身份核验有效，starter拒绝重复创建而非构建失败，不承诺剩余寿命。正式fresh-10 gate/原50题已pin，Windows和断网Linux各80合成通过（只工具初始5→6账户断言、unknown序位与runner路径修正），实际gate waiting/extracting，尚未正式产物验收/评分。原20分钟heartbeat发现PAUSED后通过原工具恢复ACTIVE，generic prompt及target不改；观察器13850只读，不自动重试/另建。

- **2026-10-26｜交接整理**：新增本文件，梳理产品选择、已有工程、诊断结论、r3/r4真实历史、独立费用账、监控错位和待推进事项；在AGENTS/MEMORY加入入口与持续更新规则。本次只整理和只读核对；API未运行导致实时DB无法确认，未启动服务、重试构建、调用模型或启用GitLab写回。
- **2026-10-26｜实现与性能核查**：用户要求评估生产目标完成度、全流程耗时及瓶颈。只读审计代码和历史报告，并离线解析固定SHA256隔离dump；六构建账及v203/r3调用统计交叉核对通过。新增状态/性能报告，确认普通增量尚未接回、恢复重复认证及全仓/全图重验工作；量化v203两小时无调用空洞和r3编译前78分钟/零模型source_privacy30分钟派发跨度。未取得精确CPU/I/O占比、当前服务状态或完整成功ETA，未虚构业务质量分；未启动WSL/服务/构建/模型、未改journal/旧账/监控/配方/预算/写回。下一步先补安全诊断和性能分段，优化认证/普通增量后，经授权完成正式产物与冻结50题验收。
- **2026-10-26｜暂停控制实现进行中**：依用户新授权实现build独立running/pausing/paused、同构建恢复、API/运维CLI、010迁移、任务准入与排空门禁；已有语法检查及代码复核，Linux/PG尚待运行，未部署或启动模型。原镜像真实父认证通过；最小GitLab单目标路由修复后TCP对照成功。更新既有自动化为本聊天每20分钟，保留原8M轮余额和所有账/journal。后续先完成回归和新镜像实际兼容，再在新控制目录唯一登记完整验证，禁止将准备工作写成构建已运行。
- **2026-10-26｜2.3.0离线验收通过**：最终不可变镜像2044单测、44一次性PG、249定向和113正式门禁合成全部通过；实际5父六组件兼容、197文件核验、原冻结50题不变。未知费用保护加强造成的旧测试假设均已改为显式legacy或安全停止场景，生产门禁未放松。运行库仍009、无新journal/模型/正式产物；已准备API-only备份迁移，随后唯一登记实跑。证据均在v230控制目录，旧首次证明原样保留。
- **2026-10-26｜2.3.0 API-only部署通过**：11:12:54隔离库010与新API健康200验证完成，Brain入口关闭、旧Worker/Beat保持停；新dump及迁移/API前后9旧表原列摘要一致，旧账/unknown/journal全保留。未POST或模型，下一步先核认证GitLab GET，再唯一登记和零模型暂停/继续演练，通过后才启付费Worker。

- **2026-10-26 10:07｜核心服务退出确认**：只读容器检查确认5个核心服务于09:48:24左右以255退出，未见OOM/重启；DB不可读，未确认新进度。保存安全观察元数据并保留已确认进度基准/日志游标，不自动恢复服务或构建。


- **2026-10-26｜2.3.0正式验证启动**：唯一POST登记242db042（11:21），14:20～14:22真实同UUID暂停/Worker启动禁入/继续零模型演练通过；14:24在API入口关闭、原生临时awake确认后启最终镜像W/B。14:28:50实际scan/extract完成，privacy12/165、running2、5父frozen、复用compile0，新模型/费用0、旧账保留，无正式产物/评分。全局观察目标和20分钟汇报已切新UUID，gate已pin并113合成通过；启动前等待不计模型耗时。旧拒绝经授权证据核对后同命令复审通过，journal不再重发。
- **2026-10-26 14:53｜首次定时进度基线**：实时privacy98/165/running2，scan/extract完成，5父frozen/compile复用0，新调用0/费用0；容器running/restart0/OOMfalse、新日志错误0、临时awakeactive且新鲜。本次建立新构建实际报告基线，不与旧r3或仅核查未报告值相减；继续只读跟进，未retry/新建/加额/探测/写回或评分。

- **2026-10-26 14:59｜用户要求每次检查附进度**：实时privacy114/165（阶段69.1%）、running2，较14:53已报告98新增16；scan/extract完成，compile/汇总未开始、实际复用0，新模型/费用0。容器运行、新日志错误0。每次人工/定时检查都按阶段完成数/总数/阶段百分比/执行中/报告增量格式通知并持久基准；下一步完成隐私后进入source_privacy。未retry/新建/加额/探测/写回。

- **2026-10-26 15:13｜20分钟进度检查**：实际privacy156/165（阶段94.5%）、running2，较上次已报告114新增42；scan/extract完成、compile实际复用0。新模型/费用0、父账保留，容器running/restart0/OOMfalse、新日志错误0、awake新鲜active；未暂停、retry、新建、探测、写回或评分。下一步完成余9单元后source_privacy。

- **2026-10-26 15:32｜阶段推进**：初始privacy165/165（较上次+9）及finalize完成；进入source_privacy51/127（40.2%）、running2，本阶段较上次+51。compile实际复用0，新模型/费用0、父账保留、服务健康无新增错误、awakeactive。下一步完成source_privacy后compile；未retry/新建/探测/写回或评分。

- **2026-10-26 15:54｜20分钟进度检查**：实时source_privacy126/127（阶段99.2%）、running1，较上次已报告51新增75；初始privacy165/165、scan/extract/finalize完成，compile尚未开始、实际复用0。新模型/费用0、五父旧账完整保留，容器running/restart0/OOMfalse、新日志错误0、临时awake新鲜active；无正式产物、未评分。下一步完成余1个源码隐私单元后进入compile；未暂停、retry、新建、探测、加额或写回。

- **2026-10-26 16:13｜进入编译阶段**：实时compile27/127（阶段21.3%）、running2，较上次新增27，已完成中含实际复用20（子集不另加）；source_privacy127/127，较上次+1，初始privacy165/165。37新调用全部settled、新占111,508 Token，unknown/live/HTTP上界0；五父旧账保留、整轮3,423,444/8M。服务无重启/OOM/新增错误，awake新鲜active及两个保活进程身份有效。下一步继续编译后进入汇总/组装，再核正式产物与冻结50题；当前无manifest、未评分，未retry/新建/加额/额外探测/写回。

- **2026-10-26 16:33｜20分钟进度检查**：实时compile62/127（阶段48.8%）、running2，较上次已报告27新增35，已完成中含实际复用47（较上次+27，子集不另加）；privacy165/165、source_privacy127/127。新实结264,969、在途预留7,047、unknown/HTTP上界0，新占272,016；五父旧账保留、整轮3,583,952/8M。服务running/restart0/OOMfalse、无新增错误，awake新鲜active、进程身份有效。下一步继续余65个编译单元后汇总/组装；尚无正式manifest、未评分，未retry/新建/加额/额外探测/写回。

- **2026-10-26 16:40｜调研期间进度跟进**：实时compile86/127（阶段67.7%）、running2，较上次已报告62新增24，含实际复用68（较上次+21，子集不另加）；privacy165/165、source_privacy127/127。106新调用均settled304,382，unknown/live/HTTP上界0，五父账保留、整轮3,616,318/8M。服务无重启/OOM/新增错误、awake新鲜active，尚无manifest/评分。下一步继续编译后汇总/组装；ThinkRail仅调研，未安装/接入、改当前配方或追加模型探测。

- **2026-10-26｜ThinkRail一手来源评估完成**：按用户问题核官方站、stable v0.2.0及固定main源码，新增ThinkRail结合评估文档。建议以人工设计规格+Brain已验收实现知识+只读MCP按需消费结合，后续可做规格偏离审查；核准既有卡片API与缺失检索/MCP能力、共享API key限制、会话恢复/pi-dag与安全暂停差异。尚未安装或运行ThinkRail/外部测试，未接入生产逻辑、改当前构建或追加模型费用；当前完整验证继续原UUID，后续先ready门禁/冻结50题，再独立验证消费层价值，不能宣称已经提速或准确率提高。

- **2026-10-26 16:53｜20分钟进度检查**：实时compile119/127（阶段93.7%）、running2/pending6，较上次已报告86新增33，含实际复用97（较上次+29，子集不另加）；privacy165/165、source_privacy127/127。新实结400,874、在途预留47,636、unknown/HTTP上界0，新占448,510；五父账保留、整轮3,760,446/8M。服务running/restart0/OOMfalse、无新增错误、awake新鲜active、进程身份有效。下一步完成余8个编译单元后汇总/组装，再核正式产物与冻结50题；当前无manifest/未评分，未retry/新建/加额/额外探测/写回。

- **2026-10-26 17:13｜确认失败终态**：实际failed，compile123/127（阶段96.9%）、running0，较上次+4，含97复用；10508首attempt固定码model_http_retry_window_exhausted，另3单元随终止取消。163新调用全部settled520,718、unknown/live/HTTP上界0，五父账保留、整轮3,832,654/8M。容器无重启/OOM，新增ModelTransportFailed类别1次；awake按failed正常释放、WSL保活仍身份有效。无正式产物、不评50题。只读补终态/调用时序与固定类别诊断，不自动重试、另建、探测或重开awake，不能单凭类别归因HTTP500/睡眠。

- **2026-10-26 17:19～17:22｜终态证据闭合**：SELECT确认17:02:18失败、有效租约0、163调用全结算，失败单元4语义+6隐私均首HTTPattempt；本构建无HTTPerror/unknown/live。固定日志只有ModelTransportFailed类别，无原始拒绝分支；电源指定窗口0事件，未保存原始日志/源码/响应。保存非评分阶段报告及当轮2小时38分18秒/模型前92分34秒/调用union25分55秒的安全耗时统计。与发送前准入检查失败相符，尚未建立离线复现或修复；费用、123检查点、submitted journal完整保留，不自动retry/另建/探测/扩大额度或写回。

- **2026-10-26 17:33、17:53、18:13｜失败后定时观察**：三轮实际failed、compile123/127（阶段96.9%）、running0，各较上次已报告新增0，复用97子集不变；新163调用/520,718及五父账/整轮3,832,654均未增加，unknown/live/HTTP上界0。容器运行、无新增错误/重启/OOM或读取失败；awake保持终态释放、WSL保活身份有效。既有诊断/完成记录/账保留，无正式产物、不评分；下一步需离线复现定位，该复现尚未启动，未重试/另建/探测/改配方/加额/写回。

- **2026-10-26｜用户明确授权根因修复**：已承接“找到失败根因并解决”的新请求，进入离线合成复现/修复/回归；并行核实际调用链、输出RAM与检查点边界、真实安全时序。旧失败构建/原账/journal保留，不把新请求解释为无限付费重建或额外模型probe；当前尚无修复或部署完成结论。

- **2026-10-27｜继续并复现**：上轮额度中断后承接用户继续，实际跑新工作窗口测试2红，复现发送前同步认证跨过时限导致HTTP误分类，输入可让出与paid输出终态分别覆盖；已知当前真构建仍以10/26最后实时状态记载，未用旧健康冒充今日。接下来区分工作准入/HTTP故障并验证本地重复认证开销；尚未完成代码修复、部署或真实重跑。

- **2026-10-27｜2.3.1离线修复验证完成**：工作准入误分类2红→绿、重复认证计数红→绿、查找/revision7红→7绿、ABA红→拒绝正确。新候选b66f…93160绑定200文件，断网Linux2091/2091/0skip通过，原时限/模型/隐私/预算不变；修复文档和闭合证明已保存。初次全量缺合成SDK key的64环境失败和BuildKit读取Windows测试目录权限失败均保留诊断，分别补仅测试字面量key/通过stdin传Dockerfile后通过；未改.env、权限或真实服务。09:58原构建只读查询曾因自动审批工作区额度不足未执行，用户再次“继续吧”后同一原命令复审通过，14:41实际failed123/127/97复用/0运行/新增0，账520,718及整轮3,832,654不变，14:42日志无新增异常；未换路径绕过审批。未部署或追加真实生成/费用，正式产物/50题仍未验收。

- 2026-10-29：用户明确选择fresh rebuild及另加4M，授权和本地独立额度证明已记录；准备目录已建立，尚未启动。

- 2026-10-29 15:01：原网络复核审批失败事实及用户继续后同动作复审成功已持久；TCP双端通过，登记/模型仍0。

- 2026-10-29 15:04：唯一fresh登记c226a797成功，journal不可重发；API入口已关闭、监测目标与84项门禁已固定，尚未启动付费Worker。

- 2026-10-29 15:05：新fresh已启动且在初始隐私阶段失败，首单元固定HTTP重试耗尽；scan1/extract1/privacy0、运行0，待闭合诊断，禁止自动再跑。

- 2026-10-29：用户回复“现在修好了，再试一下”，授权一次原20M剩余额度内同版本认证续跑；c226失败报告已保存，新控制准备未提交。

- 2026-10-29 15:21：用户网关修复后唯一认证子执行3fc8f0f6登记成功，十旧账/备份/全部旧表一致，94门禁固定通过，原生awake已实核；API入口关闭，尚未付费启动。

- **10-29 15:24:29｜网关修复后真实运行确认**：新执行15:22:27启动，extract1/1、privacy20/165、scan1/1/运行2/复用0，67次新调用成功结算29938Token，当前错误0；十旧账保留，临时awake/WSL及20分钟观察已实核，正式门禁waiting/质量未评分。


**2026-10-30 09:51重要记录：** 实时失败证据见v233r1/terminal-diagnostic.json及terminal-log-sites.json；已报告152/165并持久报告基线，后续无变化新增0。模型原文未导出，引用为空/不存在仍无法二选一。合成最小测试test_brain_privacy_response_repair.py在原代码2项均命中相同ValueError；本机首次测试因日志/临时目录权限未运行，改为本工作区隔离临时目录后真实红测。20分钟原heartbeat经工具恢复ACTIVE，不修改提示词边界。修复控制目录secrets/brain-v234-privacy-repair-20261030，尚未代码修复或部署。


**2026-10-30 10:02重要记录（离线实现/实际观察分别记录）：** 2.3.4候选已实现最多两次隐私协议纠正；完整响应先严格校验，固定reason反馈不含模型正文，实际发送前重新投影且原费用/上下文/单字段上限不放宽。嵌套分包不重开已耗尽子重试，预算/工作窗口不足时不以检查点让出重置纠正额度；固定privacy_response_invalid终止Worker，保留每次UUID计费。最小2红转绿，Windows定向163通过。首Linux全量2255通过/1失败/4错误，均为旧诊断预期未适配新类别；已更新诊断报告与原单次探测预算断言，最终候选022eafeb…5772c全量回归进行中，尚未部署/新建/付费。3fc8失败阶段报告已保存brain-quality/3fc8…，质量未评分。10:02:29实际DB仍privacy152/165/running0/复用0，较已报告新增0；账目与容器健康不变，无新增错误。旧原生awake/WSL身份失效不自动重开。下一步待完整回归通过，再按用户本次继续和既有fresh选择准备一次固定范围验证。


**2026-10-30 10:04离线修复通过：** 最终2.3.4候选sha256:022eafeb1518b9f3dbdcf7b272adc9fb651771ef960d04b0243a91c303d5772c，204源码/测试/脚本文件hash锁定，Linux断网2260/2260、0skip；Windows定向163通过。真实模型0，未部署、未新建、未质量评分。证明有界纠错/严格引用/独立计费/终态分类，不能证明原模型究竟返回空值还是其他错误值，不能保证真实纠错成功。文档docs/project-brain-privacy-response-repair.md。承接用户10-30“继续”和此前直接全新构建的偏好，准备唯一一次2.3.4/full固定SHA验证，保留十一旧账13,510,894，剩余cap6,489,106，总上限仍20M、不加额；内容组件变更不注入旧paid单元，不开发跨版本兼容，旧检查点/全部journal/unknown原样保留。新控制brain-v234-validation-20261030尚未登记或启动，下一步私有历史与账实核、隔离备份和API-only部署，门禁固定后再一次登记。
- **2026-10-30｜2.4.0诊断与瞬时重试（代码已实现/离线通过，未部署/未真实运行）**：新增安全失败诊断、固定分段计时与只读报告、有限瞬时单元重试及构建级熔断/冷却恢复；保留unknown-call、隐私、证据、预算和身份门禁。定向测试188通过、全量单测2274通过、隔离PostgreSQL集成10通过、`compileall`通过；4条既有pytest deprecation/configuration warnings；项目未配置ruff。未调用真实模型、未部署或验证生产性能；schema2普通跨提交增量及真实性能验收仍待推进。
- **2026-10-30｜2.4.1 schema2跨提交增量（代码已实现/离线通过，未部署/未真实运行）**：加入按完整输入摘要认证的compile复用、父结果完成时复核、覆盖/用量及熔断终态修复；定向319通过、全量单测2287通过、PostgreSQL 16隔离集成10通过、`compileall`通过。privacy/source_privacy stages 仍在每个构建中完整重跑，任何隐私/证据/预算/未知调用门禁均未放宽；4条既有datetime deprecation警告及pytest-asyncio配置deprecation提示。未运行真实模型或部署。
- **2026-10-30｜2.4.1兼容性与缓存命中修复（代码已实现/离线通过，未部署/未真实运行）**：新构建在`diagnostic`中记录可解析的`generator_version`（`brain_version`是整数构建序号）；旧ready构建无imports和summary时仍可读取；有imports、coverage中有incremental键、生成器版本达到2.4.1或记录的版本不可解析时要求有效summary。incremental imports也会增加finalize `cache_hits`。定向376通过、全量单测2293通过、`compileall`通过；PostgreSQL集成未重跑。4条既有datetime deprecation警告及pytest-asyncio配置deprecation提示。未部署、未调用真实模型或观察CI；privacy/source_privacy仍完整重跑，隐私/证据/预算/unknown-call门禁未放宽。
- **2026-10-30｜2.4.1已部署WSL隔离栈并登记新8M冷构建（真实构建已运行，未ready/未质量验收）**：PR #1 CI通过（test 3.12）。WSL独立worktree构建镜像sha256:4723f8ef…811c，镜像内断网单测2293通过；旧镜像标记`obc-brain-local:2.3.4-backup`，隔离库备份与`.env.brain-local`备份在`secrets/brain-v241-validation-20261030/`。迁移至`011_brain_diagnostics`，`PROJECT_BRAIN_MAX_BUILD_TOKENS=8000000`（用户新授权8M，旧轮账全部保留未清算），写回关闭。真实模型协议探测`test_brain_real_model.py` 1通过。11:5x(北京)唯一O_EXCL登记新构建`4b4cc802-be20-41e4-8623-325be53b38f6`（项目480/main，固定SHA 7d89860e，historical，recipe 6f67eed6…），登记后1分钟实际读DB：scan/extract完成、privacy 24/165、2运行。冻结50题题库已在WSL找到（`secrets/brain-v2*-validation-*/questions_source.json`，sha256 45f6…72cd9，源SHA同7d89860e，7类共50题），ready后用其评分。Windows临时防待机：`keep_awake.ps1`（≤14h，`keep-awake.stop`停止，不改电源计划）。
- **2026-10-30｜4b4cc802 新8M冷构建失败（真实运行、环境中断，非代码失败；未ready/未质量验收）**：14:11～14:13（北京时间）实际读库：privacy 165/165、source_privacy 127/127、compile 19/127（复用0，冷建无父，符合预期），finalize完成；已结算957,378，总占993,472（含1条unknown调用`f03ed953-e9e9-4944-b068-7cbbe67efc66`的预留，04:31:05Z登记），live0。Windows事件：12:30:58（北京时间）506“Idle Timeout”进入Modern Standby，最后一次调用04:31:10Z（12:31:10）；12:50:22异常重启（41/6008）；13:50:38再次空闲待机、14:03:55开盖恢复；WSL 14:09:08启动，容器退出255/OOMfalse。恢复服务栈后Beat回收过期租约，按unknown保护终态`model_calls_incomplete`（unit 12386，未经新last_failure路径故无该诊断），瞬时重试0次。结论：防待机助手只请求ES_SYSTEM_REQUIRED，挡不住Modern Standby空闲超时；改为ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED（不改电源计划，旧脚本备份keep_awake.ps1.v1）。用户“现在重新连上并继续吧”后，下一步：只读核恢复计划，以4b4cc802为父唯一POST同SHA认证恢复，子构建上限=8,000,000−993,472=7,006,528，旧账/unknown全部保留，写回关闭。
