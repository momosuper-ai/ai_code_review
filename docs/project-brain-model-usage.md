# Project Brain 分模型用量

新构建按模型别名、用途和阶段持久记录每次预留与结算。接口为：

```text
GET /api/v1/projects/{project_id}/brain/builds/{build_id}/usage
```

沿用 API Key、IP 白名单和60/min读取限流；公共Webhook代理不开放该接口。构建未就绪或失败时仍可查看用量。

## 统计口径

- `models`按`role + requested_model`分组。隐私扫描、语义生成和格式重试都会记账；相同模型别名用于不同角色时分别列出。
- `breakdown`进一步列出`stage`、`purpose`与配置别名匹配的供应商报告值。未分类的供应商模型字段不落盘，也不猜测真实后端型号。
- `total_tokens = prompt_tokens + completion_tokens`。`cached_tokens_subset`属于prompt，不能再次从总数扣除。
- `share_scope`表示读取时的已结算用量，执行中构建还会增加。只有费用全量可归属、没有估算/未知/活动预留时才返回精确占比。
- 旧版本仍显示完整总额，但标记`legacy_unattributed`，不由阶段用量、调用次数或样例推算模型占比。部分新旧混合记录显示`partial`。
- 缺usage时记录保守估算及标记；丢失响应保留未知额度。`currency_cost`为null，没有配置的实际价格不能换算金额。
- 从2.1.0起，完整临时HTTP错误单列`closed_http_error_calls`和`closed_http_error_reserved_tokens`，保留全部费用上界；`ambiguous_reserved_tokens`表示真正响应不确定的额度。兼容的`unknown_reserved_tokens`包含两者，均不算作已结算Token，也不返回精确全构建占比。每次重试另行预留、另记UUID，不能把失败结算为0。协议和迁移008见[有界HTTP恢复](project-brain-http-retry.md)。

公开Schema 2构建保留相同分组和数字统计，但API将`requested_model`替换为构建内的`model:1`等机器标签，`response_model`为null。分组仍按原始配置别名和角色计算，相同别名用于不同角色也不会合并。标签用于对照本次构建，不是模型名称，不能拿不同构建的同号标签推断它们使用相同模型。这样，即使配置别名恰好与已识别的源码秘密同值，也不会经用量接口泄露；旧Schema 1沿用原有别名显示。

## 持久化与并发

迁移`007_brain_usage`新增`brain_model_calls`与`brain_builds.usage_tracking_version`。旧记录版本为0，新ORM构建版本为1；启动自动建表仍只创建旧审查/README表。

供应商缺少 usage 时，隐私和语义调用都按完整响应估算，包括 content、reasoning、reasoning_content、thinking 及工具参数。估算仅在内存执行，账目标记 estimated，不保存这些内容，也不提供精确模型占比。

调用前生成一次性UUID，将模型/角色/阶段和预算预留在同一事务中记录，再发送请求。调用返回后，实际用量、构建总额、预留释放和调用结算原子提交。相同调用编号的同内容重复结算不重复扣费，冲突重复结算直接拒绝；重复预留不能再次发送请求。

进程丢失响应、任务取消或租约交接时，调用转为unknown而不释放预算。旧响应迟到只能结算自己的记录，不清除新租约预留。实际供应商用量超过预算时先保存实际账，再终止流程；重复结算不能绕过超额门禁。

原SHA重新成为目标、复用superseded构建时也执行同样的未知额度转移；构建与单元在锁内恢复，旧调用不能误算为新租约的活动额度。

Brain分别解析本地扫描/语义的扩展参数，空配置继承`LLM_EXTRA_BODY`。拒绝覆盖模型、消息、输出上限、工具、输出格式等正常请求字段；SDK会用extra_body覆盖正常请求，因此这项校验在任何模型调用和语义输入扫描前执行。合法的reasoning_effort和供应商thinking参数仍可使用。解析后的对象显式传入共享客户端，旧审查配置行为保持原样。

表只包含调用/构建/单元标识、租约、固定用途、模型别名、数值、状态与时间；没有消息、源码、响应正文、API Key、URL或脱敏映射。请求模型名是配置中的公开别名；供应商报告字段仅在与该别名一致时保留。

记录`attempt`表示语义请求的格式尝试序号；隐私扫描为分包恢复深度加一，不能将它等同于Celery任务重试次数。

## 验证和部署

不联网回归：

```bash
pytest tests/unit/test_brain_model_usage.py tests/unit/test_brain_model_transport.py tests/unit/test_brain_usage_api.py tests/unit/test_brain_extra_body_boundary.py -q
```

真实PostgreSQL回归还覆盖并行预留/结算、重复送达与迟到响应。使用独立`brain_local`数据库，禁止在业务库运行本机辅助初始化。若用一次性schema隔离，连接初始化必须在autocommit下设置并核验search_path，避免事务回滚还原到public。

部署先备份本机配置、专用DB和旧镜像，停止旧应用角色并重建镜像；先仅启动API，执行`alembic upgrade head`并核对`007_brain_usage`，再启动BrainWorker/Beat。没有新环境变量；`.env.example`无需追加配置。API/BrainWorker/Beat与正式产物读取一致后再开放新的验证，不恢复暂停的监测或启用GitLab写回。

本次实现改变生成配方，后续必须新建构建，不能用新代码继续旧检查点。回滚应用时保留007表和记录，旧应用通过原字段继续运行；只有明确要求删除账目、备份后才降级。
