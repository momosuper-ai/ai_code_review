# Project Brain 成本与质量控制

正式知识由固定 SHA 的静态事实、已确认关系、有限源码证据和模型解释共同组成。JSON 是规范数据，Markdown 从卡片确定性渲染。模型解释始终是 AI_INFERRED；引用有效不等于业务解释正确。

## 成本口径

实际 Token 是各次调用供应商返回的 prompt_tokens + completion_tokens，包含隐私扫描、语义编译、汇总、输出检查和实际重试。cached_tokens 是输入 Token 的缓存子集，不能再加一次。供应商不返回用量时记录估算标志；丢失响应保留保守预留。

金额需要分别乘以网关实际渠道的输入、缓存输入和输出价格。本服务不知道网关计费合同，不能把 Token 数直接说成确定人民币费用。项目规模、语言/框架、声明与关系密度、模块分包、隐私等级、输出长度、缓存命中及失败重试都会改变成本。

`cost_plan` 记录 canonical_bytes、wire_bytes、semantic_packets 和字符估算的语义输入 Token。`reservation_envelope` 是各请求 UTF-8 保守上界与输出上限之和，既不是账单，也不代表会同时占用如此多额度。输入估算不含语义输出、隐私、格式重试、项目汇总和组装，不能据此保证整个项目在预算内完成。

## 无损优化

1. 完整语义字段投影：代码名称的 AST 位置和语法能确认它的分类上下文，相同完整文本只扫描一次。描述、凭证和未知属性保留各自上下文，不因为内容类似名称而跳过。
2. 源码与表去重：包内短 ID、列数组、表级常量和连续行去掉重复包装，保留事实、关系、所有候选目标、未知状态、证据范围和源码缩进。仅内部绑定审计集合不进入语义传输；完整事实图仍保存这些集合。
3. 检查点复用：只有 Worker 能通过已完成扫描/finalize 状态、固定 SHA、配方及完整哈希创建验证证明。每个新源码 excerpt 仍完整扫描；Summary 继续完整扫描。最后重新应用整个构建积累的规则/HMAC，避免晚发现漏掉早期内容。
4. 稳定增量缓存：仅提交 SHA 变化而输入 blob、范围和依赖未变时可复用；依赖目标内容、公共配置、解析器、提示词、Schema、模型或隐私配方改变仍失效。
5. 并发预算：其他活跃请求暂时预留额度时等待结算，不能直接判预算耗尽。未知费用不释放，旧租约响应不覆盖新租约预留；真正耗尽时保留成功检查点，终止未完成任务，不发布残缺知识。

默认语义包上限从 12000 调整为 32000 保守预算单位，初始输出上限为 4096。只有供应商报告 length，下一次语义请求才逐步增加输出额度，最高 PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS（默认 16384）；结构/证据错误不主动扩容，每次实际调用分别预留和计费，截断正文不能进入正式卡片。若模型实际上下文较小，请求先检查完整输入与输出额度，不通过通用裁剪丢弃证据。

隐私普通包仍为 6000 保守输入单位；超过普通包的完整字段单独扫描，硬上限 PROJECT_BRAIN_PRIVACY_MAX_FIELD_TOKENS（默认 32000），同时受模型上下文、单调用限时和输出检查约束。源码、结论及重试需要回传的 reasoning 均保留完整文本；不能靠截取前半段、丢弃 reasoning、放过长字段或关闭隐私来恢复任务。供应商返回敏感值输出被截断且该包只有一个字段时，仍终止该单元。达到字段、模型上下文或语义输出硬上限是确定性容量故障，任务一次终止并保留已花费用，不再以相同配置反复请求。

`PROJECT_BRAIN_SEMANTIC_EXTRA_BODY` 默认空值继承全局，仅覆盖知识语义编译；非空 JSON 对象也进入生成配方。不同网关对思考参数的支持可能不同，必须实测实际 reasoning、finish_reason、JSON 与证据有效性。`test_brain_dense_model_contract.py` 的普通合成探测上限50000 Token；显式额外大包探测仅合成数据、最多两次语义请求、100000 Token硬预算。协议通过仍不能替代真实项目业务质量评估。

完整stop/合法JSON仍可能包含不存在的证据。每包输出Schema明确列出全部允许E别名，减少生成后重试；严格引用与人工语义核验仍保留。测试包含命名为E前缀但不是证据ID的合成函数，检查模型是否混淆命名空间。

`PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE=auto` 保持现有 response_format 策略。确认网关支持后可选择 `strict_tool`，强制返回一次固定知识函数的 JSON 参数，函数只是格式容器，不执行任何操作。当前包全部证据别名作为 enum；未知工具、多个调用、普通正文或异常结束均拒绝，不自动退回普通 JSON。仅为兼容传输去掉网关不支持的长度关键字，原非空/Pydantic、证据、路径与输出隐私门禁仍保留。完整工具定义、选择参数与完整响应纳入预留或缺用量估算；模式进入配方，需新建构建。

## 质量验收

规范事实和卡片验证路径、SHA、Git blob、行范围、符号归属、关系状态和关联引用。E 别名只在当前包有效；空白标题或结论、伪造引用、不完整输出均拒绝。新服务侧 JSON/JSONL 文件写入 manifest.service_files；完整 ZIP 另有 DB 中的哈希，读取和导出前检查。旧清单没有服务文件哈希时只兼容读取，不声称已认证。

`scripts/brain_quality_audit.py` 独立解析固定源码，分别报告受支持提取范围的精确率/召回率、未知范围和文档定位。40 道源码确认的问题用于检查知识能否支撑回答；关键词出现只表示可定位，不能充当回答正确率。业务含义和推断仍需逐条人工核对。

2026-10-09 新隐私协议合成验证：两轮各 100 个口令均识别，6 正例/8 负例混合样本无漏报/误报，11 次请求、13807 Token，未出现截断或格式错误。这不是实际项目敏感值召回率。真实首建通过后还需独立评估，单次首建不能证明增量更新或 GitLab 发布正确。

## 本机更新与回滚

本轮只使用 obc-brain-local，保留 PostgreSQL、Redis、公网代理与隧道，停止并更新 api、brain-worker、celery-beat。以下命令在仓库的 WSL 目录执行，始终使用同一配置和 public overlay：

```bash
docker compose --env-file .env.brain-local -f compose.brain-local.yml -f compose.brain-public.yml stop api brain-worker celery-beat
# 对照 .env.example 同步配置；不要复制覆盖原密钥。
docker compose --env-file .env.brain-local -f compose.brain-local.yml -f compose.brain-public.yml build api brain-worker celery-beat
docker compose --env-file .env.brain-local -f compose.brain-local.yml -f compose.brain-public.yml up -d --no-deps api
docker compose --env-file .env.brain-local -f compose.brain-local.yml -f compose.brain-public.yml exec -T api alembic upgrade head
docker compose --env-file .env.brain-local -f compose.brain-local.yml -f compose.brain-public.yml exec -T api alembic current
docker compose --env-file .env.brain-local -f compose.brain-local.yml -f compose.brain-public.yml up -d --no-deps brain-worker celery-beat
curl --fail http://127.0.0.1:18080/health
docker compose --env-file .env.brain-local -f compose.brain-local.yml -f compose.brain-public.yml ps
docker compose --env-file .env.brain-local -f compose.brain-local.yml -f compose.brain-public.yml logs --tail=50 api brain-worker celery-beat
```

当前分模型记账版本新增007迁移，预期为007_brain_usage；先迁移再启动Worker/Beat。真实构建使用新配方、新build ID，不续跑旧配方的失败单元。2026-10-10曾按当时授权临时提高至200万，后续400万试验累计已用3,962,515，剩37,485；这些历史授权不能自动转为新重建预算。常规测试配置已恢复500000，当前试点paused。新账可按模型/阶段/用途查询；旧账不反推模型Token比例，见[分模型用量](project-brain-model-usage.md)。

回滚时先停止这三个服务，恢复 ignored secrets/brain-cost-deploy-backup/config.env，并将 obc-brain-local:before-cost-20261010 标记回 obc-brain-local:dev，然后使用相同 up 命令及健康/迁移检查。不恢复旧 DB 覆盖新运行记录，不删除卷、不降级迁移、不重启共享 Docker/WSL。
