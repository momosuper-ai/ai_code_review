# Project Brain v1

语义输出默认 `PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE=auto`。可在实际网关合成契约验证后使用 `strict_tool`：唯一固定函数仅承载 JSON，不执行工具；动态证据枚举及应用侧结构、证据、路径、隐私校验保持，工具Schema/完整参数纳入预算。模式进入生成配方，不支持时失败，不自动降低约束。

与审查共用 `POST /api/v1/webhooks/gitlab`。启用后，MAIN_BRANCHES 的目标 push / MR merge 自动登记：无有效知识基准则首建，有兼容基准则增量维护。API 用于查询、补建、归档、暂停和恢复，无须作为首建前置步骤。使用现有 LLMClient，不执行项目代码；OpenViking 后置。

## 自动维护

Webhook 认证后先持久化 Brain 事件，再进入审查去重与投递。审查/评论失败、MR/push Redis 去重均不阻止知识更新；DB 登记失败返回 503，Broker 失败由 Beat 补发。自动接入必须配置非空 GITLAB_WEBHOOK_SECRET。

| 事件 | 行为 |
|---|---|
| 目标 push，包括分支创建、空 commits 的有效 HEAD | 自动登记并检查最新输入 |
| MR merge 到目标 | 调度解析目标 HEAD，不用源分支 last_commit |
| MR open/update/reopen、非目标分支、tag | 不建知识 |
| 目标删除 / GitLab 分支 404 | source_status=missing，停止旧构建/发布，保留版本 |
| 已验证机器人提交/MR | 对账发布，不递归 |
| 仅标题前缀、混合业务变化 | 正常处理 |

状态为 unmanaged / active / paused。历史归档是 unmanaged，不接管当前版本，目标事件可使其 active；paused 仅显式恢复。006 保守迁移旧关闭维护记录为 paused，仅历史记录为 unmanaged。

事件推进 event_seq 和 dirty，不立即取消构建。调度确认不同输入才推进 request_seq；重复事件保留同输入租约。清理 dirty 比较所观察的 event_seq，新事件保留到下一轮。默认合并 60 秒、最长 300 秒，每 300 秒核对已维护目标以补偿漏发事件。

同提交/模式/语义配方复用构建。文档提交推进 HEAD 而源码指纹不变时，确认祖先关系与 ready 产物可用后复用版本，知识仍显示真实来源 SHA。force push、历史不可达、基准/配方不兼容时全量扫描。语义配方与发布 policy_version 分开管理。

同输入的旧产物已清理/损坏时，创建下一 generation、新 build_id 和知识版本；旧历史元数据不改写。

Brain 模式停止旧 README 分发及最终写入，覆盖已排队/已生成待提交任务；旧文件和历史保留。暂停 Brain 不恢复 README；关闭总开关并显式启用 README 才恢复旧流程。

## 知识与文件组织

固定完整 SHA，在独立缓存导出无凭据快照，不修改审查目录。链接、子模块、LFS、大文件、二进制不执行或展开；记录覆盖缺口，路径/blob/mode 仍参与指纹。五语言通用结构，重点适配 FastAPI/SQLAlchemy、Spring/JPA/MyBatis。候选/动态关系不冒充确定事实。

六类卡片：Project、Architecture、Module、Interface、Data、Flow。语义结论引用已有证据，链接绑定 SHA/blob/行范围；Flow 仅静态确认路径，最多 5 层/100 节点。结构/证据/隐私/预算失败保留上一版，不发布残缺结果，不反脱敏。

### 有界、可恢复的隐私扫描

静态 `extract` 只规划扫描范围，多个 `privacy` 单元完成扫描，`finalize` 重建并脱敏事实后才写事实检查点。编译输入、模型输出和最终文档也采用同一严格扫描。空数组、空对象、数字等没有可扫描文本时不调用模型。输入保留完整字段及字段名；普通输入包默认6000保守UTF-8预算单位、每包最多100条，同时按全体字段都敏感时的输出容量分包，留25%输出余量。超普通包的完整字段独立扫描，硬上限 PROJECT_BRAIN_PRIVACY_MAX_FIELD_TOKENS 默认32000，同时受模型上下文与限时约束。超硬上限明确失败，禁止硬切字符后继续。

默认扫描输出 4096 Token、单调用超时 60 秒、每任务最多 8 次扫描，工作窗口 480 秒，仍保持 Celery 600 秒软限时。到达正常工作窗口后交还租约续跑，不增加失败次数。`finish_reason=length` 只二分该批次并重扫完整子记录，最多 8 层；单字段仍截断或 JSON/引用不合规则停止。成功、截断和丢失响应调用均受原有构建预算约束，未知用量保留预留；不通过提高总预算或放松校验恢复。

Worker 私有卷保存每构建随机密钥、HMAC 指纹/长度/类型及完成/二分收据；不保存原值、原始包或可逆映射。失败后从固定 SHA 重建输入，成功收据跳过已完成模型调用。收据绑定配方、构建、完整字段文本及语义字段路径；移除 items/fact 等机械包裹的影响，每条记录独立分类。同文本不同语义字段重新扫描，同字段重复出现跨阶段复用。子包按内容标识，避免恢复后索引移动跳过未扫描内容。最终事实和全部卡片再次应用完整指纹，处理后续发现敏感值的情况。原始事实只作每个 Worker 进程内的有界缓存，不进入正式产物或 API 卷。

并发预留预算在获得数据库锁后刷新 ORM 对象，防止锁前读取的旧对象覆盖其他任务已提交额度。实际用量和缺响应预留均计入固定 500,000 Token 构建上限；真实 PostgreSQL 回归覆盖锁等待期间的对象变更。

Brain 关闭 SDK 和 LLMClient 内层重试，网络失败交由持久任务最多两次重试，每次单独预留；格式纠正仍逐次结算。这样不会用一次预留覆盖多个隐藏请求。旧审查调用保留原重试配置。大型声明的关系和对应证据按有界片段拆分，基础声明在片段中重复，全部关系保留；单元按确定包序号及哈希定位。声明证据片段最多 12 行、关系证据最多 2 行，完整 SourceRef 行范围及图仍在正式索引中；模型不据片段宣称完整运行时流程。

语义初始输出默认4096，仅供应商 finish_reason=length 时逐步扩容至 PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS（默认16384），每次调用按本次完整输入与实际输出额度预留；结构或证据错误不主动扩容。必要reasoning完整扫描后才回传，不能截断或绕过隐私。达到字段、模型上下文或输出硬上限时使用 CompilationLimitError 一次终止构建，保留已花用量和成功检查点，避免任务层重复相同限额故障。两个容量参数参与生成配方，调整后需新构建。

`PROJECT_BRAIN_PRIVACY_EXTRA_BODY` 是扫描专用 JSON 对象，空值继承 `LLM_EXTRA_BODY`，非空仅替换扫描参数。`PROJECT_BRAIN_SEMANTIC_EXTRA_BODY` 同样独立管理知识语义参数，默认空值，不影响隐私扫描或审查。通过同一网关的合成对照验证实际效果后配置，不按模型别名猜测协议。当前试点两者均采用 `{"reasoning_effort":"none"}`，全局审查参数保持原值。SDK 保留 finish_reason 及思考字符/Token 计数，不将原始思考写入诊断报告。新配方生成新构建，保留旧失败历史。

输出Schema的 evidence_ids 枚举由当前知识包全部E别名构成，提示词与 strict Schema 请求使用同一枚举。json_object 网关只能保证JSON语法，服务仍严格验证引用；不存在的E、F/R混用和跨包引用均拒绝，不能删除错误结论后凑出正式产物。Schema诊断只记录固定类型/位置及数字，不回显供应商内容、未知字段名或引用值。

写回授权仅限根目录 `PROJECT_BRAIN.md` 和 `docs/project-brain/` 的安全相对后代路径。目录内不按扩展名、摘要文件名或固定子目录授权。模型可用 document_path 建议模块文档名称/层次，它是包含 docs/project-brain/ 前缀的完整仓库相对路径，null 使用默认布局，不改变模块归属。Schema、服务路径校验和格式重试反馈明确同一边界，输出隐私扫描失败直接终止单元。稳定卡片 ID 与路径独立，manifest.card_paths 和 index 保存映射，导航/API 使用同一映射；默认输出 Markdown/JSON。

协议入口 docs/project-brain/manifest.json 不可被卡片占用。默认布局包括 project.json、architecture、modules、interfaces、data、flows。完整事实/关系/证据/索引服务端保存，随 ZIP 下载；不保存源码全文、上下文包或脱敏映射。

## 写入与发布管控

发布只接收 build_id，加载不可变产物，核对 DB manifest 哈希及项目/分支/构建身份。API/模型不能提供 actions 或自报删除所有权。整批检查路径、字段和目标树，拒绝遍历、绝对路径、反斜线、异常分隔符、Windows 特殊路径、链接、子模块、执行模式、权限修改和重命名动作。

内部布局变化通过旧清单失效文件删除及新产物创建实现，不使用 GitLab move 动作；Git 将两者识别为 rename 时，也必须旧端属于已确认删除集、新端属于已验证产物。

只更新/删除上一份经 DB 哈希认证清单拥有且内容仍匹配的文件。人工内容即使相同也不自动获得机器人所有权；其他人工文件保留，不清空目录。update/delete 使用 last_commit_id，不创建空提交。实际 commit diff/全部内容、整个 MR/源 SHA 再校验；冲突提供 error_code，不改变 ready 构建或重跑模型。

所有触发探测 protected。未保护目标直接提交，受保护目标建立 ai-brain/{build_id} 和 MR。临时分支也受保护才尝试自动合并，否则保留 mr_open / unprotected_source_branch；配置 ai-brain/* 仅允许发布机器人推送并禁止 force push。审批/CI/权限由 GitLab 执行。

响应丢失先对账清单、文件、父提交和确定 MR/分支。只关闭 DB 记录、源分支及构建标记匹配的过期服务 MR，不操作人工 MR。发布前后核对源码指纹，并发业务推进则补建。last_commit_id 不提供分支 HEAD 原子交换，文档始终显示真实来源。Brain 不评论或 @mention。

## 权限隔离

brain-worker 消费 brain 队列，产物卷读写，使用读取身份；brain-publisher 消费 brain-publication，产物只读，独立 Docker secret 加载发布身份。API 产物只读，Beat 无产物卷。Publisher 不加载共享 env_file、源码/仓库卷或 LLM 凭据，仅注入必要 DB/Redis/GitLab/发布配置。

临时快照和检查点通过 PROJECT_BRAIN_WORK_DIR 存放在独立 brain_work_data 卷，仅 worker 挂载；API/publisher 的 brain_data 只包含正式产物。单进程未配置 WORK_DIR 时回退到 BASE_DIR/_work，容器部署使用分卷配置。

Brain 模式 GITLAB_TOKEN 用于评论，须是无代码写权限的 Reporter；GITLAB_READ_TOKEN 可独立配置，也须无代码写权限。发布 Token 只写入 secrets/brain_publish_token 或外部受控文件，不填共享 .env，不纳入版本控制/镜像。

Token scope/项目角色没有目录权限。应用检查约束本服务入口；Token 在其他客户端中仍受强制目录限制，需要 Self-Managed 管理员安装 [服务端 hook](project-brain-server-hook.md)。脚本使用 GL_ID，不信任 Git 作者名，检查每个新引入提交和最终树，拒绝越界、非普通模式、标签、目标删除、改写历史。临时分支按当前目标为基线，不把全仓库算新增。脚本交付不表示已安装；GitLab.com 不能声称 Token 获得服务器目录权限。

## API 与保留

前缀 /api/v1/projects/{project_id}/brain，API Key/IP 白名单，读 60/min，构建/重试 5/min。GET 根（branch 参数）、/builds、/builds/{id}、/builds/{id}/cards、/cards/{card_id}?format=json|markdown、/export 提供当前状态/历史/卡片/ZIP。PATCH 根的 auto_update 暂停/恢复，gitlab_sync 控制同步。POST /builds 接收 branch/mode=auto|full，显式完整 commit_sha 仅历史归档；POST /builds/{id}/retry 重试构建单元，/publication/retry 单独重试发布。

默认保留每分支最近 10 个 ready 版本；当前版本、运行构建基准和待发布/未合并 MR 引用不清理，历史元数据保留。

## 部署（Linux）

先备份数据库、.env、知识卷，记录应用版本。Compose v2 将 docker-compose 换成 docker compose。

1. `docker-compose down` 停旧服务，不加 -v。
2. 对照 .env.example 同步配置，首次 PROJECT_BRAIN_ENABLED=false；设置真实 MAIN_BRANCHES、GITLAB_WEBHOOK_SECRET、读取/评论身份、PROJECT_BRAIN_PUBLISH_TOKEN_PATH。更改 BASE_DIR 同步三处卷挂载。
3. 设置发布机器人、ai-brain/* 保护规则；安全准备独立 Token 文件：

   ```bash
   mkdir -p secrets
   chmod 700 secrets
   umask 077
   touch secrets/brain_publish_token
   chmod 600 secrets/brain_publish_token
   # 用受控编辑器/秘密管理系统填入 Token，启用同步前不能为空。
   ```

   强服务器边界同时安装并验收 hook。
4. `docker-compose --profile brain up --build -d` 重建启动。
5. `docker-compose exec api alembic upgrade head`，再 `docker-compose exec api alembic current`，应为 006_brain_auto；新表只通过迁移创建。
6. 验证：

   ```bash
   docker-compose --profile brain ps
   curl http://localhost:8080/health
   docker-compose logs --tail=100 api brain-worker brain-publisher celery-beat
   ```

   在具备 grep 的 Linux 开发/CI 环境执行 `pip install '.[dev]'`、`pytest tests/ -q`。生产镜像不默认安装 pytest 或复制 tests。
7. PROJECT_BRAIN_ENABLED=true，试点在现有 Webhook URL 开启 push/MR 并配置正确 Secret，不加第二个 Webhook。执行 `docker-compose --profile brain up --build -d --force-recreate api celery brain-worker brain-publisher celery-beat`。
8. 下次目标变动自动首建；检查 SHA、证据、覆盖、Token、提交/MR和独立审查。无新变更时可用补建 API 初始化试点，并非自动流程前置。

本地 SQLite/模拟 GitLab、LLM 测试不代表生产 PostgreSQL、权限或 hook 已验收。日志不保存上下文或脱敏映射。

## 回滚

1. 备份 DB、知识卷与 hook 配置，`docker-compose --profile brain down`。
2. PROJECT_BRAIN_ENABLED=false，按需显式恢复 README_GENERATION_ENABLED，恢复旧应用及配置。README 写回需要恢复相应身份权限，关闭 Brain 不会提升 Reporter 权限。
3. `docker-compose up --build -d` 启动原服务，不启用 brain profile。
4. 默认保留表/卷。仅撤销本次字段，备份后用新 Alembic 降至 005_project_brain；确需删除 Brain 表才降至 004_readme_unique。
5. `docker-compose ps`、`curl http://localhost:8080/health`、`docker-compose logs --tail=100 api celery` 验证恢复。
6. 已发布知识普通 revert 回退；管理员用保存的 hook 包恢复规则，不删除团队其他 hook，不改写业务历史。

已有同输入多代重建时，006 会拒绝自动降至无法表示这些历史的 005 唯一约束。此时保留新增表回滚应用；若确需降库，先备份并由管理员制定历史归档迁移，不能自动删除历史来绕过检查。

## 验收与后续

自动验证固定版本、增量、证据、隐私、租约、首建、重复事件、目录边界及真实临时 Git 历史。上线需验证目标 GitLab hook/REST、保护规则、审批/CI和响应丢失。团队项目 30～50 道问题用于人工评估 API 精确率 ≥95%、明确 DB 关系 ≥90%、辅助回答 ≥85%，同时记录召回/未知；不能以单元测试宣称模型质量达标。

后续 OpenViking Adapter 消费已校验不可变版本，限定项目/SHA/build 检索，PostgreSQL/本地产物保持生命周期权威，各模型出口独立隐私过滤，锁定经契约测试的版本及许可证。

## 编译隐私窗口回归

输入完整扫描若实际消耗过模型调用，先在语义请求之前正常续跑；成功收据保留在 Worker 私有 HMAC 索引中。下一投递从固定 SHA 重建输入，再重放规则和全部指纹，完整扫描收据有效才开始语义请求。这样输出拥有完整的8次/480秒窗口，不增加窗口、不跳过扫描、不保存原模型响应。规则模式或已完成输入收据不增加投递。

输出或格式重试必要 reasoning 的扫描窗口耗尽时明确终止，不允许以正常续跑隐式重新生成已收到的语义响应。来源绑定的结构诊断仅记录固定类别及缺失/多余/重复数量，模型生成的键、槽位和值不写日志。此修复改变配方；验证后创建新构建，不用新代码继续旧检查点。

不联网回归：`pytest tests/unit/test_brain_compiler_privacy_window.py tests/unit/test_brain_source_bound.py tests/unit/test_brain_source_bound_capacity.py -q`。真实模型调用仍按当前对话的次数和总预算授权执行；模拟通过不等于完整项目或业务质量通过。新变量未增加，.env.example 无需变更；重建应用、迁移确认、健康与回滚按本机测试说明的部署更新部分执行。

已有语义响应后的并行预算暂占会在当前进程内存中有界异步等待，保留输出、必要 reasoning 和格式次数，为下一本地/语义调用分别留60/120秒。纯输入及首语义请求仍持久等待；真正预算耗尽仍终止。已执行过语义请求后，格式重试的时间/次数门禁不足不能通过正常续跑复位格式次数。共享预算异常移至 exceptions.py，旧 lifecycle 导入兼容且实现纳入配方。
