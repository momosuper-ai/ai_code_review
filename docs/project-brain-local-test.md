# WSL 本机验证

本机使用 Ubuntu 的 Docker，不需要启动 Docker Desktop。测试栈使用独立 Compose 项目 `obc-brain-local`、数据库、Redis 和知识卷；现有 `.env`、生产配置及其他容器保持原样。默认只开放 `127.0.0.1:18080`，PostgreSQL/Redis 不发布宿主端口。

普通使用只需要在目标 GitLab 项目配置现有 Webhook。项目 ID、项目路径、目标分支由事件自动提供，不需先调用首建 API，也不需另建一个 Brain Webhook。专用可丢弃项目适合验证写回、MR、删除与人工冲突；第一轮关闭写回时可验证现有项目。

## 一、初始化与回归

以下在 WSL Ubuntu 中执行。配置和 Token 不要发到对话、提交到 Git 或打印到终端日志。

```bash
cd /mnt/d/MO/code/obc_ai_code_review
docker version
docker compose version

# 只构建本机测试镜像，生产默认镜像阶段没有 pytest/tests。
docker build --target local-test -t obc-brain-local:dev .

# 仅首次执行。已有 .env.brain-local 时保留配置，不重新生成密码。
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD:/workspace" -w /workspace obc-brain-local:dev \
  python scripts/brain_local.py init --reuse-config

# 每个新终端重新定义。明确指定 env-file，避免 Compose 读取生产 .env。
dc() { docker compose --env-file .env.brain-local -f compose.brain-local.yml "$@"; }

# 首次没有旧测试容器；已有测试环境先停测试栈，不加 -v。
dc --profile review --profile publication down
dc config --quiet
dc build api
dc up -d postgres redis
dc run --rm check python scripts/brain_local.py bootstrap
dc run --rm -T check python - <<'PY'
import os
import pytest
import src.config as configuration
for key in configuration.Settings.model_fields:
    if key not in {"DATABASE_URL", "REDIS_URL"}:
        os.environ.pop(key, None)
os.environ["VLLM_API_KEY"] = "synthetic"
configuration.settings = configuration.Settings(_env_file=None)
raise SystemExit(pytest.main(["tests/", "-q", "-p", "no:cacheprovider"]))
PY
```

也可在 Windows 的项目目录用 `python scripts/brain_local.py init --reuse-config` 初始化，再进入 WSL 执行其余步骤。

初始化只复制 `.env` 中明确许可的 GitLab 读取/评论、模型和隐私配置；生成新的 DB/Redis 密码、API Key 和 Webhook Secret，不复制发布 Token。`.env.brain-local` 已加入忽略列表，Docker 构建不复制任何 `.env`。确认 MAIN_BRANCHES 与试点实际目标一致。本机新库以旧系统三张表建立 004 等价基线，再实际执行 005/006/007；bootstrap 只接受 `brain_local@postgres/obc_brain_local`，禁止在业务库自动 stamp。

全量回归仅为一次性测试进程重建默认配置，保留隔离DB/Redis连接；Brain测试自行启用对应模式。仅删除环境变量不足以清除已实例化的Settings，因此必须重新赋值。不要设置真实模型或在线栈探测开关；已启动服务和.env.brain-local仍保持原配置。

若模型地址使用 localhost，它在容器内指向容器自身。WSL 中监听宿主的服务可改用 `host.docker.internal`，该名称配置为 Docker host-gateway；Windows 服务的地址还需按 WSL NAT/镜像网络核对。已有外部模型地址通常无需改动。不要把模型 API Key 放进 Webhook URL。

## 二、启动知识生成环境

```bash
dc up -d api brain-worker celery-beat
dc ps
curl -fsS http://127.0.0.1:18080/health
dc run --rm -e BRAIN_LOCAL_STACK=1 check \
  pytest tests/integration/test_brain_local_stack.py -q
dc logs --tail=80 api brain-worker celery-beat
```

确认配置的模型可供测试后，可执行一次真实模型协议探测；仅发送合成测试源码，使用现有隐私过滤，预算上限 50000 Token，可能产生供应商费用：

```bash
dc run --rm -e BRAIN_LOCAL_REAL_MODEL=1 check \
  pytest tests/integration/test_brain_real_model.py -q -s
```

它验证真实模型、隐私扫描、结构化输出和证据约束，不读取试点仓库，也不证明模型对真实业务的解释质量。完整项目仍需后续真实 Webhook 首建/增量验收。

额外大型合成协议测试须单独开启，最多两次语义请求、100000 Token硬预算，覆盖60声明、117调用与178证据，并用E前缀函数名挑战证据命名空间混淆。该测试在进程内设置Brain专用非思考参数，不修改审查或生产配置，也不代表真实业务质量已通过：

```bash
dc run --rm -e BRAIN_LOCAL_REAL_MODEL=1 -e BRAIN_LOCAL_LARGE_MODEL_PROBE=1 check \
  pytest tests/integration/test_brain_dense_model_contract.py::test_large_source_names_cannot_be_used_as_evidence_aliases -q
```

`PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE` 默认为 `auto`，`strict_tool` 强制唯一数据输出函数，但仍需本地检查证据引用；网关接受 `strict` 请求不代表它实际约束了枚举。新增可选 `source_bound`：服务先为每个声明的当前关系分片或汇总结论组分配固定槽位和来源范围，模型只填写解释及未知项。完整槽位、非空文本、重复键、未知字段、路径及输入/输出隐私校验仍适用。输入中的候选/未知状态保留，提示词禁止将其描述为确定调用；解释是否遵守此限制仍须独立核实。函数不执行任何操作。

`source_bound` 同时受输入容量与 `PROJECT_BRAIN_MAX_EXPLANATION_SLOTS=24` 限制，配置范围1～64，其他输出模式不使用该槽位上限。声明和汇总超过上限时拆分完整主体，不丢弃事实、关系、候选状态或证据；直接输入超限包时须在语义模型调用前拒绝。先进行不联网的槽位与分包回归：

```bash
dc run --rm check pytest tests/unit/test_brain_source_bound.py -q
```

真实模型合成测试仍须显式开启 `BRAIN_LOCAL_REAL_MODEL=1` 和大型测试开关，逐次记录请求与预算，按合规分包选择样例；不得把61槽单包的失败结果改写为成功。现有两次来源绑定合成请求已完成、合计82906 Token，本轮不追加调用。真实问题包另按用户授权次数与预算执行，合成测试不替代此验收。

验证通过后才能考虑在 `.env.brain-local` 选择 `source_bound`，按第六节重建应用并创建新配方构建，不能用新协议续跑旧检查点。槽位上限纳入配方；新增上限后须重新测算完整项目的包数与预算，不能沿用旧协议119包估算。

`source_scope` 是服务绑定的解释输入范围，不证明文本受该范围充分支持。结论仍为 `AI_INFERRED`，覆盖中的 `semantic_support_status` 为 `unreviewed`，Markdown 显示“解释输入范围（语义支持尚未独立核验）”。Flow 只依据已解析静态关系展开，不把该类解释移入路径卡。旧卡片的新增可空字段默认为 `null`；`source_reference_coverage_ratio` 以及兼容保留的 `evidence_coverage_ratio` 表示引用和输入范围有效性，不是业务准确率。

Swagger 为 `http://127.0.0.1:18080/docs`。API Key 使用 `.env.brain-local` 的 API_KEYS，在 Swagger 的 X-API-Key 中填写；也可不把 Key 放进命令参数，使用辅助脚本：

```bash
# project_id 用事件自动登记后的实际 ID，branch 用试点目标分支。
dc exec api python scripts/brain_local.py api \
  '/api/v1/projects/<project_id>/brain?branch=main'
```

第一轮 PROJECT_BRAIN_GITLAB_SYNC=false，brain-publisher 和 review-worker 未启动。Webhook 仍登记独立审查任务到本机队列，但没有审查 Worker 消费，不发送 GitLab 评论。知识 Worker 可以调用真实模型；自动测试用确定性模型响应验证链路，不能代表真实模型的知识质量。

### 隐私名称与凭证值的有界对照

`scripts/brain_privacy_diagnostic.py` 的 `identity_collisions` 套件只使用独立合成数据：负例仅把一个字符串用作公开代码/配置名称；正例明确将同一个字符串也作为虚构凭证的实际值。每例独立临时 HMAC 账本且最多发送 1 次请求，整套最多 2 次、10000 Token；生产扫描提示词、完整 guard 和路径隐私门禁不变。默认不联网，真实执行还需 `--real`、`BRAIN_LOCAL_REAL_MODEL=1` 和该轮明确的费用授权；不把旧的单次完整构建授权自动扩展为诊断授权。

报告分别给出协议完成、检测、误报、路径改写与未知用量等计数，不输出响应、值、路径、脱敏映射或任意供应商元数据。SDK 发送后即使本地解析失败，其未结算预留也继续保留。只接受合法空 JSON 不能证明负例、正例均通过。两个样本通过仅是有限能力证据，不能证明真实分类误报或据此删除真实指纹、豁免路径、重建全项目。离线 mock 回归位于 `tests/unit/test_brain_identity_collisions_diagnostic.py` 和 `tests/unit/test_brain_privacy_diagnostic.py`；它们不调用真实模型。

旧 `tp` 和 `detection_ok` 分别使用包含匹配与包内清除判据，不能当作精确提取指标；另用 `exact_positive_findings`、`overbroad_positive_findings`、`exact_value_detection_ok` 区分整段圈定。路径计数只比较给定合成路径的同一过滤条件，没有运行完整 FactBundle 或产物验收。真实测试费用单独记账后从当轮剩余额度扣除，不能改写旧构建费用或释放未知预留。

## 三、GitLab Webhook 与网络

GitLab 项目 Settings → Webhooks，配置：

| 项目 | 设置 |
|---|---|
| URL | `http(s)://<GitLab能访问的本机入口>:18080/api/v1/webhooks/gitlab`，使用 HTTPS 代理时按代理实际端口 |
| Secret token | 从本机 `.env.brain-local` 的 GITLAB_WEBHOOK_SECRET 取值 |
| 触发 | Push events、Merge request events |
| Push 分支过滤 | 试点目标分支；应用另用 MAIN_BRANCHES 校验 |
| SSL verification | 有效 HTTPS 证书保持开启 |

GitLab 在其他机器上时，不能把 URL 写成 localhost。先由 GitLab 侧确认能访问 `/health`。默认仅监听回环；需要 LAN 访问时，先准备仅允许 GitLab 来源的网络入口，再将 BRAIN_LOCAL_BIND_ADDRESS 改为所需监听地址并 `dc up -d --force-recreate api`。WSL 镜像网络与 NAT 的入站方式不同；NAT 通常需 Windows 端口转发，镜像模式需相应防火墙规则。参考 [Microsoft WSL 网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)。不要为验证直接关闭整机防火墙。

Self-Managed GitLab 可能阻止 Webhook 访问私有地址，应由管理员仅放行测试入口。GitLab Webhook 页面可查看 Recent events 的请求、响应及测试投递结果，参考 [GitLab Webhook 文档](https://docs.gitlab.com/user/project/integrations/webhooks/)。不必提供项目 ID 才能使用此流程。

镜像网络可在管理员 PowerShell 使用辅助脚本建立测试规则（把示例字符串替换为实际 IPv4）：

```powershell
$brainGitLabIP = "GitLab 实际 IPv4"
$brainLocalIP = "本机实际 IPv4"
& D:\MO\code\obc_ai_code_review\scripts\brain_local_network.ps1 `
  -GitLabAddress $brainGitLabIP -LocalAddress $brainLocalIP -Port 18080
```

规则只允许指定 GitLab/本机来源访问指定本机地址和端口，Windows 与 WSL Hyper-V 两层都配置；已有同名规则时拒绝改变范围，先显式 `-Remove` 再重建。随后 `.env.brain-local` 设置 BRAIN_LOCAL_BIND_ADDRESS=0.0.0.0 并重新创建 api，便于本机 localhost 与远端 GitLab 分别验证。Windows 自己访问 LAN 地址失败不一定说明远端不可达，镜像网络的 hostAddressLoopback 默认为关闭；优先用 localhost 检查服务，再看 GitLab 的真实投递。无需为此重启其他项目使用的 WSL。

若 Windows 可以访问 GitLab、容器却超时，先在 WSL 执行 `ip route get <GitLab IPv4>`。当目的地址错误地走 docker0 时，说明 Docker bridge 与企业网段冲突；核对 `ip route show default` 后，可只为 GitLab 地址加一条 /32 路由：

```bash
brain_gitlab_ip="GitLab 实际 IPv4"
brain_gateway_ip="已验证的默认网关 IPv4"
brain_interface="已验证的出口接口"
sudo ip route add "$brain_gitlab_ip/32" via "$brain_gateway_ip" dev "$brain_interface"
ip route get "$brain_gitlab_ip"
```

添加前确认没有既有 /32 路由，不覆盖其他路由；该临时路由在 WSL 重启后需要重新核对。测试结束时按相同参数 `sudo ip route del ...` 回收；防火墙回收使用上述 PowerShell 脚本加 `-Remove`。不要直接改全局 Docker 地址池或重启共享 Docker。

本轮语义模型可通过 PROJECT_BRAIN_MODEL_NAME=deepseek-v4-flash、CLOUD_LLM_MODEL_NAME=deepseek-v4-flash 选择现有审查网关，VLLM_MODEL_NAME 保持已有本地隐私扫描模型。密钥只保存在本机测试配置；模型名称不代表网关一定支持，仍需上述真实协议探测。

先使用 Test → Push events 确认认证和投递；自动构建会读取目标分支真实 HEAD。再做一次实际目标分支源码提交，观察首次构建：pending → scanning → extracting → compiling → validating → ready。默认本机合并窗口 10 秒，Beat 每 15 秒恢复/调度，不应预期提交后立刻完成模型生成。

无法配置路由器/入站防火墙时，可使用 [临时公网 HTTPS 验证](project-brain-public-test.md)。它通过出站 Cloudflare Tunnel 转发，不以本机公网出口 IP 或路由器端口转发为前置；API 保持本机回环监听，公网代理只开放健康检查与 Webhook。

## 四、验收首建与增量

1. 无现有版本的目标分支发生源码变更后自动首建，不调用 POST /builds。
2. API 的 current.source_commit 与构建固定提交一致；coverage.valid_reference_ratio=1.0，下载 ZIP 核对卡片、事实和源码链接。对 source_bound 逐项核对 source_scope 的主体、关系分片和来源范围，并独立评估文本是否受源码支持；100% 引用有效不能替代这一步。
3. 修改一个模块，再提交目标分支；新版本 base_build_id 指向前一就绪版本，未变模块仍在完整知识集合中。再验证删除、重命名文件的旧知识没有残留。
4. 重送同一事件，观察未创建重复同输入构建、不打断租约。连续不同提交合并后维护最新 HEAD。
5. 暂停 auto_update 后提交不会建新版；恢复后重新核对最新 HEAD。
6. 真实模型响应错误、隐私过滤失败或预算不足时保留上一就绪版本；记录失败原因及 Token，不能将 ready 等同于人工语义质量达标。

## 五、启用写回与完整审查

这一步会在目标仓库提交文档或创建 MR，应在可丢弃试点项目验证。读取/评论身份使用 Reporter、发布身份使用独立具备必要 api 权限的机器人，仅授予试点项目；服务端目录强约束按 [hook 说明](project-brain-server-hook.md) 安装与验收。

1. 安全创建 `secrets/brain-local-publish-token`，填入独立发布 Token；不放进 `.env.brain-local` 或模型 Worker。目录 700、文件 600，Windows 挂载盘的实际权限还受 Windows ACL 控制。
2. 设置试点分支保护规则及 ai-brain/* 临时分支仅允许发布机器人推送，禁止 force push。先保持 PROJECT_BRAIN_MR_AUTO_MERGE=false，人工核对 MR。
3. 将 `.env.brain-local` 中 PROJECT_BRAIN_GITLAB_SYNC 改为 true，`dc --profile publication up -d --force-recreate api brain-worker celery-beat brain-publisher`。
4. 已自动接入的分支已有独立 gitlab_sync=false 状态；Swagger 调用 `PATCH /api/v1/projects/{project_id}/brain?branch=main`，body 为 `{"gitlab_sync":true}`。这用于显式启用试点发布，不是首建前置步骤；尚未接入的分支将在下一事件按新默认接入。
5. 验证未保护分支直接提交，受保护分支创建 MR；实际 diff 仅 PROJECT_BRAIN.md 和 docs/project-brain/，人工新增文件不被接管。手动修改机器人文件后下一发布应报告冲突，ready 知识仍可下载。
6. 手动合并机器人 MR 后没有知识/审查递归；混入业务代码的事件仍正常更新。确认自动合并权限、审批和 CI 条件后，再单独启用自动合并并重新创建 publisher。
7. 验证全部审查链路时执行 `dc --profile review up -d review-worker`。第一轮累积的审查队列也将被消费，并可能发表评论；应先确认这些均来自试点。若需从空队列开始，保留测试卷备份后重建全新测试栈，不能清空业务 Redis。

## 六、停止、恢复与回滚

本机测试跨越关机或待机时，先按具体构建的 UTC 起止时间核对 Windows System 的启动/关机/新型待机事件，再核对 WSL/Docker 启动时间、容器 RestartCount/OOM、Worker 日志间隔和任务租约。墙钟耗时包含环境暂停，不能全部计作模型耗时；主机无电源事件也不能排除 WSL 单独退出。缺少构建结束时间或保留日志时，明确标记无法排除，不推测原因。对可以离线复现的程序错误保留独立归因，不能用待机期间的时间重叠替代因果证据。上述检查只读，不改变整机电源策略或重启共享 Docker/WSL。

仅更新本机应用代码时，先备份 `.env.brain-local`、独立 DB/知识卷并记录旧镜像 ID。与公网验证共用 `dc_public` 函数（见公网说明），保留数据库和隧道，防止 Quick Tunnel 换域名：

```bash
dc_public stop celery-beat brain-worker api
# 对照 .env.example 同步 .env.brain-local；模式默认auto，切换前完成合成/有界真实验证。
dc_public build api
dc_public up -d --no-deps api
dc_public exec api alembic upgrade head
dc_public exec api alembic current
dc_public up -d --no-deps brain-worker celery-beat
dc_public ps
curl -fsS http://127.0.0.1:18080/health
dc_public logs --tail=30 api brain-worker celery-beat
```

当前版本新增分模型记账迁移，应确认为 `007_brain_usage`。先迁移API，再启动Worker/Beat，避免未迁移表被新任务访问。公网代理使用 Docker DNS 动态解析 API，不需要重建隧道。确认 Brain 扫描和语义模式已按上述范围验证；GitLab 同步、publisher 和 review-worker 保持关闭。生成配方变化后新建构建，不向旧检查点混入新实现。回滚时停止这三个应用角色、恢复旧镜像及备份配置、按上述顺序启动验证；保留 DB/卷，不降级或删除数据。统计和异常预留口径见 [分模型用量](project-brain-model-usage.md)。

```bash
# 停止所有测试角色，保留本机测试数据；不影响其他 Compose 项目。
# 若使用公共隧道，使用公共验证文档的 dc_public down，包含 overlay/profile。
dc --profile review --profile publication down

# 恢复时先同步测试环境变量，重建、迁移，再启动默认只生成模式。
dc build api
dc up -d postgres redis
dc run --rm check python scripts/brain_local.py bootstrap
dc up -d api brain-worker celery-beat
dc ps
curl -fsS http://127.0.0.1:18080/health
```

需要恢复只生成模式时，关闭配置及已登记分支的 gitlab_sync，停止 publisher。测试服务回滚可恢复旧镜像/代码并保留 DB/知识卷；默认不降级迁移、不删卷。仓库已写文档通过普通 revert 回退。结束测试后移除/禁用新增测试 Webhook，撤销试点 Token，清理测试卷前先确认 Compose 项目和备份；不要对其他项目执行 down -v。

## 编译隐私窗口回归

输入完整扫描若实际消耗过模型调用，先在语义请求之前正常续跑；成功收据保留在 Worker 私有 HMAC 索引中。下一投递从固定 SHA 重建输入，再重放规则和全部指纹，完整扫描收据有效才开始语义请求。输入按8次调用续跑；从1.1.2起，已收到语义响应的输出及必要重试reasoning使用独立64次调用上限，仍共用480秒工作窗口，不跳过扫描、不保存原模型响应。规则模式或已完成输入收据不增加投递。

输出或格式重试必要 reasoning 的扫描窗口耗尽时明确终止，不允许以正常续跑隐式重新生成已收到的语义响应。来源绑定的结构诊断仅记录固定类别及缺失/多余/重复数量，模型生成的键、槽位和值不写日志。此修复改变配方；验证后创建新构建，不用新代码继续旧检查点。

不联网回归：`pytest tests/unit/test_brain_compiler_privacy_window.py tests/unit/test_brain_source_bound.py tests/unit/test_brain_source_bound_capacity.py -q`。真实模型调用仍按当前对话的次数和总预算授权执行；模拟通过不等于完整项目或业务质量通过。输出次数变量与配置见 `.env.example` 和[分层汇总说明](project-brain-hierarchy.md)；重建应用、迁移确认、健康与回滚按本机测试说明的部署更新部分执行。

已有语义响应后的并行预算暂占会在当前进程内存中有界异步等待，保留输出、必要 reasoning 和格式次数，为下一本地/语义调用分别留60/120秒。纯输入及首语义请求仍持久等待；真正预算耗尽仍终止。已执行过语义请求后，格式重试的时间/次数门禁不足不能通过正常续跑复位格式次数。共享预算异常移至 exceptions.py，旧 lifecycle 导入兼容且实现纳入配方。

## 完整响应中的 JSON 语法错误

`finish_reason=tool_calls`、唯一固定函数和字符串参数只能证明传输形状符合要求。2026-10-12真实重建的一个15主体包满足这些条件，却仍返回非法 JSON，且未达到输出上限。不能用提高预算、裁剪证据或修补残缺字符串把它发布成知识。

编译器把语法层错误单独标记为 `invalid_json`，只反馈固定 JSON/转义规则；槽位、证据、路径校验仍按各自规则执行。日志只记录封闭的语法类别与数字位置，不含参数正文、JSON键、原始解析错误消息或值。每次格式重试仍完整预留/计费，必要reasoning与最终成功输出仍完整扫描；重试次数和工作窗口不复位。更改实现后创建新配方构建，不能续跑旧配方检查点。离线定向回归见 `tests/unit/test_brain_json_retry.py`；模拟重试成功不证明网关的真实错误率已经降低。

## 汇总来源元数据与确定顺序

汇总结论的直接 `source_scope` 在传输时用 `I` 引用共享 `source_scopes` 表，表按 `columns` 和 `defaults` 解码。历史主体/关系身份使用独立 `O` 别名，来源包摘要使用 `D`，证据继续使用本包 `E`。服务内存的逆表可精确恢复原身份、列表顺序、分片和摘要；正文、未知项、完整规范来源数据不删减。未知包装字段仍完整传输并按原规则扫描，同一来源范围 ID 内容冲突、字段缺失或隐式类型转换直接拒绝。

这些历史身份仅用于追溯解释看过什么，不代表当前包新增了确定关系或证明业务语义。汇总的历史关系 ID 本身没有解析状态，不能从别名推导确定调用。完整输入/输出隐私扫描、来源绑定槽位和发布验证不因压缩放宽。

汇总调度与最终组装查询显式按 `BrainUnit.id` 排序，以固定结论、分包和首个文档建议路径的选择。模拟回归见 `tests/unit/test_brain_context_summary_scope.py`、`tests/unit/test_brain_unit_order.py`；后者使用 SQLite 的 `reverse_unordered_selects` 验证无序查询的反例。压缩和提示词改变生成配方，后续真实验证必须新建构建，不能继续旧检查点。

2026-10-12 对132个完成编译结果的完整离线重划分得到118个汇总包，原计划为226个；约48%的传输字节减少不等于实际Token或金额节省。该次测算仍直接汇总叶级编译结果，并非后续1.1.x的分层概览容量结论；不要据此承诺整项目预算足够。当前前沿规划、关系视图及完整树门禁见[分层汇总说明](project-brain-hierarchy.md)。

## WSL 重启后的服务生命周期

先核对容器状态、health及Docker启动时间，再恢复持久任务。若所有基础容器同一时刻退出255、无OOM标记，且Docker启动时间在命令之间反复变化，检查Ubuntu是否缺少前台常驻进程。[微软说明](https://learn.microsoft.com/en-us/windows/wsl/systemd)指出systemd服务本身不会保持WSL实例存活。

本轮采用隐藏的Windows `wsl.exe -d Ubuntu -- sleep infinity` 进程保活，PID与启动时间只记录在ignored `secrets/brain-wsl-keepalive.json`，不修改系统设置。它没有轮询或网络行为，不恢复用户暂停的定时监测。创建前先核对记录进程仍存活，避免重复；服务停止并结束本机测试后，核对进程名与启动时间，再只结束该自有进程。不要为保活重启共享Docker/WSL。

WSL重启也可能遗失GitLab精确/32路由；先 `ip route get` 确认实际接口和网关，再恢复原单地址规则，不照抄旧接口、不扩大默认路由或防火墙范围。
