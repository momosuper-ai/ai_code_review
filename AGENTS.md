# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## 交接入口与持续记录（2026-10-26）

**新会话先读取根目录 [AGENT.md](AGENT.md)。** 该文件是本会话已整理的总结、关键结论、最近执行状态与待推进清单；本文件下方保留原有规范和历史记录，历史段落中的“最新”仅指写入时刻，不能覆盖较新的交接事实。

用户明确要求：**以后每一次重要修改或关键结论，必须先更新 AGENT.md 的当前状态、依据/验证限制、待推进事项及重要记录，再向用户报告完成。** 不只写进聊天、ignored状态或memory；不把计划、离线通过、实际部署和正式质量验收混为一谈。不记录凭据、源码/模型原文或脱敏映射。

交接整理时的历史要点：当时最后登记为r4，扫描ConnectTimeout失败、模型0；r3保留119/127编译（59复用子集），当时观察目标错位且API未运行。本聊天新授权后已另行唯一登记2.3.0构建`242db042-a2ae-4685-9d20-81971186a4c8`，暂停/继续实测通过，14:24正式启动；网络和观察目标已按实际证据修复。历史r4/journal/全部旧账保留，不重复POST或伪造frozen；当前实时状态与边界以AGENT.md第5～9节为准。

历史交接阶段的授权仅为整理交接，不追加启动、retry、真实模型探测、额度、写回或自动化迁移授权。该限制不构成后续执行授权；后续执行必须以用户新的明确请求为依据。保留所有已submitted journal、检查点、unknown与旧账；大量未提交/未跟踪实现文件不得reset/clean。新会话不得按下方旧运行文字擅自开工。

## 本聊天后续明确执行请求（2026-10-26）

交接/只读分析之后，用户在本聊天明确要求：**“我希望你写一个暂停的算法，能够在我明确告诉你该暂停的时候能够保留进度并暂停，在我需要继续恢复时能够正常恢复，写完之后直接帮我开始进行完整验证，每隔20分钟汇报一次进度”**，随后又明确“继续”。这是真实用户的新请求，授权依据来自聊天，不能仅凭本文件自行扩大。

- 当前任务因此包含：实现并验证暂停/继续；必要隔离部署；一次固定SHA、同两模型/完整隐私、写回关闭的完整试点验证；本聊天每20分钟进度汇报。它不是仍停留在上文历史“仅交接”的阶段。
- 费用仍受原轮8,000,000上限约束，旧占3,311,936和所有unknown/HTTP上界原样保留，新调用cap仅原剩余4,688,064；不增加总额度、不清账、不额外模型probe、不启用写回、不自动失败后另建。
- 新代码完整recipe已改变，不能新代码retry旧r4；拟以已认证r3检查点为父，旧r4零模型扫描失败及submitted journal全部保留。新控制目录`secrets/brain-v230-validation-20261026/`，唯一登记必须先O_EXCL journal，提交后不可重发。
- 2.3.0已完成2044单测、44一次性PG、249定向、113门禁合成及实际5父六组件兼容；010与API-only部署通过，Worker/Beat仍停。登记目标是隔离API容器内`127.0.0.1:8000`，不是向GitLab/模型网关登记或写入；GitLab只读、发布0。具体真实状态见AGENT.md，准备/登记/付费生成/正式验收必须分别记录。
- 2026-10-26首个登记工具请求曾被自动审批拒绝，**进程未执行、journal不存在、未登记新构建、模型0**。不得换工具/路径绕过；只可补核真实用户授权与只读状态证据后重新送审同一动作，或请求用户批准。被拒的事实应保留，不能写成已登记。

## WSL 本机 Project Brain 验证

最新终态（2026-10-23）：当前r3构建27a6778c-9352-4185-b510-3bd046dac0a5已于09:51:05.767Z（北京时间17:51:05）failed，compile10083首attempt/model_calls_incomplete；privacy165/165、source_privacy127/127、compile119/127完成（其中59复用）、0运行，另1失败及7随终止取消。新实结1201687、unknown6719（a7563bc1，09:47:52.749Z预留，未确认结算）、live0/HTTP上界0、占1208406；四层旧账2103530不变，整轮3311936/8M，账面剩余4688064不等于新调用授权。Windows506于09:47:54.573Z进入Modern Standby、507于09:56:16.759Z恢复（09:42:45另有待机），与未知调用及DB终态时间重叠；容器restart0/OOMfalse。原异常被保护替换为ModelCallsIncomplete，日志只见该类别，不能确诊直接由休眠、HTTP500、网关或模型能力导致；unknown也不单独证明请求到达网关。awake756按failed终态释放，WSL保活25452仍身份有效；不重开助手/改电源计划。完成检查点、旧账与unknown均保留，无正式manifest/ZIP、50题不评分；只读阶段报告secrets/brain-quality/27a6778c-9352-4185-b510-3bd046dac0a5/assessment.{json,txt}。本轮已报告失败及119/127（较上次+6、0运行），每5分钟进度通知继续，未来无新证据不重复根因。不得自动retry/另建/探测/加额/写回；建议后续先补固定原异常与发送阶段诊断元数据、合成验证，再由用户指示认证恢复。下方r3运行中描述是终止前历史。

最新进度通知偏好（2026-10-23）：用户明确要求每一次都报告具体进度，不仅说是否正常。已通过automation_update更新原gitlab-project-brain（同ACTIVE/每5分钟/同线程）generic prompt，正常推进或未变也NOTIFY；每轮提供北京时间、当前阶段completed/total/running、相对上次已报告进度增量及下一步，有异常优先说明，读取失败明确无法确认，不用旧缓存冒充实时。已完成数包含复用compile子集，不能另加或当整体百分比。每轮将实际已报告进度持久到active-trial/monitor-state的last_reported_progress及r3/last-reported-progress.json，普通save_observation不能替代这项报告基准记录。详细UUID/金额仍仅本机，不追加外部prompt；预算/模型/隐私/唯一请求journal和写回边界不变，不授权额外模型探测或续跑。此偏好覆盖下方所有“正常推进保持安静/仅重大事件通知”的历史指令；已知失败无需重复根因，但用户要求的定时进度仍应给出。

最新认证续跑（2026-10-23）：用户明确“之前电脑休眠…未完成帮我续跑”后，实际检查4a仍failed且unknown5286不变；本次授权仅用原轮8M剩余5896470续一次同2.2.1恢复。06:26:17.921Z（北京时间14:26）唯一O_EXCL POST已submitted新historical执行27a6778c-9352-4185-b510-3bd046dac0a5，直接父4a再上3eff/980d/32ae，原SHA7d89860efe1113bcb1480bac2f1aaa9b1045bd8d、recipe aec96ef005d49cb06882bc873d75c033da460e6e25e81cd54d050e3edc717cca、镜像8d6a52db…eebb92/009/同两模型/完整隐私不变。四层私有MAC/history/receipt/DB和6组件兼容、旧表全行指纹、无活动租约/在途及47.1MB隔离dump已通过实际预检；76completed compile为候选（内含10继承），不可加14或宣称已全复用。旧4层实结2070719、unknown12312（d005319b5286+fd97ce667026）、closedHTTP上界20499、占2103530及更早账全保留，未清账/补结算；新cap5896470合计仍8M。API/Worker/Beat实际新cap，APIfalse/health200/mainpaused/auto_updatefalse/syncfalse/review+publisher停，公网proxy恢复，无额外模型探测。新控制secrets/brain-v221r3-validation-20261023；旧所有submitted journal不动，新request-attempt不可重发。新observer15139只读，Windows临时awake756/thread49088实际active、最多12h/终态释放不改电源计划，原WSL保活25452身份有效；旧awake37028已释放不可用。scan/extract完成，recovery frozen4层，privacy按固定输入核验并已收据命中，尚无正式产物/质量分。原generic heartbeat仍ACTIVE每5分钟、仅本地active-trial已切新目标，不追加外部详细提示词；不要再用r2/tmp-status或4a旧终态当当前。r3 recovery-7 gate已pin27a、101合成回归通过，冻结50题45f6…72cd9不变，ready后正式核验再评，failed仅诊断不自动retry/另建/加额/探测。下方4a失败和待授权是本次之前历史。

最新终态（2026-10-23）：当前4a63506b于06:07:34.674Z（北京时间14:07:34）failed，compile9617首attempt/model_calls_incomplete，privacy165/165、source_privacy127/127、compile76/127完成，50单元随终止取消、10历史compile实际复用。当前实结1231000、unknown5286（调用d005319b，06:07:32.329Z预留，无响应用量确认）、live0、占1236286；三父867244及所有更早账不动，整轮2103530/8M。unknown是费用上界不是实耗，不清零；账面余5896470非新调用授权。无正式manifest/ZIP、不评50题；报告brain-quality/4a63506b…/assessment.{json,txt}及独立power/status/log闭合证明。Windows实际506/507证实03:57:54Z入Modern Standby、06:02:06后多次进出至06:07:05，解释长时间进度暂停；容器restart0/OOMfalse，但不能证明06:07:32最后调用直接由睡眠导致。当前unknown保护覆盖原异常，仅持久ModelCallsIncomplete，新日志没有原异常阶段，不能归因模型能力、HTTP500或具体网络超时；仅只读诊断，无真实重现/探测/重试/新建/加额/写回。awake37028按终态正常释放，实际状态见active-trial；原WSL保活25452不改，Worker/Beat服务仍running但本构建已终止，mainpaused/APIfalse/syncfalse阻止额外构建。原heartbeat继续ACTIVE每5分钟，未来仅新实质事件才报告，已报告失败不重复；用户明确指示前不续跑或另建。下方运行中是终止前历史。

最新同构建续跑（2026-10-23）：用户明确“帮我续跑吧”后，仅恢复原active 4a63506b，不POST/不retry/不另建/不清账。02:08:05Z实际只读预检通过：原SHA/recipe/2.2.1镜像8d6a52db…eebb92/009/cap7132756不变，当前私有收据及3父链认证通过，唯一active、publication0、8调用全settled3139/unknown0/live0；两个遗留source_privacy9498/9499均attempt1/租约过期/零预留，可由Beat正常回收。02:08:28Z（北京时间10:08）专用Worker与Beat已恢复，02:09:13Z实际9500/9501已领取新有效租约，无新增错误，检查点保留，尚无正式产物/50题分数。自有隐藏WSL保活25452（具体start见brain-wsl-keepalive.json）、Windows临时awake37028/thread18792新鲜active（最多12h/终态释放、不改全局电源），旧13616/41848已失效不可用；原控制目录awake文件已更新并保留旧副本。前台observer94962只读写原run-state，约4h窗口不等于任务生命周期，原5分钟heartbeat继续读active-trial。API仍false/health200、mainpaused/auto_updatefalse/syncfalse、review/publisher停；同两模型/完整隐私、旧三层867244和更早账不动，无额外模型探测。续跑闭合证据secrets/brain-v221r2-resume-20261023，原request-attempt保持submitted不可重发。ready后按原recovery-6门禁及冻结50题评估，失败仅诊断，不自动另建/加额。下方“Worker停/待用户授权续跑”为本次恢复前历史。

最新只读检查（2026-10-23）：用户问昨天休眠后是否完成。实际隔离API于01:53:01Z读取4a63506b仍extracting，privacy165/165、finalize完成、source_privacy84/127（41pending、2遗留running），compile未开始，正式manifest0；尚未完成，不评50题。当前8privacy全settled3139、unknown0/live0，父867244不变、整轮占870383，检查点保留。原API/Worker/Beat/PG/Redis均于10-22T10:09:43.31Z集体退出255/OOMfalse，原awake41848和WSL保活13616均无进程；Windows指定电源事件按LocalDateTime查询并再以UTC过滤，确有10-22T10:31:51Z入待机、10-23T01:45:52Z恢复，退出早于睡眠约22分钟，不能把原退出直接定因为休眠。仅为读DB临时启动隔离PG/API，未启动Worker/Beat、未调用模型/retry/dispatch/恢复函数、未改配方/额度/写回或重启共享WSL-Docker；查询会话结束后API再次不在运行，服务尚未稳定恢复。两个DB running状态不表示进程仍执行。证据及最新状态在v221r2/execution-check-20261023.json与brain-active-trial.json；已告用户未完成且中断，后续无新事实不重复。保持原定时只读监测，但不能当作用户授权恢复生成；用户明确继续后需先稳定WSL生命周期、核同SHA/配方/私有检查点/租约与预算，再恢复本active执行，不自动另建。以下“正在推进/防待机active”是昨天历史状态。

最新实际继续运行（2026-10-22）：用户“local_model已经恢复了，可以继续了”授权后，已仅一次O_EXCL POST登记同2.2.1恢复4a63506b-ab60-497c-95e5-f0e0a5a6ddce（09:11:06Z/北京时间17:11），直接父failed 3effa、再上980d/32ae。新cap7132756+三层旧占867244仍原轮800万；旧839719实结/20499关闭HTTP错误上界/7026unknown及全部更早账原样保留，绝不把HTTP费用上界当unknown或实耗。原SHA7d89860efe1113bcb1480bac2f1aaa9b1045bd8d、recipe aec96ef005d49cb06882bc873d75c033da460e6e25e81cd54d050e3edc717cca、2.2.1不可变镜像8d6a52db…eebb92/009/同网关deepseek-v4-flash+local_model/完整隐私不变，无额外真实探测。三层私有/DB/全部旧行摘要、6组件兼容、无活动租约和新45.7MB备份已通过实际预检；14compile仅候选。API/Worker/Beat实际新cap验证，APIfalse/health200/mainpaused/auto_updatefalse/syncfalse/review+publisher停/公网proxy恢复。新目录secrets/brain-v221r2-validation-20261022，request-attempt已submitted不可重发；只读前台observer73634，新Windows临时awake PID41848/thread24604实际成功active（最多12h/终态释放，不改全局电源计划），WSL保活13616身份仍核验有效。scan/extract完成、recovery frozen3层、privacy进行中；真实复用数看新run-state，不能将预检候选或收据hit当完成率/节省率。原heartbeat ACTIVE每5分钟，generic prompt不改/只读本机active-trial新目标；不再用旧v221/tmp-status、旧PID22772或旧3eff终态当当前。ready评估使用新目录recovery-6 gate已pin4a63506b，冻结50题45f6…72cd9不变，92合成回归通过；正式产物/质量尚未验证，failed仅诊断不评分，不自动新建/重试/加额/探测。下方“等待用户指示/未提交/唯一目标3eff”均历史。

最新继续授权（2026-10-22）：用户明确说“local_model已经恢复了，可以继续了”，授权在既有原轮800万的剩余范围继续一次同2.2.1认证恢复，不额外探测、不改模型/隐私/源码SHA/配方。直接父改为failed 3effa39c（再上980d/32ae），旧三层占867244=实结839719+closed_http_error上界20499+unknown7026完整保留，新调用cap7132756；余额不清零也不另加额。上一份v221 request-attempt已submitted不可重发，准备新目录secrets/brain-v221r2-validation-20261022/new authorization来源current_user_message_local_model_restored_continue，仍原SHA7d89860e…/recipe aec96…/2.2.1镜像8d6a52db…eebb92/009。需要真实三层私有认证与全旧行摘要/无活动租约、隔离备份及临时防待机核验后，唯一O_EXCL POST登记新子执行；不是对上次失败原地retry（未恢复HTTP组会409），复用既有成功收据/compile而不伪造当前调用。此段编写时尚无新journal/build/模型请求；root正执行预检。原heartbeat仍ACTIVE、通用prompt仅引用本机记录，不把内部细节追加外部；新build成功登记后再更新active-trial/本地监测目标与正式评估gate，先不擅自改监测对象。写回/API额外入口关闭、mainpaused/review+publisher关闭，失败停下诊断、不自动再建。下方“等待用户指示/无新授权”属本次继续前历史。

最新终态（2026-10-22）：2.2.1唯一恢复3effa39c-95f6-48e5-9db6-17b154c0bc86已于07:45:36.947Z（北京时间15:45:36）failed，privacy9219/model_http_retry_exhausted。07:44:58、07:45:14、07:45:32Z同组初始及两次重试均完整关闭HTTP500，身份与序号一致，显式重试确实执行；不是旧SDK未识别，也不是未知响应丢失。privacy140/165完成、24随终止取消，17948收据hit非独立覆盖；新4privacy实结1267、3http_error费用上界20499、总占21766，unknown0/live0/semantic0/publication0，费用非精确。旧两层845478/unknown7026保留，整轮占867244/800万、账面余7132756不是新调用授权。API/Worker/Beat仍2.2.1/009同镜像，APIfalse/mainpaused/auto_updatefalse/syncfalse，容器running/restart0/OOMfalse；07:43-07:48:30Z的System指定电源事件查询成功且0，不支持本次因待机；只能确证网关连续500，内部上游原因须查日志，不能归因模型识别能力。observer终态退出；awake助手22772按failed正常释放并退出，不再重开，系统电源策略未改。无正式manifest/ZIP，50题不评分；阶段报告secrets/brain-quality/3effa39c-95f6-48e5-9db6-17b154c0bc86/assessment.{json,txt}，HTTP组/电源元数据在v221控制目录。失败已本轮通知，未来无新实质事件不重复。保持原heartbeat ACTIVE/5分钟至用户结束，只读跟进，不自动retry/新建/探测/加额；下一步先核网关北京时间15:44:58-15:45:37的local_model上游日志，再由用户指示续跑。自动审批拒绝将详细终态与费用追加到外部应用提示词；允许的安全替代是通用监测指令、细节仅留本机，不直接改自动化配置文件绕过。

最新定时观察授权（2026-10-22）：用户明确要求及时检查并设置定时任务，已通过automation_update将原heartbeat gitlab-project-brain恢复ACTIVE、每5分钟，目标仍本对话，但prompt已更新为唯一2.2.1构建3effa39c-95f6-48e5-9db6-17b154c0bc86。本条覆盖下方“旧automation PAUSED”的历史状态；不得新建重复任务。定时独立查实际DB、服务/新增日志/两层旧账/防待机状态，前台observer约4小时结束不等于构建失败；无变化保持安静，ready/failed/确认异常/需操作/质量评估完成才通知。不得自动retry/新建/加额/真实探测/改模型或隐私/写回/重启共享WSL-Docker。ready按v221 assessment-readiness.md完成正式gate与冻结50题，失败仅诊断不评分。新增v221/log_check.py使用每服务UTC游标、读取失败不推进、只导出固定统计不存原始日志；首次实际检查07:25:28Z所有容器running/restart0/OOMfalse、无新增错误。07:25:32Z privacy完成50/165、收据6326hits、新3privacy settled1104/unknown0，无当前错误；不是最终质量或Token节省率。最新定时授权和状态见secrets/brain-active-trial.json、secrets/brain-local-monitor-state.json及v221/scheduled-monitor.json。此前PAUSED是用户此次恢复前的历史，今后保持监测直至用户明确结束。

最新实际运行（2026-10-22）：用户恢复工作区额度并要求“开始吧”，已按既有授权仅一次登记2.2.1认证恢复3effa39c-95f6-48e5-9db6-17b154c0bc86，07:14:23Z（北京时间15:14）提交，父980d/祖先32ae、原SHA7d89860efe1113bcb1480bac2f1aaa9b1045bd8d、recipe aec96ef005d49cb06882bc873d75c033da460e6e25e81cd54d050e3edc717cca，historical/full。新调用cap7154522+父两层占845478=原轮800万，父unknown7026及全部更早账保留，不清零。真实两层私有/DB材料与六组件认证、备份及部署前后旧表摘要核验通过；API/Worker/Beat均镜像8d6a52db…eebb92/009/cap7154522/同网关两模型/完整隐私。扫描和静态extract已完成，recovery frozen父2层，privacy已出现实际收据命中，候选14个compile不等于承诺全部复用。API已恢复Brainfalse且health200/mainpaused/auto_updatefalse/syncfalse/publication0，公网代理恢复，review/publisher仍停/旧automationPAUSED。当前只读前台observer72466写secrets/brain-v221-validation-20261022/run-state.json；request-attempt已submitted，绝不可再次POST。Windows临时防待机自有pwsh PID22772/thread51020、execution request实际成功且active；最多12h，终态自动释放，不修改电源计划，不防人工关机/主动睡眠。旧WSL保活13616身份已核，不重启共享Docker/WSL。新gate recovery-5固定此build，冻结50题45f6…72cd9、pin后77合成通过；尚无正式产物/质量评分。失败停下诊断，不自动新建/retry/加额/探测；ready后验证正式manifest/服务哈希/私有seal/history/两层恢复账及固定Git，再评50题，不能把收据命中或证据覆盖当业务回答准确率。启动记录见该目录launch-summary.json和secrets/brain-active-trial.json。下方“审批阻塞/未部署/未提交”均为已解除的历史。

最新待执行授权（2026-10-22）：用户“那继续吧/继续”已授权一次2.2.1认证恢复，目标父980d（祖先32ae）、新cap7154522、旧两层845478/unknown7026保留，原轮8M不变。准备目录secrets/brain-v221-validation-20261022含authorization.json/status=authorized_not_submitted、recipe_hash=null、无request-attempt.json/新build，授权未消耗。真实WSL before/parent预检调用因自动审批服务“Your workspace is out of credits”未能审核而被拒绝，操作未执行；非unsafe判定，不绕过/不反复重发，需工作区额度或审批服务恢复。未部署/未启动新模型或Worker/Beat，也未启动防待机请求。上次成功只读确认仍API2.2.0/009、旧Worker/Beat退出0。已完成Python辅助AST、keep_awake.ps1语法和本机两层门禁77合成测试（无DB/模型/电源请求）；gate recovery-5父链固定980d/32ae、独立两账，AUTHORIZED_BUILD待真实登记固定。临时防待机helper仅自有进程线程请求，不改电源计划；启动脚本须assert_awake实际确认后才允许付费Worker。使用/边界见该目录preparation-readiness.md，恢复前必须执行真实双层认证/配置绑定/备份/新镜像围栏；当前授权有效，无需仅因审批服务恢复再索要费用确认。下方失败及运行文字是更早时点。

最新终态与离线修复（2026-10-22）：用户确认电脑自动待机；980d439b-8be7-4287-8411-1266720a5b5c已failed，privacy8994首attempt/source_binding_invalid，完成80/165、84随终止取消，9954收据命中是计数非独立覆盖率。11次新privacy调用均02:59:30Z前settled，共4060/unknown0/live0、无语义/正式manifest/ZIP/50题评分。Windows506于03:03:12Z进入Modern Standby、507于03:48:45Z退出；两单元03:03:02/08开始，03:13:02/08 soft600与后续hard660，DBfailed03:13:03；容器restart0/OOMfalse。父DB/私有材料事后重新认证均valid。合成确认SoftTimeLimitExceeded能被恢复层及下层_audit_seal包装为终态来源错误，证据支持待机相关超时+误分类，旧原栈被隐藏无法逐层回溯；另复现当前索引无锁双读竞态但不宣称其就是本轮根因。2.2.1已离线修复：三包装有界最多16个真实异常cause/context按id防环、仅真实billiard中断类型透传、不读正文；当前index用原_index_lock一致读取，同写者锁序，合法版本推进仍不复用，父/MAC/来源/seal门禁不降级。privacy_history及内容协议未改，6组件代码指纹兼容；新21例、Linux1957/一次性PG39/189hash全通过，模型0，镜像8d6a52dbade2ffcd16fa79fac3e3f3806a01a816a7534be63a3756ea5feebb92仅offline未部署。运行仍API2.2.0/009/Brainfalse/mainpaused/syncfalse，专用Worker/Beat已停，foreground82647自然结束/旧automationPAUSED；Windows电源计划未改。80检查点/父834392+7026unknown=841418及所有旧账保留；本轮累计845478，余额7154522仅对账非新调用授权，不自动retry/新建/真实探测/清账。报告secrets/brain-quality/980d439b-8be7-4287-8411-1266720a5b5c/，修复证明secrets/brain-v221-offline-20261022/validation-summary.json，说明docs/project-brain-interrupt-recovery.md。WSL保活不能阻止整机待机，后续长测需保持唤醒；新配方不能注入旧付费单元，需认证恢复流程。以下运行中表述均为更早历史。

2.2.0正式评估准备（本次恢复仍运行中）：secrets/brain-v220-validation-20261022/assessment_gate.py 为recovery-4，独立核原父链/导入归属、子新调用与父unknown分账、固定Git树/所有正式产物/私有seal/history/ZIP；75项network-none离线测试通过，未执行在线ready门禁或50题评分。冻结questions_source.json字节复制自v2原件，SHA45f6c603fc02fb7fd84e7265e7bd4bac3e9c7aa4950b46f0958f226eb0072cd9；使用见该目录assessment-readiness.md、证明assessment-gate-offline-proof.json。只在980d439b ready后执行，旧2.1.0 gate授权scope不能直接用于新构建；报告须完整当前/历史/恢复父隐私过滤，不把词项命中当回答正确。当前状态与费用应读run-state.json，前台monitor82647持续但旧automation仍PAUSED。

最新恢复运行（2026-10-22）：用户“可以，开始吧”已授权启动一次2.2.0认证恢复，按原800万轮余额7158582硬上限执行，父841418不清零。build980d439b-8be7-4287-8411-1266720a5b5c，父32ae7934-89ff-4456-86b1-a7884ef2bcdf，原SHA7d89860efe1113bcb1480bac2f1aaa9b1045bd8d，recipefe09069585131472c5a7ee0e88d952fc93a95aa5d73aef3e0c37212b323c39ad，historical/full。旧Worker/Beat先确实停止，原Worker容器冻结环境只在RAM传给原不可变2.1.0镜像，原recipe复算相同并签六组件证明；父key/index/origin/seal/history/收据及14compile真实成功调用通过，六组件与新环境相同。45.7MB隔离库私有dump已保存；运行库已迁009，API/Worker/Beat均ae86f022…1daf7a且cap7158582，同网关deepseek-v4-flash/local_model/完整隐私/syncfalse。API健康200，原所有旧表行摘要部署前后一致；一次O_EXCL POST于02:46:02Z提交并已关闭API新建入口。main仍paused/auto_updatefalse/当前指针空/review+publisher停，旧automation仍PAUSED；只前台observer82647监控，不自动启用定时观察。私有父认证状态frozen，初始privacy进行中且已出现真实收据命中，尚无正式产物/评分，不能宣传14全复用或实际节省比例。旧父834392实结/7026unknown/841418占用及更早账保持；失败停下诊断，不自动新建/追加探测/扩大额度。控制secrets/brain-v220-validation-20261022；授权、请求journal、原配置认证、安全进度与备份在该ignored目录。真实认证模块挂载应保留/tmp/brain-recovery/core/project_brain/recipes.py层次，浅层/tmp文件会在模块导入的parents[2]失败（已修部署脚本与说明，生产实现及recipe未改）。以下“未部署/未迁移/未授权”仅为先前时点。

最新离线工程状态（2026-10-22）：用户因反复完整重建浪费明确要求先完善断点恢复；project-brain/2.2.0 已实现认证父子恢复，Linux1936/一次性PostgreSQL schema39全部通过，真实模型0。离线镜像sha256:ae86f022081c0556a0ccfdce66529acfcea01c9fa566606077a8c5f4b51daf7a、188文件hash/实际导入核验通过；普通及硬编码凭证合成项目均恢复到ready/完整ZIP，继承compile不调用SDK，旧账/unknown/检查点归属不变。六阶段组件指纹与完整审计recipe分离；仅内容兼容、完整输入/结果/证据与私有key/index/origin MAC/seal/history/收据认证后复用，不直接继承summary/assemble。新建historical子构建，保持固定SHA/分支paused/当前指针和发布关闭；父unknown不补结算或伪造成当前成功调用。新增009_brain_recovery，仅在一次性schema验证，运行库未迁移、镜像未部署、未启动真实恢复或新模型探测。本轮未读取真实栈状态；上次确认API2.1.1/008、Brainfalse/Worker+Beat停止/syncfalse/automation PAUSED，不能以离线测试断言今日健康。32ae7934旧实结834392/unknown7026/占841418和全部更早账保留，7158582余额不是新费用授权，未生成真实正式产物/50题分数。GET recovery-plan仅候选，POST recovery登记子构建；同父同配方活动登记并发复用，父被引用后禁止原地retry。父新隐私写入先以独立UUID持久意图并隔离全部后代再更新私有索引，成功清自己的意图，中断保留且拒绝新恢复；ready后发现仍隔离，API无私有卷时用DB依赖门禁。旧版无隔离回调，实际继承前必须停/排空旧Worker，brain_recovery_attest.py以--workers-drained声明并在真实旧镜像/原配置/原recipe一致时签私有组件证明；不得用当前配置回填旧DB。旧2.1.0镜像六组件在相同合成配置下相等，不代表项目480实际历史配置/检查点已认证，也不是新执行授权。控制secrets/brain-v220-offline-20261022，见docs/project-brain-checkpoint-recovery.md。以下最新终态/部署文字均为更早历史时点。

最新终态与修复（2026-10-21）：2.1.0唯一授权构建32ae7934-89ff-4456-86b1-a7884ef2bcdf于08:33:36Z（北京时间16:33:36）failed，compile8797/首attempt的semantic_output隐私扫描HTTP500，SDK默认httpx2.Response被旧httpx类型检查拒绝，未进入显式重试。165/165 privacy、127/127 source_privacy、14/127 compile；实结834392/unknown7026/占841418/live0，余额7158582仅对账，不是新调用授权。旧账全部保留、unknown不回填；无正式manifest/ZIP/50题评分。2.1.1新增src/core/http_transport.py显式兼容真实httpx/httpx2响应与超时，除closed/consumed还要求完整content缓存可无I/O访问，拒绝部分读后关闭/伪造状态对象，不补读或解析错误正文。adapter及两HTTP包版本纳入recipe，必须新配方新构建，禁止续旧paid检查点。默认SDK16项旧镜像7红9绿，修复SDK3.26/httpx2及SDK2.13/httpx各16绿；Linux1734/一次性PG35通过、真实模型0，镜像e938c9a3…c4bdbc8的174hash通过。隔离API已更新2.1.1/008，全部calls/builds/units/projects/publications元数据指纹更新前后一致；Brainfalse、Worker/Beat停止、main paused/syncfalse/review+publisher关闭、automation PAUSED。额外HTTP健康检查被自动审批工作区额度不足拦截，未执行，不绕过审批；恢复后先核健康再考虑授权新构建。控制secrets/brain-v211-offline-20261021，阶段报告secrets/brain-quality/32ae7934-89ff-4456-86b1-a7884ef2bcdf/。早先07:58:44Z API/PG/Redis同刻255恢复与最终500分开；现有证据不能把最终500归因关机。以下运行中文字均为历史。

2.1.0授权启动记录（历史）：用户call_2b7b2c81db71455b962ce32f87504607批准2.1.0唯一1次原SHA完整构建及独立800万Token/成功后冻结50题；已O_EXCL仅POST一次，build32ae7934-89ff-4456-86b1-a7884ef2bcdf，recipe38cbd5ff04bf537db92643eff36f27b00558161ebf28c70e2ae58c9c31049691，SHA7d89860efe1113bcb1480bac2f1aaa9b1045bd8d，historical/full。同网关deepseek-v4-flash/local_model、完整隐私、无额外模型probe/syncfalse。前轮4555276（实结4541421/unknown13855）、旧7993236及unknown653732保留，不清零。新目录secrets/brain-v210-validation-20261021显式compose.authorized+ingress-disabled，API/Worker/Beat均imagefb207f8d…efa4e6与008、cap8M，APIfalse/main paused、review/publisher关闭、旧automation PAUSED。08:09:41Z登记/08:10:08Z启动；预检172hash/186blob/旧18指纹/MAC/15历史（新extract认证）/旧账/008/GitLab GET/eth3路由通过，前台observer95550。此前基础API/PG/Redis07:58:44Z同刻退出255无OOM、自有keepalive44256已无进程；仅恢复隔离容器/新hiddenkeepalive13616（08:03:03Z），未重启共享WSL/Docker/改防火墙；原因只据现有证据保留。当前初始privacy进行中，未正式产物/50题分数。新评估gate需http_error合法链、保留费用未知、原SHA/完整MAC+历史+seal及冻结50题；不要使用旧v203门禁假定全settled。后面的停止/仅离线文字是历史时点。

最新终态（2026-10-21）：2.0.3唯一授权构建c7a72e22-3935-4dad-94b8-0c3eb57037ec已failed，06:21:42Z（北京时间14:21:42）unit8489/module_summary输出隐私扫描收到HTTP500，约43ms后错误，用户核网关确认gateway→local_model临时连接问题。初始privacy165/165、source_privacy127/127、compile127/127、初始module_summary前沿82/83；实结4143412、unknown7017、占4150429/live0，首attempt停止，无正式manifest/ZIP，50题不评分。本轮含此前404847共占4555276，余额3444724不是新调用授权；旧7993236/unknown653732保留。此前主机卡顿恢复且继续完成检查点，无电源/重启/OOM证据支持最终500是电脑关闭。阶段报告在secrets/brain-quality/c7a72e22-3935-4dad-94b8-0c3eb57037ec/。

2.1.0显式HTTP恢复已实现并部署隔离API：仅APIStatusError+完整关闭且已读取响应的429/500/502/503/504进入局部重试，PROJECT_BRAIN_MODEL_HTTP_RETRIES默认2(0..4)，2/5秒退避+抖动/数字Retry-After<=30；单元累计6个http_error为DB硬上限。SDK隐藏retry仍0，每次新UUID/group/transport_attempt和独立预留；http_error保留完整费用上界但不伪造settled/零usage，从unit live扣除而build占用不释放。真正timeout/断连/丢失仍unknown停止。输出扫描保留原语义响应在RAM、每次刷新HMAC/安全输入并绑定成功实际发送输入/UUID，全部尝试计入8/64次及480秒窗口。最终逐组连续同身份且末项settled，拒绝在途/未知/未恢复组，privacy origins额外仅settled；manifest记录费用未知上界，精确占比不可用。迁移008_brain_http四nullable字段+唯一组序号；旧unknown不回填，阶段retry遇不完整调用返回409。新配置/transport/模型实现入recipe，不用新代码续旧paid检查点。Linux1716+一次性PG schema35通过，真实模型0；API已用镜像fb207f8d…efa4e6且运行库008，旧账迁移前后指纹一致。Worker/Beat停止、入口false/main paused/syncfalse/旧automation PAUSED；未来付费运行需新构建授权并显式使用2.1.0 Worker镜像，不能用旧203 overlay直接启动旧Worker。控制目录secrets/brain-v210-offline-20261021，见docs/project-brain-http-retry.md。以下运行中或仅离线文字是早先历史时点。

project-brain/2.0.3修复捕获异常后的响应丢失处理：先锁项目/build/unit并核当前租约、转unknown，再仅按本次build/unit/lease查询未知调用；若存在则首次终止model_calls_incomplete，不self.retry，保留费用/未知额度/完成检查点，取消未完成单元。未预留或已结算后错误仍按原分类重试，其他旧租约unknown不伪造成当前失败；正常PrivacyScanDeferred/BudgetBusy仍正常续跑。真实原UUID迟到结算保留，不自动恢复failed。Brain metadata_errors增加transport_phase=connect/read/write/pool/unknown，仅有界遍历16个异常的httpx类型，不检查正文/URL/请求头；审查旧日志/实际超时参数不变。未捕获的进程丢失仍用原租约恢复，本次不保证该路径成功；最终全settled门禁不放宽。无新配置/迁移，改变配方须新构建。Linux1618+隔离PG33通过，0真实模型；2.0.3仅offline镜像sha256:7cd9a27e9ecf524576e5ea86e75a6df60ef57271e582ddb43338b85335856c69，未部署。见docs/project-brain-model-timeout-recovery.md。

2.0.2唯一授权构建786d26ab已在2026-10-20停止并隔离failed：隐私143/165，732次已结算398009、1次unknown6838、live0，总占404847，取消22；无语义/正式manifest/ZIP/50题分数。约5秒APITimeout日志两条对应同一次调用，具体传输阶段未记录，不能确诊连接/模型；所查电源窗口事件0、容器restart0/OOM false，不支持电脑关闭原因。原调用unit重试成功仍留unknown，无法通过最终封存，故停无效花费。独立800万余额7595153仅为对账，旧7993236与更早unknown653732保留，不自动授权新调用。API仍实际2.0.2/Brain false、Worker/Beat停止、写回false/项目paused/旧automation PAUSED；不混用新代码与旧检查点。阶段报告位于ignored secrets/brain-quality/786d26ab-02c0-4f79-93da-4ad599ecbab2/。ready会清理构建snapshot，后续评估须从固定Git缓存只读恢复评估专用临时快照，2.0.2 gate当前仍需适配这一生命周期，不能改用分支最新代码或让无正式产物获分。

独立测试栈为 `compose.brain-local.yml`（项目 obc-brain-local、默认端口 18080），显式使用 `.env.brain-local`，不修改生产 .env 或共享 DB/Redis。Dockerfile 的 local-test 阶段包含 pytest/tests/scripts，默认 production 不包含。`scripts/brain_local.py init --reuse-config` 只复制 GitLab 读取/评论与模型白名单配置，生成独立密码/Secret；发布默认关闭、review-worker 默认不启动。bootstrap 只接受隔离库 brain_local@postgres/obc_brain_local，建立旧表 004 基线再执行 005/006，禁止用于业务库自动 stamp。`tests/integration/` 真实 PostgreSQL 测试通过 BRAIN_LOCAL_TEST 显式开启，在线栈检查另需 BRAIN_LOCAL_STACK=1。详见 `docs/project-brain-local-test.md`；真实首建只配置同一 Webhook，不以项目 ID/API 接入为前置。

真实模型协议探测显式 BRAIN_LOCAL_REAL_MODEL=1，仅使用合成源码及现有隐私流程。Compilation.document_path 必须包含 docs/project-brain/ 前缀；Schema/提示词/重试一致，目录内自由命名。格式失败仅反馈固定错误码/规则，输出隐私扫描失败直接终止单元，不继续格式重试。

完整 tool_calls 响应仍可能包含非法 JSON 参数，不能把 finish_reason 或 strict 标记当作语法保证。JSONDecodeError/Pydantic json_invalid 单独归为 invalid_json，格式重试只反馈固定的合法 JSON 与转义规则；不回传原参数，不容错修补、不删除结论。语法诊断仅记录封闭错误类别及行/列/偏移。其他槽位、证据、路径和完整隐私门禁保持独立；修复改变配方，必须新构建，不能用新代码继续旧检查点。

WSL 重启后需核对发行版生命周期：systemd 服务本身不会保活 WSL，可能使全部容器一起退出255而没有OOM。无常驻终端时可在 Windows 用隐藏的 wsl.exe sleep infinity 保活，PID仅记入ignored secrets/brain-wsl-keepalive.json；不是定时监测，不自动启用PAUSED的观察任务。先确认Docker启动时间稳定与health，再恢复任务；不要反复重启共享WSL/Docker或给退出原因无证据定性。停止测试后仅结束记录且已核验身份/启动时间的自有保活进程。

隐私扫描诊断使用 `scripts/brain_privacy_diagnostic.py`：默认不联网；`--real` 仍需 BRAIN_LOCAL_REAL_MODEL=1，standard/repaired/parameters/parameters_next/prompt/final 仅发送合成数据，限制 18 次/50,000 Token，mixed及identity_collisions限制 2 次/10,000 Token。只导出用量、耗时、结构与识别计数，不记录正文或映射。缺响应保守计入测试预留。隐私工程回归见 test_brain_privacy_scan.py、test_brain_privacy_diagnostic.py 和真实 PostgreSQL 的 test_brain_postgres.py。protocol/pipeline 合规与 detection_ok 分开，不能把空合法 JSON 当作召回通过。

identity_collisions是独立合成名称/凭证值对照：每例独立临时HMAC账本，最多1次SDK发送，整套最多2次；只报告协议/识别/误报/路径改写的封闭计数，不回显合成名称、路径、供应商模型或未知字段。已发请求在解析/结算前异常时保留未知额度。该测试通过不证明真实指纹误报，也不授权删除指纹、豁免路径、绕过最终门禁或重新完整构建。回归见test_brain_identity_collisions_diagnostic.py；不改变生产扫描提示词或配方。

诊断的旧tp/detection_ok只表示包含关系/合成包保护，整段圈定也可能满足；精确识别需另查exact_positive_findings、overbroad_positive_findings和exact_value_detection_ok。2026-10-15已获准的2次真实对照均通过，共485 Token/unknown0，负例0发现/0路径变化，正例精确1/过宽0/2路径变化；次数已耗尽，不自动追加请求。诊断费用单独计入当轮余额而不改旧构建账，旧未知653732保留；这些数不证明真实finding误报或完整项目质量。

`scripts/brain_identity_context_diagnostic.py` 是独立的完全合成上下文/1.1.5来源链对照，默认仅零联网plan，不接受真实输入。`--real`须有本轮人工授权、BRAIN_LOCAL_REAL_MODEL=1、冻结--plan-digest及新的一次性--journal；目录须已存在且私有。最多2次SDK发送/10000 Token，每例1次，异常或非预期结果立即停止，不自动补发。O_EXCL和文件/支持平台父目录fsync在发送前持久预留，缺响应保留unknown，同日志拒绝重发；计划绑定模型别名local_model、实际网关/配方及诊断实现摘要，不保存URL/密钥。正例必须来源候选全集/实际输入/call UUID严格匹配、MAC重开有效、先持久发现再冲突且无完成收据；负例须无发现且收据完整。合成通过不复判旧finding、不授权完整重建。2026-10-16本轮已批准的2次均完成，共622 Token/unknown0，次数耗尽；与前485合计1107，本轮已结算3252836、旧unknown653732、占用3906568、余额4093432，旧构建账不变。见test_brain_identity_context_diagnostic.py与docs/project-brain-privacy-trace.md。

提取拆为静态 extract、多个 privacy 单元及 finalize，然后编译/汇总/组装。隐私按完整字段、输入及最坏输出容量分包；空数据不调用模型，超长单字段拒绝，length 局部二分且不接受残缺 JSON。每单元最多 8 次扫描/480 秒工作窗口，60 秒单调用限时；正常续跑不消耗失败重试。Worker 私有工作卷仅存 HMAC 指纹、长度/类型和扫描收据，不存原值、上下文或可逆映射；重启从固定 SHA 重建输入并跳过已完成扫描，最终重新应用全部指纹。源码事实缓存只驻留 prefork 进程内存。队列预取 1，投递数量受并发限制。

重复记录以完整文本和语义字段路径去重，机械包裹层不改变分类；不同语义字段仍重扫。预算预留取得锁后必须 populate_existing，防止 ORM 锁前旧值覆盖并发额度。Brain 禁用 SDK/客户端内层重试，使用持久任务重试并逐次预留，未知用量继续保留额度。大声明按关系分片，声明与全部关系/证据均保留；编译单元按确定包序号及哈希重建，不能只凭重复 fact_ids 找分片。

编译输入完整扫描取得收据后，只要本次消耗过扫描调用，必须在语义请求前正常续跑。下一投递重建固定输入并重放规则/HMAC，确认收据后为输出保留完整8次/480秒窗口；不是免扫。输出或格式重试必要reasoning的隐私窗口耗尽时终止单元，不能通过续跑丢弃已生成响应并隐式重生成，也不保存原响应。回归见 test_brain_compiler_privacy_window.py。来源绑定错误只记录固定类别与缺失/多余/重复计数，槽位名称、未知JSON键与值不得回显。

已有语义响应后的 BudgetBusy 必须在当前内存上下文有界异步等待，保留输出/必要reasoning/格式次数，并为下一调用保留60或120秒余量；不能重新投递并重生成。等待或格式重试窗口耗尽时终止，真正预算耗尽仍透传。纯输入和首语义预留保持原持久等待。共享预算异常位于 exceptions.py，lifecycle 保留原名导出；实现纳入配方。

PROJECT_BRAIN_PRIVACY_EXTRA_BODY 仅替换 Brain 本地扫描的扩展参数，空值继承全局；不得影响审查和语义模型。当前试点网关返回 Qwen/Qwen3.8-27B-FP8，实际合成测试只有 reasoning_effort=none 关闭了 reasoning 输出，thinking.type=disabled、chat_template_kwargs.enable_thinking=false 和顶层 enable_thinking=false 均未在该网关生效。其他网关不能照搬此结论；切换先核验 reasoning/reasoning_content 字段、finish_reason 和实际用量。配方包括扫描配置与实现，修复后需新构建，不能用新代码续跑旧配方检查点。

PROJECT_BRAIN_SEMANTIC_EXTRA_BODY 仅覆盖 Brain 语义编译，空值继承全局、非空须为 JSON 对象，纳入配方；审查与隐私模型不受影响。更换参数先在显式真实模型合成测试中确认实际 reasoning、结束原因、结构及证据。大包协议测试额外要求 BRAIN_LOCAL_LARGE_MODEL_PROBE=1，最多两次语义请求、100000 Token 硬预算，输入全部合成；普通探测仍为50000。Schema 日志仅固定类型/位置及计数，未知字段名和引用值不得回显。

编译输出Schema中的证据枚举来自当前完整包的E别名，同时用于提示词和支持strict Schema的网关；json_object网关仍需应用侧校验。真实已观察到完整合法JSON多次伪造E引用，不能仅依靠JSON语法或通用字符串类型。候选关系不得升级、不能删掉无效结论后发布；格式修复只反馈固定规则。

PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE 默认 auto。strict_tool 必须先在实际网关用合成数据核验，强制唯一 emit_project_knowledge 函数，仅读取JSON参数、不执行工具，不支持时不降级。传输Schema移除网关不支持的长度关键字，原Pydantic非空、证据枚举还原、路径和完整输出隐私校验保留；重试不回传未配对工具调用。预算与未知usage估算包括工具定义、选择和完整响应。模式纳入配方；Brain客户端仅记录异常类型/HTTP状态，不回显供应商正文。

可选 source_bound 使用同一固定函数传输，但服务先按声明的当前关系分片或汇总结论组分配 S 槽位与 source_scope，模型只填每槽 texts/unknowns，不生成或选择事实、关系、证据 ID。必须返回完整且相同的槽位集合，拒绝重复 JSON 字段、未知字段、空白解释及越界文档路径；完整输入/输出隐私校验保留。服务绑定主体证据与该分片关系证据，未知项继续保留；source_scope 仅表示解释看到的输入范围，不能当成语义支持证明。结论仍为 AI_INFERRED，semantic_support_status=unreviewed，Markdown 明示语义尚未独立核验，Flow 不复用带 source_scope 的解释为路径专属结论。API 增加可空 source_scope，旧卡片默认为 null；引用覆盖率不得宣传为业务回答准确率。模式与 source_bound.py 纳入配方，切换或修复必须新建构建，不续跑旧检查点。默认仍为 auto；先合成契约，再按本轮用户授权范围验证真实问题包与完整首建。

PROJECT_BRAIN_MAX_EXPLANATION_SLOTS 默认24、范围1..64，仅 source_bound 按固定槽位数限制声明和汇总包，与输入字节预算共同生效。超过槽位数时确定性拆分完整 items，保留全部主体、关系、候选状态与证据；绕过分包直接提交超限包时，在真实语义请求前拒绝。槽位上限纳入配方，改动后新建构建并重新测算成本；旧协议119包及其估算不能直接作为新协议容量结论。真实合成61槽失败、31槽成功只说明当前有限样本结果，不能断言失败根因是槽位数，也不能证明语义正确率。

WSL 镜像网络对外测试需管理员配置 Windows/Hyper-V 防火墙，`scripts/brain_local_network.ps1` 仅放行指定 GitLab/本机 IP 的测试端口，支持 -Remove 回收；不关闭整机防火墙。Docker bridge 可能与公司网段冲突，访问前核对 `ip route get`，优先仅为 GitLab IPv4 添加 /32 路由，不重启共享 Docker/WSL。Celery worker_init 的连接检查在 fork 前执行，worker_process_init 必须 dispose(close=False) 替换子进程同步连接池，避免继承父进程的 PostgreSQL socket；真实 prefork 回归见 test_worker_database_pool.py。

无法配置路由器时，可显式叠加 compose.brain-public.yml / tunnel profile，使用固定摘要的 Cloudflare Quick Tunnel 和 Nginx 代理。公网仅开放 GET /health 与 POST /api/v1/webhooks/gitlab，保留 API Secret 认证；不公开 Swagger/知识/审查查询。两个辅助容器没有模型/DB/GitLab 凭据或知识卷，日志不记录请求头、正文和查询参数。API 本机监听保持 127.0.0.1；公网域名在隧道重建后变化，停止时必须包含公共 overlay，不能用 remove-orphans 清理仍在使用的隧道。服务经第三方转发事件/Secret 前须确认当前用户已授权该目的地；本轮已明确授权所生成的 Cloudflare 地址。见 docs/project-brain-public-test.md。

知识成本优化使用 `privacy_projection.py` 的完整语义字段投影与 `context.py` 的无损表/别名/连续行协议。默认语义包上限为 32000 保守 UTF-8 预算单位，输出仍为 4096；不能把字节上界当供应商 Token。`planning.py` 显示输入估算及排除成本，不保证完整项目预算可容纳。规范事实/关系/候选状态与全部证据留在服务侧；仅解析器审计集合不进入语义传输。包内 E 别名必须严格还原，未知别名拒绝。

汇总输入的直接 claims[].source_scope 使用 I 引用 source_scopes 共享表，按 columns/defaults 完整恢复；历史主体/关系身份用 O、包摘要用 D，证据仍用 E。I/O/D 仅为来源范围元数据，不是当前 F/R 实体或语义支持证明；历史关系 ID 自身不携带解析状态。字段全集/严格类型、同 ID 冲突、全部引用及逆映射必须校验，不删除文本/unknowns/证据，不压缩未知包装字段；规范数据不变、逆表仅驻内存、完整隐私扫描不变。汇总调度与组装按 BrainUnit.id 读取，避免无序查询影响分包、结论顺序及首个文档路径。压缩与排序改变配方，必须新构建，禁止用新代码继续旧检查点。

`VerifiedPacket` 仅由 Worker 从已完成的隐私/finalize 检查点、源码 SHA、配方及完整哈希创建；归属、声明、关系、证据必须与规范来源匹配，未知包装字段不得免扫。新源码片段始终扫描，所有内容再应用规则/HMAC；Summary 不借事实证明跳过扫描。模型重试只保留 assistant/content 和已完整扫描的必要 reasoning 字段。构建预算暂被并行请求占用时等待，不算失败；未知调用额度保持，预算真正耗尽才终止并取消未完成租约。

隐私普通包 6000 与完整单字段硬上限 PROJECT_BRAIN_PRIVACY_MAX_FIELD_TOKENS=32000 分开：长字段独立完整扫描，超过硬上限/模型上下文或单字段输出截断仍失败。语义初始输出4096，仅 finish_reason=length 时逐步增长至 PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS=16384；每次按实际输出额度预留，不能接受残缺输出。必要 reasoning 完整扫描后回传，不截断或免扫。两个上限均纳入配方，修改后新建构建，不续跑旧检查点。

服务侧卡片、事实、符号/关系/证据索引及查询索引写入 manifest.service_files 哈希；新版本读取与导出前校验，旧清单无此字段只保持旧兼容，不宣称已认证服务文件。源码行按 CR/LF 分割，不把字符串内 Unicode 分隔符当新行。独立质量脚本 `scripts/brain_quality_audit.py` 以固定 SHA 的 AST 为真值，事实覆盖、文档定位和业务回答正确率分开统计；未人工确认的业务语义不能获得虚构分数。

分模型记账迁移007_brain_usage新增brain_model_calls与构建usage_tracking_version（旧0/新1）。调用UUID在I/O前与预算同事务预留，响应与构建总量同事务幂等结算，冲突重复拒绝；响应丢失/交接保留unknown额度，旧回复不清除新租约预算，重复结算仍受供应商超额门禁。GET builds/{build_id}/usage按角色/配置模型与阶段统计，只在全量归属且无估算/未知/活动预留时给精确占比；旧账不回填。cached属于prompt，金额无价格时null，百分比为读取时已结算值。供应商model字段仅保留配置别名匹配值，未分类值不落盘；表不含正文/源码/URL/凭据/脱敏映射。全部编译/隐私门禁仍保留；配方改变，新建构建。部署后必须确认007，启动不自动建Brain表。见docs/project-brain-model-usage.md。

缺usage时两个模型都按完整响应（含reasoning与工具参数）保守估算，不把隐私reasoning漏掉；原SHA复用superseded构建前也必须转移旧调用unknown额度。Brain扩展参数（含继承LLM_EXTRA_BODY）在I/O前解析并拒绝覆盖model/messages/输出上限/tools/response_format等正常协议字段，显式传入解析对象，保留reasoning_effort与供应商thinking扩展；旧审查客户端不改变。007更新先仅启动API并迁移，再启动Worker/Beat，禁止新Worker抢先访问未迁移表。

本机一次性PostgreSQL schema测试须在连接初始化的autocommit下SET并核验search_path，不能让rollback回退public。若Docker新容器卡created且runc child处于D状态，而既有容器exec仍正常，可在既有隔离容器独立/tmp目录复制本服务代码做模拟测试，不复制.env/试点源码，不改其运行目录；未经用户授权不得重启共享WSL/Docker，也不得把这种测试表述为已部署。

完整重建达到预算门禁后保留累计账目、unknown额度及已完成检查点，不能清零费用继续执行。单独调整PROJECT_BRAIN_MAX_BUILD_TOKENS不改变配方，同配方retry仅重置failed单元；额外模型费用须符合本轮用户明确的上限。失败构建无正式manifest时只做阶段诊断，不伪造正式产物评分。

从project-brain/1.1.0起summary.py负责确定性module_summary→project_summary归并树，每模块与项目各一个最终概览；依赖完整/哈希与半开分片重建/完整模块覆盖是组装门禁。最多24子分片、8层，完整概述仍不收敛或单条超限则失败，不丢文本。每包综合模型只有服务绑定S1，summary_synthesis scope含child_ids/module_ids及完整根来源，仍AI_INFERRED/unreviewed。完整规范包全扫，wire传子概览、概览未知与真实关系状态，用子检查点身份/hash表示来源，不重复叶级SourceRef元数据；all_unknowns完整保存/全扫，wire仅显示详情保留计数，属于语义归纳而非无损摘要。卡片claims/unknowns为概览、detail_claims/detail_unknowns为完整详情，所有引用/最终隐私/不可变哈希及独立审计覆盖两者。scope规范digest在清扫前由服务绑定，隐私不得改变身份/hash/来源；新发现可脱敏文本。预算_reserved_tokens/_unknown_tokens不属于规范输入，仅允许非负整数白名单，不可借此忽略其他未知字段。代码/常量/协议纳入配方，须新建构建，不混用旧检查点；API/用量按新阶段展示，无新增DB迁移。见docs/project-brain-hierarchy.md。

project-brain/1.1.1修复综合输入身份校验与路径脱敏副本的耦合：规范包/证据与digest不变，完整包仍扫描；仅临时副本的SourceRef.path可等于既有过滤器对原路径的精确结果，不把原路径恢复到副本或模型传输。综合wire本来就不含SourceRef路径。其他字段、字段集合、ID/SHA/行范围/哈希/关系/子来源严格比较；任意路径替换仍在语义调用前拒绝。实际试点曾因HMAC匹配两处路径触发旧校验，未发起汇总语义请求；只能据此确认工程耦合，不能断言隐私分类准确率。修复改变配方，不能重试旧构建来应用新逻辑。无新配置/迁移，见test_brain_synthesis_transport.py。

project-brain/1.1.2将已收到语义响应后的扫描次数与输入续跑分开：事实/输入/确定性组装仍用PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT=8；输出和格式重试必要reasoning共享PROJECT_BRAIN_PRIVACY_OUTPUT_MAX_CALLS_PER_UNIT=64（范围1..128）的单次投递上限。480秒总工作窗口、60秒单调用、单字段/上下文/输出容量与逐次预留预算不变；完成全部收据即停止，不是每次扫描64次。输出上限或时间耗尽仍终止单元，不保存未扫描响应、不续跑丢弃响应后隐式重生成。次数纳入配方，改变需新构建；失败容量错误只记录固定failure_code，日志只记录调用数/上限/剩余秒及原因。真实反例compile:33的有效响应需第9次扫描，前8次仅6.4秒却触发旧8次门禁，不能归因为480秒超时或模型返回非法结果。回归见test_brain_compiler_privacy_window.py。

## 工作流要求

2026-10-20用户明确批准2.0.2唯一一次新构建/独立800万Token及冻结50题评估。build786d26ab-02c0-4f79-93da-4ad599ecbab2，原SHA7d89860efe1113bcb1480bac2f1aaa9b1045bd8d，recipe382e6ec5c2d6690b234b426be0f8bd54cae68166313c6fc207e64b6583163ade，historical/full。显式叠加secrets/brain-v202-validation-20261020/compose.authorized.yml与compose.ingress-disabled.yml，API false/worker+beat true、全部cap8000000/sync false，main仍paused，旧automation PAUSED。image tag obc-brain-local:project-brain-2.0.2-verified最终ID19d49c04b8d90b65e9b1903fc2f3f749e4e9e9b5fe271806a1e7dd9e6a2161d0；离线复用旧运行时须显式PYTHONPATH=/app，防/tmp辅助脚本误读旧site-packages；临时目录导入探针+31哈希+Linux1594/PG31重新通过，实际API/worker/beat配方一致。预检186blob/bundle MAC/旧18指纹/007通过，GitLab读200；仅恢复目标/32 eth3路由，未改防火墙或重启共享Docker/WSL。O_EXCL request-attempt只POST一次；旧7993236及旧unknown653732均保留。当前前台monitor.py观察该build，不重启暂停自动化、不额外真实探测；失败停下诊断，ready后按assessment-readiness和新gate核正式产物/私有seal/history及冻结题，不用词项命中当问答分数。

project-brain/2.0.2汇总隐私分类使用独立VerifiedSummaryPacket：Worker先固定SHA/私有bundle MAC，再核finalize/privacy/source_privacy与配方，check_summary_task完整重建子树并按JSON摘要精确比较payload/result（False/0/0.0不同）。clean_summary对完整原包执行历史/规则/HMAC，再按完整安全投影提取明确Schema自由字段；机器字段仅在完整来源认证后排除重复分类，未知包装/缺字段/非法来源固定错误拒绝。模块名/title/claims/全部unknowns含all_unknowns/coverage/关系目标/display_path全扫；legacy显式display_path=null仍扫path，hash样式文字也扫。扫描后重投影原包，输入续跑、完整输出/reasoning、预算/晚发现/封存不变；汇总证明不得进入leaf摘录分支。收据仅同构建同配方完整text+role复用，命中仍重放HMAC。两真实历史输入零模型回放1892/1990→284/282分类记录，规范/wire哈希、来源/关系/unknowns不变；不等于供应商Token节省或业务质量。无新增配置/迁移，需新配方新构建，不能续e77旧检查点或清账。旧2.0.1镜像/入口保留禁用，Worker/Beat停止、automation仍PAUSED；本轮未授权新真实模型调用。见docs/project-brain-summary-privacy.md。

2026-10-20最新终态：2.0.1 e77d3b87于北京时间15:11:16预算失败，实际7465151/unknown0/live0（4049隐私+231语义调用，全部settled）；同轮旧528085保留，共7993236/800万，剩6764非新授权。165privacy/127source_privacy/127compile全部完成，module_summary53/81；1个BudgetExhausted、27个随终止取消。12历史继承生效，201安全展示路径旧已知值0，未复现旧遗忘；无正式manifest/ZIP，不评50题。隐私模型62.62%、语义37.38%；模块汇总4401295 Token。真实两包零模型重建认证：规范隐私输入412941/430095预算单位，wire31085/30113，约1900记录；不得把单位当供应商Token、按ID外形免扫或截断事实来降本。后续优先认证组件收据/完整语义投影及双容量规划；协议变化必须新建，同配方追加额度才可续保留结果。旧账不清零。电脑相关指定System事件窗口0、容器restart0/OOM false，直接失败是预算。观察脚本曾单次docker exec超时退出，现仅ignored monitor.py捕获恢复并合成回归通过；构建独立继续。专用Worker/Beat已停止，API false、sync false、main paused，旧automation仍PAUSED；未经新明确费用授权不retry/新建/真实探测。阶段报告在secrets/brain-quality/e77d3b87-7557-4e44-99d2-25c76d4728b3/。

2026-10-20用户已追加明确授权：使用2.0.0轮剩余7471915 Token启动唯一1次2.0.1完整构建，已提交e77d3b87-7557-4e44-99d2-25c76d4728b3，原SHA7d89860efe1113bcb1480bac2f1aaa9b1045bd8d，配方086cf07d18585e178ecc929913c4bf806a66e9ba8cdbd3c6591806e6dddb6de8，历史/full。旧528085及旧轮unknown均不清零，本轮合计仍800万。显式叠加secrets/brain-v201-validation-20261020/compose.authorized.yml及compose.ingress-disabled.yml：API Brain false（阻止额外新建），Worker/Beat true，三者cap7471915/sync false；本机生产.env不改，review/publisher关闭，main仍paused，旧定时观察PAUSED。33实现文件/007/186blob/身份锚点/冻结50题哈希开跑前核验；历史继承12非空快照已登记。一次性O_EXCL请求记录在该目录request-attempt.json，不重复POST；当前对话只读观察，ready后完整哈希/私有seal/原SHA及冻结50题评估，失败停下诊断，不自动再建/真实探测/扩大额度。

project-brain/2.0.1已修复跨构建遗忘：privacy_history.py由DB选择同GitLab项目已完成extract的私有历史，首次extract冻结HMAC快照，后续核对privacy_history_digest；当前完整记录实际匹配才按新key登记inherited_hmac_match，规则发现登记deterministic_rule，私有v3来源不伪造模型call/首次发现、不保存原值。继承先于收据/cache/local/cloud发送，输出和reasoning也覆盖。新Worker用require_index在新key同锁写认证空index，重开/refresh缺失即失败；旧2.0.0 open缺index无法区分空/丢失，保守拒绝。已存在audit或Schema2 ready必须认证key/index/state/seal及revision，快照保存并重验四件套，不降级work；更早版本无私有材料明确legacy_unavailable。source-history与source-identities/seals/audits独立私有备份、不随10版产物清理；256父快照/128MiB上限fail-closed，未来需认证压实，不承诺无限历史或全局即时撤回。审计增加--privacy-history-base，2.0.1正式清单绑定历史digest；隔离失败及配方改变的retry409。无新变量/迁移，必须新配方新构建。2026-10-20最终镜像1524单元/隔离PG31通过，两种完整模拟链ready；原SHA按18历史构建/12非空快照零模型回放，4敏感路径81refs安全遮蔽、1999事实/8405关系/14282来源完整。已部署隔离API（健康200/007），Brain false、Worker/Beat停止、旧监测PAUSED、写回关闭；da043b84仍failed，费用528085/unknown0/semantic0/publication0，余7471915非第二次授权，未生成新正式试点产物/50题评分。见docs/project-brain-privacy-inheritance.md及敏感来源验收记录。

2026-10-20最新：上述da043b84完整构建被独立安全验收隔离，known_privacy_regression。首轮165privacy完成但旧v2已认证的路径同值凭证未进入新HMAC，安全facts展示路径仍有4处；source_privacy完成104/127时停止专属Worker/Beat，DB failed/余23取消。已结算528085、unknown0、semantic0、publication0，无正式产物/50题评分。原800万本次余7471915只是账面余额，单次授权已消耗，不自动重建。API恢复ignored compose.disabled.yml（Brain false），旧监测仍PAUSED。零模型重建证实凭证仍在规则预处理后待扫描记录中、规则未识别、新索引无对应发现；是此轮检测链没复现旧发现，不能靠反复重扫或删除旧HMAC。接下来离线实现已认证历史发现的HMAC继承，并区分非模型来源，不伪造SDK调用/旧finding因果；新配方不续旧检查点。详见docs/project-brain-sensitive-source-validation.md。

2026-10-20本机2.0.0已部署，Linux1489单元/隔离PG31通过，原SHA零模型静态回放通过4敏感路径81refs的旧失败点，但不证明真实生成或50题。用户新授权唯一一次独立800万Token完整构建及评估，build da043b84-912d-43ef-b8d1-fa54f87d53bf、recipe f81ad9c7f436e5855f827b70800cf24873ca92bf96fb702e1e34e1d57a902425、原SHA7d89860efe1113bcb1480bac2f1aaa9b1045bd8d。须显式叠加ignored secrets/brain-v2-validation-20261019/compose.authorized.yml，800万含全部模型/重试，禁止新探测/额外构建、旧账不清零；历史构建，main仍paused，GitLab同步/review/publisher关闭。旧观察automation仍PAUSED，不自动恢复，当前对话前台观察；失败先诊断，不擅自再建。WSL只补GitLab IPv4 /32路由绕docker0，未改防火墙/重启共享服务。详见docs/project-brain-sensitive-source-validation.md。

project-brain/2.0.0支持原SHA含硬编码凭证及同值路径：SourceCatalog仅Worker从固定Git事实重建私有RAM索引，项目HMAC身份位于独立source-identities区，带key锚点及已完成extract登记，丢失整个已登记目录也不能自动轮换。公开Schema2与规范FactBundle分离，来源path为opaque locator、display_path仅展示，敏感路径source_url=null；不恢复原值或输出编码假链接。初始privacy包含全部计划excerpt，source_privacy屏障后才编译；规范MAC/digest保持，临时安全投影在发送前刷新，完整输入/输出门禁不变。最终公开投影覆盖卡片/详情/unknowns/图/元数据/API/ZIP，ZIP显式清单；API用DBmanifest哈希认证且不回显原分支、自由错误或无法认证的MR URL。组装前与ready事务核调用全部settled、无活动/unknown预留和其他未完成单元，否则model_calls_incomplete；封存后新发现privacy_seal_conflict使版本失效、清当前指针/禁发布，显式新请求须独立generation，旧账不清零。source-seals及source-audits/build/privacy四文件认证材料独立于work清理，仅Worker可读，不含原值/源码/可逆映射；需单独备份，当前不自动清理/轮换。新schema不能交FactExtractor._restore；2.0先全量静态重建、同构建检查点恢复，暂不跨构建复用语义缓存。独立审计新增--source-identity-key，仅本机/Worker读取，缺密钥/账本拒绝评分，不用显示路径做真值。无新配置/迁移，必须新配方新构建；尚未真实部署/重建时不得称试点通过。详见docs/project-brain-sensitive-source-design.md。

project-brain/1.1.5在Worker私有fingerprints.json用version2原子保存entries+候选origins+整包index_hmac；仅有call/build/unit/stage、封闭字段类别、keyed输入/记录/字段指纹及字符位置计数，无原值/路径/源码/字段名/响应。候选位置只证明实际发送safe records中出现该值，不证明分类因果；旧flat v1来源legacy_unknown且不伪造回填。Worker核验完整MAC，审计纯parser只验证结构，完整旧版本回滚不在保证内。fsync文件/支持平台父目录后才写完成收据；extract可建key，后续模型阶段create=False，缺目录/key或坏长度privacy_checkpoint_missing终止，不创造空隐私历史。

2026-10-16试点1.1.5的v2记录已用固定SHA/整组实际输入HMAC和AST重建：source_excerpt同值出现在环境读取第二默认参数，一处为明确凭证配置，同时命中4源码路径/81条来源，现有身份门禁正确终止。不能仅看os.getenv/os.environ.get调用名就称配置键或误报，必须区分第一参数键与第二参数默认值，并核对标准库绑定/遮蔽。静态凭证上下文不证明值有效或部署使用，不能回填旧v1首次发现。用户10/19明确必须支持原提交，不再以维护者移除硬编码或换SHA为前置；采用私有规范身份/安全公开投影设计，详见docs/project-brain-sensitive-source-design.md。不删除HMAC、豁免路径或在链接中恢复原值。早期设计核心原型13项通过、真实模型0次；现2.0任务链实现与验证状态见上段，部署前旧1.1.5门禁不放宽。该次唯一构建已耗554245、unknown0，轮余3539187仅是账面余额，不是新调用授权；无正式产物则50题不评分。

1.1.5在每次clean/cache/收据入口、新发现持久后、语义/格式重试/预算等待后的预留前检查完整files/modules/refs/observations身份，冲突固定source_identity_privacy_conflict并终止不重试；已用/unknown保留。成功检查只可同unit按完整身份+entries+已知过滤器规则/动态值/映射/实现状态缓存，未知状态全扫；同count替换必须重检，最终完整门禁不删。FS/DB/网络非原子事务，在途请求仍可能计费；不能宣称全局即时零调用。无需新配置/迁移，privacy_provenance.py和privacy_identity.py纳入配方，必须新建构建、不续旧检查点。见docs/project-brain-privacy-trace.md。

project-brain/1.1.4修复晚发现隐私规则与综合结果认证的耦合：summary envelope不再重复存派生all_unknowns；_checked_unit先严格校验整个envelope，再从规范payload继承未知+已扫描Compilation.unknowns派生内存集合。Worker不得对认证envelope整体sanitize改写身份/哈希；Compilation本身完整输入/输出隐私、后续规范包全扫和最终卡片HMAC不变，未知项不删除，额外all_unknowns副本仍拒绝。实际1.1.3反例5739仅all_unknowns被晚HMAC改写，其payload/hash匹配，5735检查兄弟时失败；不是电脑关闭导致。修复改变配方，不续旧构建/清零费用。见test_brain_summary_inherited_unknowns.py。

project-brain/1.1.3将前沿规划拆为独立planning租约单元：付费输出先持久化，完成事务仅检查SQL前沿；规划读取一致快照后关闭事务，在锁外重建完整SHA/配方/事实/子检查点，再用DB依赖签名和租约校验提交下一层。规划失败不得回滚合规子输出；同配方重试规划不再次调用模型。单次_evaluate的索引/缓存不跨调用，仍执行原关系OR谓词和完整规范哈希，不能按fact_id或unit.key复用未认证输入。

1.1.3综合关系视图区分module/level0的/2 symbol_graph与模块高层、项目所有层的/3 resolution_counts。后者按type/resolution/knowledge_type精确计数，完整规范关系/证据/详情仍保留并全扫；扫描前关系摘要由服务绑定。统计不带端点/目标名/归属，不证明模块依赖或调用路径，不能把它宣传为无损图或语义支持。scope.relationship_view可空兼容旧卡，新任务必须精确匹配阶段；API/Markdown/审计显示视图。大图完整树的模拟收敛不能替代真实生成或50题业务评估。此变更无配置/迁移，改变配方，不能继续旧检查点。见docs/project-brain-hierarchy.md。

**每次会话开始时**：
1. 阅读本文件 (AGENTS.md) — 了解项目架构、约定和最新配置
2. 阅读 `memory/MEMORY.md`（如果存在）— 了解稳定模式和已知注意事项
3. 查看 `git status` — 了解当前工作状态

**每次功能修改完成后**，检查以下文件是否需要同步更新：
- **AGENTS.md** — 新增模块/配置/端点/流程变更时必须更新
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

以下审查后 README 流程适用于 `PROJECT_BRAIN_ENABLED=false` 的旧模式。Brain 模式的自动维护、队列和发布身份以文末 Project Brain 节为准。

**请求流**: GitLab Webhook → FastAPI (`webhooks.py`) → Celery task (`review_task.py`) → ReviewEngine → [影响面预分析 → 隐私预扫描 → 多维度Agent并行审查 → 结果反脱敏] → DB 持久化 → GitLab comment → （可选，仅 MR merge 到主分支 或 push 到主分支）触发 `generate_readme` 任务

**文档生成流**: 仅当 `(trigger_type=push 且 branch ∈ MAIN_BRANCHES)` 或 `(trigger_type=merge_request 且 mr_action=merge 且 branch ∈ MAIN_BRANCHES)` 的审查成功时 → `generate_readme` (Celery, queue=readme) → ReadmeGenerator → [Agent 工具循环或增量更新] → 按 trigger_type 分支提交：push 直接 `create_commit` 到主分支；merge 探测主分支 `protected` 字段，未保护走直接 commit，受保护走"临时分支 → commit → MR → 自动合并"绕过 → DB 记录

**重复触发三层防御**:
1. **Webhook 层**（`src/api/v1/webhooks.py`）：
   - MR 事件仅 `action == "merge"` 触发（open/update/reopen 完全 ignored）
   - MR merge 事件分发 `run_review` 后写 Redis 标记 `mr_merge:{project_id}:{commit_sha}` TTL 300s；紧随的目标分支 push 事件命中此标记直接 ignored，保证"merge 仅触发一次"
   - AI-README 临时分支（前缀 `README_TEMP_BRANCH_PREFIX`）的 push 事件 ignored，防止自动文档 commit 递归触发
   - AI-README 自动 MR（source_branch 前缀或 title 前缀命中 `README_TEMP_BRANCH_PREFIX` / `README_MR_TITLE_PREFIX`）ignored
   - Push 事件中所有 commits 均以 `[AI-README]` 开头时 ignored（兜底）
2. **Task 层**：`run_review` 入口 `_find_recent_review` 按 (project_id, commit_sha) 查询近 30 分钟内是否已有 in_progress/completed 审查，命中则跳过；`_should_dispatch_readme(trigger_type, mr_action, branch)` 防御性校验 README 分发条件；`generate_readme` 入口 `_has_completed_readme` 按 (project_id, branch, commit_sha, status='completed') 查询是否已生成
3. **DB 层**：`readme_records` 表 partial unique index `(gitlab_project_id, branch, commit_sha) WHERE status='completed'`；并发冲突时 `IntegrityError` 降级为 info 日志，不触发 Celery 重试

所有去重查询均 fail-open（Redis/DB 故障时放行），由下游层级兜底。

### Core Pipeline (src/tasks/review_task.py)

0. **入口去重**: `_find_recent_review` 查询 (project_id, commit_sha) 近 30 分钟内是否已有 in_progress/completed 审查，命中则直接返回 `{"status":"skipped","reason":"duplicate"}`，不再创建新的 Review 记录
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
12. **文档生成触发**（可选）: `README_GENERATION_ENABLED=true` 且 branch ∈ `MAIN_BRANCHES` 且 `(trigger_type=push 或 (trigger_type=merge_request 且 mr_action=merge))` 时，审查成功后链式分发 `generate_readme.delay(trigger_type=..., ...)` 到 readme 队列；文档任务根据 `trigger_type` 选择提交路径：push → 直接 commit（成功 push 本身意味着主分支未保护）；merge → 探测主分支 `protected` 字段，受保护走"临时分支 → create_commit → create_merge_request → (可选) accept_merge_request 自动合并"，未保护走直接 commit；失败不影响审查流程

### Key Modules

- **src/core/review_engine.py** — Agent 审查引擎：`ReviewEngine` 总控（单/多Agent模式切换）、`DimensionAgent` 维度Agent（独立 tool-calling 循环 + 本地 TokenUsage 累加）、结果解析/去重/加权评分、分维度+分模型 token 统计、隐私反脱敏、双格式 GitLab 评论；`_parse_changed_lines` 解析 diff hunk 变更行范围、`_ranges_overlap` 区间重叠检测，用于精确化影响面预注入
- **src/core/agent_tools.py** — 6 个沙箱工具（read_file, search_code, list_directory, find_definition, get_file_info, get_blast_radius），`AgentToolExecutor` 基类做路径越界检查和输出截断（20000 字符 / 500 行上限）；`find_definition` 支持 10+ 语言的定义正则匹配；`get_blast_radius` 利用代码图分析变更影响面（上游调用者/下游被调用者）
- **src/core/code_graph/** — 代码图模块（影响面分析）：`parser.py` 多语言 AST/正则解析器（Python/JS/TS/Java/Go）；`store.py` SQLite 持久化存储 + FTS5 全文检索；`query.py` `CodeGraph` 高层查询 API（影响面 BFS 追溯），`get_blast_radius` 全文件分析 + `get_blast_radius_for_nodes` 指定节点精确分析；`incremental.py` 基于 git diff 的增量更新（首次全量扫描，后续仅处理变更文件）
- **src/core/llm_client.py** — AsyncOpenAI SDK 封装，连接 vLLM (OpenAI compatible API)；`TokenUsage` dataclass（prompt/completion/cached tokens + `__iadd__`）、`ChatResult` dataclass（message + usage）；`chat()` 返回 `ChatResult` 支持逐次 token 统计；内置指数退避重试（RateLimitError/APIConnectionError/5xx 可重试）；集成上下文裁剪（`trim_messages` 前置调用）和隐私过滤（`sanitize_messages` 发送前脱敏）
- **src/core/readme_generator.py** — AI_README.md 文档自动生成引擎：`ReadmeGenerator` 类支持两种模式（首次生成用 Agent 工具调用循环，增量更新用单次 LLM 调用）；结构指纹计算（SHA-256）跳过无变化项目；复用 `AgentToolExecutor` 的 5 个只读工具；`FileAction`/`ReadmeResult` 数据结构；**根目录增量更新采用子目录摘要的增量语义**——`_collect_root_info` 仅把本次变化子目录的 `FileAction` 注入 `subdir_docs` 章节，未出现的子目录通过 prompt 显式指令（`_ROOT_SYSTEM_PROMPT_UPDATE` 硬规则 + blockquote 前导文案）引导 LLM 原样保留现有文档描述，避免"缺失即删除"误判
- **src/core/gitlab_client.py** — httpx async 客户端，11 个 GitLab API 方法（`get_project`, `get_mr_changes`, `get_compare`, `post_mr_note`, `post_commit_comment`, `create_commit`, `file_exists`, `get_branch`, `create_branch`, `delete_branch`, `create_merge_request`, `accept_merge_request`）；分支/MR 管理方法用于受保护主分支绕过：`create_branch` 幂等（400 already exists → 返回空 dict），`delete_branch` 非致命（404/网络错误 warning），`accept_merge_request` 处理 405/406/409（pipeline/方法/冲突）降级 warning
- **src/core/repo_manager.py** — Git 仓库生命周期管理，oauth2 token 注入 clone URL
- **src/core/privacy_filter.py** — 隐私过滤核心：`SanitizationMapping` 双向映射（original ↔ `<<TYPE_N>>` 占位符）、`PrivacyFilter` 3阶段过滤（`pre_scan` diff预扫描 → `scan_tool_results` 工具结果扫描 → `desanitize_text` 反脱敏）、规则引擎 + 本地LLM混合扫描、`token_usage` 属性跟踪本地模型 token 消耗
- **src/core/privacy_rules.py** — 敏感数据检测规则库：7类规则（KEY/SECRET/IP/DOMAIN/EMAIL/CERT/CUSTOM）、3个检测等级（minimal仅密钥密码/standard+IP邮箱/strict+高熵字符串）
- **src/core/dimensions.py** — 多维度审查定义：5个 `ReviewDimension`（bug 30%/security 25%/performance 15%/quality 15%/architecture 15%）、维度专属 System Prompt、加权评分算法（任一维度D或存在critical → 最终评级 ≤ C）
- **src/core/context_manager.py** — 上下文长度管理：Token 字符估算、4阶段消息裁剪（工具结果截断2000字符 → 参数感知摘要替换 → 历史工具组移除 → Diff截断60%预算），保护 system+user 头部和尾部4条消息
- **src/utils/security.py** — Webhook Token 验证（`verify_gitlab_token`）+ API Key 认证（`require_api_key`），未配置时默认放行（开发模式）
- **src/utils/rate_limit.py** — Redis 固定窗口速率限制器，`rate_limit_dependency()` 工厂函数返回 FastAPI 依赖；Redis 故障时 fail-open
- **src/utils/ip_whitelist.py** — `IPWhitelistMiddleware` IP白名单中间件，支持IPv4/IPv6 CIDR，`/health` 始终放行，未配置时全部放行（开发模式）
- **src/tasks/readme_task.py** — Celery 任务：`generate_readme` 文档生成主入口（签名含 `trigger_type`），编排 ensure_repo → ReadmeGenerator.run() → 按 trigger_type 分支提交路径：push 直接 `create_commit`，merge 探测主分支 `protected` 后选择直接 commit 或 `_submit_via_temp_branch_mr`（临时分支 → commit → MR → auto-merge） → DB 持久化；入口 `_has_completed_readme` 幂等预检避免重复生成；`_save_readme_record` 捕获 `IntegrityError` 降级为 info
- **src/utils/redis_client.py** — 同步 Redis 客户端惰性单例，供 rate_limit 与 webhook 抑制标记共享，故障由调用方 fail-open 处理
- **src/models/readme_record.py** — ORM 模型：`readme_records` 表，追踪文档生成历史和结构指纹
- **src/api/v1/reviews.py** — 审查记录查询 API（分页列表 + 详情），路由 `/api/v1/reviews`
- **src/schemas/webhook.py** — Webhook 请求体 Pydantic schema（`MergeRequestEvent`, `PushEvent`）
- **src/api/router.py** — API 路由聚合，前缀 `/api/v1`

### Webhook Trigger Logic

- **MR 事件**: `object_kind == "merge_request"` && `action == "merge"` && target_branch 在 MAIN_BRANCHES 中 && source_branch **不**以 `README_TEMP_BRANCH_PREFIX` 开头 && title **不**以 `README_MR_TITLE_PREFIX` 开头
  - `open/update/reopen`: **不再触发任何任务**（精简为"merge 仅触发一次"的严格语义）
  - `merge`: 审查合并后代码，结果写入 MR 评论；**触发 README 生成**（trigger_type=merge_request）
  - `merge` 额外行为：`_mark_mr_merge(project_id, commit_sha)` 写 Redis 标记（TTL 300s），抑制 GitLab 同步发送的目标分支 push 事件
  - AI-README 自动 MR（source_branch 或 title 命中前缀）：直接 ignored，防止递归
- **Push 事件**: `object_kind == "push"` && before 非全零 && 有 commits && branch **不**以 `README_TEMP_BRANCH_PREFIX` 开头
  - **AI-README 临时分支**: branch 以 `ai-readme/` 前缀开头时直接 ignored（受保护分支绕过产生的 push 事件）
  - **AI-README commit 防递归**: 所有 commit message 均以 `[AI-README]` 开头时跳过
  - **MR merge 抑制**: `_is_mr_merge_dispatched(project_id, after_sha)` 命中 Redis 标记则跳过，返回 `suppressed by MR merge event`；Redis 故障时 fail-open
  - **README 触发**: branch ∈ MAIN_BRANCHES 且审查成功 → 链式分发 README（trigger_type=push，直接 commit）

### 提交人邮件通知

**push 链路**：`_post_comment_async` 在 commit comment 正文开头插入 `@{user_username}\n\n`，借助 GitLab mention 机制把审查结果以邮件形式投递给 push 触发者（GitLab 默认不会主动通知 commit 作者）。`user_username` 来自 push webhook payload 顶层字段，由 `_handle_push` 透传到 `run_review.delay(author_username=...)`，再传至 `_post_comment_async`。该字段缺失时降级为普通 commit comment（不阻塞流程，warning 记录"push 评论无 author_username，跳过 @mention（fail-open）"）。

**MR 链路**：`post_mr_note` 不注入 @mention，依赖 GitLab 原生 MR 评论邮件通知机制（自动通知 author/assignee/reviewer/参与者），避免重复邮件。

**README 链路**：README 任务（`src/tasks/readme_task.py`）只调用 `create_commit` / `create_branch` / `create_merge_request` / `accept_merge_request` 等内容 API，**不调用任何评论 API**，自然不会触发 GitLab @mention 邮件——"README 不发邮件"是结构上的不变量。任何把 README 结果通过评论汇报的需求都需重新评估邮件触发面。

### 受保护主分支处理（仅 merge 链路）

push 到主分支能成功本身即意味着主分支未保护，因此 push 链路无需探测；仅 **merge 链路**需要此流程：

1. `GitLabClient.get_branch(project_id, main_branch)` 获取 `protected` 字段
2. 未保护（`protected=False`）：直接 `create_commit` 到主分支
3. 受保护（`protected=True`）：走 `_submit_via_temp_branch_mr` 流程：
   - `create_branch(ai-readme/{ts}-{sha8}, ref=main_branch)` — 时间戳避免同 sha 重试冲突
   - `create_commit` 到临时分支；失败时 `delete_branch` 清理残留后抛出
   - `create_merge_request(source=temp, target=main_branch, title="[AI-README] 更新 AI_README ({sha8})")`
   - `README_MR_AUTO_MERGE=true` 时调用 `accept_merge_request`（`should_remove_source_branch=README_MR_AUTO_MERGE_REMOVE_BRANCH`），失败降级 warning 人工兜底
4. 整条链路由 webhook 层 AI-README 前缀过滤防止递归触发审查

### Database

PostgreSQL + SQLAlchemy 双引擎设计。三张表：`reviews`（审查记录）1:N `review_comments`（问题明细）、`readme_records`（文档生成历史）。启动时自动建表（`main.py` lifespan）。

- FastAPI 使用异步引擎（`asyncpg`）
- Celery Worker 使用同步引擎（`psycopg2`），通过 `DATABASE_URL_SYNC` property 自动转换
- `review_comments` 表新增 `dimension` 字段（String(50), nullable），记录问题所属审查维度
- `reviews` 表新增 `total_prompt_tokens`、`total_completion_tokens`、`total_cached_tokens` 字段（Integer, default=0），持久化 token 统计
- `readme_records` 表：追踪文档生成历史（project_id, branch, commit_sha, fingerprint, status, token 统计等）
- `readme_records` **partial unique index** `ix_readme_records_unique_completed`：`(gitlab_project_id, branch, commit_sha) WHERE status='completed'`，保证同一提交仅有一条成功记录（失败记录不受约束，便于排查）；并发冲突时 `IntegrityError` 降级为 info
- Alembic 迁移: `001_add_dimension_to_review_comments.py`、`002_add_token_columns_to_reviews.py`、`003_add_readme_records.py`、`004_add_readme_records_unique.py`

### API Endpoints

- `GET /health` — 健康检查
- `POST /api/v1/webhooks/gitlab` — GitLab Webhook 接收端点（Token 验证 via `verify_gitlab_token`，速率限制 30/min）
- `GET /api/v1/reviews` — 审查记录列表（分页、按 project_id/status 过滤，需 API Key，速率限制 60/min）
- `GET /api/v1/reviews/{review_id}` — 审查详情（含关联 comments，需 API Key，速率限制 60/min）

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
- SonarQube 尚未与应用代码集成，`.env` 中有 `SONARQUBE_URL`/`SONARQUBE_TOKEN` 预留配置项
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
- **消息无损归一化**: 任何 pydantic `ChatCompletionMessage` 入消息列表前都经 `context_manager.normalize_message()` 统一转 dict（`model_dump(exclude_none=True)`），无损保留 `reasoning_content`（DeepSeek thinking）、`thinking`（Codex extended thinking）等模型特定字段；`_parse_with_retry` 重试时保留原始 response 而非仅 content，确保"需要把字段回传给下一轮"的模型（如 DeepSeek v3.2+）不会 400
- **模型特性透传**: `LLM_EXTRA_BODY`（JSON 字符串）作为 OpenAI SDK 的 `extra_body` 下发，用于切换模型时主动控制 thinking/reasoning_effort 等参数，避免每换一个模型就改代码
- **结构化输出（response_format）**: `chat()` 内部按 `settings.response_format_mode_for(model)` 自动决策——三档模式 `json_schema` / `json_object` / `off`，工具循环阶段（`tools` 非空）一律不挂；`tools=None` 终局调用按配置注入。可选传 `response_format=None` 显式禁用（README 等 Markdown 输出场景使用），或传 dict 显式覆盖。`LLM_RESPONSE_FORMAT_MODE_CLOUD` / `LLM_RESPONSE_FORMAT_MODE_LOCAL` 可分别覆盖云端/本地模型；DeepSeek 系仅支持 `json_object`，本地 vLLM 0.8.5+ / Qwen 系两档都支持
- **schema 构造与重试**: 多维度 Agent 通过 `build_dimension_json_schema(dim)`、单 Agent 通过 `build_single_agent_json_schema()` 构造 OpenAI strict 兼容 schema（`additionalProperties:false`、required 全集、可空字段用 `["integer","null"]` union）；格式重试全程透传 schema_builder，落盘文件名加入 `rf_mode` 段（`{rid}_{dim}_a{attempt}_{rf_mode}_{error_type}_{ts}.txt`）便于关联分析
- **能力探测**: 切换 endpoint/网关/模型时，先用最小请求探测当前 endpoint 对各模式的支持情况，再设置 `LLM_RESPONSE_FORMAT_MODE_*`；探测建议：云端用 DeepSeek 系确认 `json_object` 可用，本地用 vLLM 0.8.5+/Qwen 系尝试 `json_schema`

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
| `README_TEMP_BRANCH_PREFIX` | `ai-readme/` | 受保护主分支绕过时的临时分支前缀（webhook 据此识别并 ignored） |
| `README_MR_TITLE_PREFIX` | `[AI-README]` | 自动 MR 标题前缀（webhook 据此识别并 ignored） |
| `README_MR_AUTO_MERGE` | `true` | 受保护分支场景下是否自动合并临时分支 MR |
| `README_MR_AUTO_MERGE_REMOVE_BRANCH` | `true` | 自动合并后是否删除临时源分支 |
| `README_MR_DESCRIPTION` | `Automated AI_README.md update...` | 自动 MR 的描述文案 |

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
| `LLM_EXTRA_BODY` | `""` | OpenAI SDK `extra_body` JSON 透传（thinking/reasoning_effort 等） |

### LLM 结构化输出（response_format）
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `LLM_RESPONSE_FORMAT_MODE` | `json_object` | 全局模式: `json_schema` / `json_object` / `off` |
| `LLM_RESPONSE_FORMAT_MODE_CLOUD` | `""` | 云端模型独立覆盖（空 = 跟随全局）；DeepSeek 仅支持 `json_object` |
| `LLM_RESPONSE_FORMAT_MODE_LOCAL` | `""` | 本地 vLLM 独立覆盖（空 = 跟随全局）；vLLM 0.8.5+/Qwen 系可设 `json_schema` |

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

## Project Brain v1（新增）

**功能边界**：通过现有 LLMClient 生成结构化项目知识，JSON 为规范数据、Markdown 确定性渲染；不调用 Codex CLI、不执行项目代码，OpenViking 后置。默认 `PROJECT_BRAIN_ENABLED=false`。

**知识请求流**：与审查共用认证 Webhook，MAIN_BRANCHES 的 push / MR merge 自动登记首建或更新 → DB 持久化事件（早于审查去重）→ Brain Worker 独立 brain 队列 → 固定目标 HEAD 的无凭据快照 → 提取/语义编译/证据校验 → 不可变产物 → 独立 brain-publisher 的 brain-publication 队列发布。创建分支/空 commits 有有效 HEAD 也可登记；删除/404 标记 missing 并保留版本。自动接入要求非空 GITLAB_WEBHOOK_SECRET，登记失败返回 503。

**维护与调度**：unmanaged（仅历史归档）/ active / paused；事件可登记 unmanaged，不能恢复 paused。event_seq 标记事件，request_seq 只在调度确认不同输入时推进，重复 merge/push 不中断相同输入租约。清理 dirty 必须比较观察到的 event_seq，晚完成依 request_seq/状态/租约令牌防护。Beat 15 秒恢复、事件合并 60 秒最长 300 秒、PROJECT_BRAIN_RECONCILE_SECONDS=300 周期核对 managed 目标以补偿漏发。源码指纹包括 path/blob/mode；相同输入且祖先/产物有效可复用 ready。

**新增模块**：`src/core/project_brain/` 包含 snapshot、schema、extractor、compiler、artifacts、lifecycle、publisher、webhook；`src/tasks/brain_task.py` 分阶段检查点及保留策略；`brain_publication_task.py` 发布租约/对账；`src/api/v1/brain.py` 接入与查询。源码缓存与审查目录隔离，RepoManager.ensure_snapshot 接受完整 SHA。知识事实图不能使用现有码图的 dst_name 猜测已解析关系。

**数据库**：四张 Brain 表由 `005_project_brain` 创建；新增 `006_brain_auto`（前版 005）维护状态、event_seq、source_status、观察 HEAD/时间、发布 error_code/policy_version。005 冻结，不随 ORM 更改。006 将旧关闭正常维护记录迁为 paused、仅历史为 unmanaged。启动 create_all 只创建原三张表。

同输入保留产物可复用；产物已清理/损坏时创建下一 generation、新 build_id/版本，保留旧历史元数据。唯一约束含 generation。存在多代重建时 006 不允许自动降回 005 的旧唯一约束，回滚应用默认保留新增表/卷。

临时快照/检查点位于 PROJECT_BRAIN_WORK_DIR 的独立 brain_work_data 卷，仅 Brain Worker 挂载；API/Publisher 的 brain_data 不得包含临时源码。未配置 WORK_DIR 的单进程模式回退 BASE_DIR/_work。

**API**：统一 `/api/v1/projects/{project_id}/brain`。POST `/builds` 返回 202；GET 根（branch 查询）、`/builds`、`/builds/{id}`、`/builds/{id}/cards`、`/builds/{id}/cards/{card_id}?format=json|markdown`、`/builds/{id}/export`；PATCH 根配置维护与同步；POST `/builds/{id}/retry` 和 `/builds/{id}/publication/retry`。复用 API Key / IP 白名单；读 60/min、写/重试 5/min。不接受客户端仓库 URL 或文件路径。

**旧文档接管**：Brain 模式关闭所有旧 README 分发/生成/最终提交，包括已经生成的任务；paused 不恢复 README。关闭 Brain 并显式启用 README 才恢复旧模式，须同时恢复旧写入身份权限。AI_README 与历史记录保留。enrolled 依维护状态判断，首次登记后立即生效，不依赖已生成版本。

**发布与安全**：授权仅根 PROJECT_BRAIN.md 和 docs/project-brain/ 的安全相对后代，不按扩展名、文件名或子目录硬编码授权。Compilation.document_path 可建议模块布局，稳定卡片 ID 与路径独立，manifest.card_paths/index 保存映射，协议 manifest 路径保留。发布加载不可变产物并核对 DB manifest 哈希与项目/分支/构建；目录内人工文件不覆盖，只更新/删除旧清单拥有且哈希匹配的文件，使用 last_commit_id。write_policy.py 验证目标树、实际 commit diff/完整产物、整个 MR/源 SHA；禁止链接、子模块、模式修改和重命名动作。临时分支未保护时保留 MR 人工合并。通用 brain_request 仅 GET；旧 README 最终入口在 Brain 模式关闭。机器人豁免需 DB 发布、远端 manifest 哈希/内容和仅知识路径差异。Brain 无评论、无反脱敏。

**部署配置**：brain profile 包含 brain-worker、brain-publisher、celery-beat；API/publisher 产物只读，worker 读写。Publisher 不挂载仓库/源码/LLM 凭据，不加载共享 env_file；独立 Docker secret 经 GITLAB_BRAIN_PUBLISH_TOKEN_FILE 加载，文件由 PROJECT_BRAIN_PUBLISH_TOKEN_PATH 指定。GITLAB_TOKEN（评论）/GITLAB_READ_TOKEN（读取）在 Brain 模式须为无代码写权限身份。Token scope 不提供目录权限：强服务端保证由管理员安装 scripts/gitlab/project_brain_pre_receive.py，按 GL_ID 检查每个新提交与最终树；本地交付不代表已安装。配置默认值见 .env.example；完整部署/回滚见 docs/project-brain.md，hook 安装见 docs/project-brain-server-hook.md。

**测试**：`tests/unit/test_project_brain.py` 覆盖真实临时 Git 快照、混合语言事实、删除失效、路径与证据、隐私失败、DB 租约/预算和完整本地检查点链路；真实模型及 GitLab 使用模拟边界，团队项目准确率仍需试点人工评估。

## 原有单元测试

单元测试位于 `tests/unit/`，使用 pytest：

- `test_agent_tools.py` — AgentToolExecutor 沙箱工具测试
- `test_context_manager.py` — 上下文裁剪逻辑测试
- `test_dimensions.py` — 维度定义和格式重试提示测试
- `test_privacy_filter.py` — 隐私过滤规则和脱敏/反脱敏测试
- `test_review_engine.py` — 审查引擎解析和格式化测试
- `test_webhooks.py` — Webhook 路由和事件处理测试
- `test_code_graph.py` — 代码图解析器、存储、查询和影响面分析测试
- `test_readme_generator.py` — AI_README 文档生成引擎测试（指纹计算、行数统计、工具过滤、防递归等）
