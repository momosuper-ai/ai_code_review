# Project Brain 暂停与继续

从 `project-brain/2.3.0`、迁移 `010_brain_control` 开始，每个构建持久保存独立执行控制状态。构建的 `status` 仍表示扫描、提取、编译、校验或终态；`execution_state` 表示是否允许继续执行。

| execution_state | 含义 |
|---|---|
| running | 可以领取新单元 |
| pausing | 已停止领取新单元，正在排空现有单元 |
| paused | 已无执行中单元、有效租约或未确认调用，可以保留进度等待继续 |

请求暂停后，在项目和构建行锁内关闭新单元准入。已经领取的单元可以继续完成语义输出、必要隐私扫描、用量结算与检查点保存；不能为追求立即停止而丢弃内存中的已付费输出。队列中旧消息在领取时再次检查持久状态，暂停后不会启动新单元。最后一个单元完成或正常让出任务后，才确认 `paused`。

**应等待“已暂停”后再让电脑休眠。** `pausing` 期间仍可能有模型调用，等待时间受当前单元的工作窗口和超时保护约束。突然休眠、断电或强杀进程仍可能造成未知响应；算法不会把这种情况伪装为正常暂停，不会清零费用或声称无损恢复。

继续时保持同一个 build UUID、固定 SHA、配方、已完成结果、隐私收据、attempt 和账目。只在配方未改变、当前无在途或 unknown 调用、原维护代际和恢复依赖有效时重新开放准入。只重派 pending 单元，清除暂停期间失效的投递时间，避免额外等待15分钟；重复继续请求不会重置进度或重复登记构建。

暂停期间 Worker、API 或电脑可以退出。重新启动服务不会自动解除持久暂停；用户明确要求继续后才恢复。项目级 `maintenance_state=paused` 是关闭分支自动维护的另一项配置，不能替代本功能。非历史构建若在暂停期间被新源码维护请求取代，继续会拒绝旧代际，不会静默切换 SHA。

## 控制接口

沿用现有认证和项目作用域：

```text
POST /api/v1/projects/{project_id}/brain/builds/{build_id}/pause
POST /api/v1/projects/{project_id}/brain/builds/{build_id}/resume
GET  /api/v1/projects/{project_id}/brain/builds/{build_id}
```

POST 接受空对象，拒绝附带源码、模型、预算等字段。响应和 GET 提供 `execution_state`、`pause_requested_at`、`paused_at`、`resumed_at`，时间由持久数据库保存。暂停入口在 Brain 总开关关闭时仍可用；继续要求 Brain 执行已启用。失败、已就绪或已被取代的构建不能通过继续冒充恢复；应使用原有失败诊断和认证恢复流程。

可信 Worker 环境还可执行运维命令，无需输出凭据：

```text
python scripts/brain_execution_control.py status BUILD_UUID --project-id GITLAB_PROJECT_ID
python scripts/brain_execution_control.py pause BUILD_UUID --project-id GITLAB_PROJECT_ID
python scripts/brain_execution_control.py resume BUILD_UUID --project-id GITLAB_PROJECT_ID
```

它只控制明确指定的项目/构建，不启动模型或修改电源策略；继续时可向原队列派发同一构建的 pending 单元。聊天中的“暂停/继续”由助手先核对本机当前构建记录，再使用这些接口，不要求用户手动填写 UUID。

## 失败与部署边界

- 暂停请求即使发现已有 unknown，也会持久保留停发意图并显示 `pausing`，不会错误确认 `paused`；任务/Beat 会按未确认调用保护终止，保留费用和检查点。
- Beat 回收或 Broker 重投领取过期租约时，若旧预留变为 unknown，立即终止本构建，不发新模型请求；旧真实响应仍可按原 UUID 独立结算。
- 运行中单元若最后完成整个组装，`ready` 优先；真实失败也优先，不会把终态改写为暂停成功。
- 部署前必须排空并停用旧 Worker/Beat，再迁移和启动全部新版本进程。旧版本不了解执行控制字段，不能与新版本混跑。
- 存在 `pausing/paused` 记录时，010迁移拒绝降级，避免删除暂停意图后旧 Worker 自动运行。
- 修改生成配方后不能用新代码直接继续旧 paid 构建；暂停控制与认证父子恢复是两个不同流程，后者仍须完整内容兼容、隐私和来源认证。

本说明描述实现语义。离线、数据库竞态、部署和真实试点的验证结果分别记录在 `AGENT.md` 及对应本机控制目录，不以接口已实现代替正式质量验收。
