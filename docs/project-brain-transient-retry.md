# Project Brain 瞬时重试与诊断

2.4.0 仅对 `ModelTransportFailed`、`PrivacyResponseInvalid`、`ModelWorkAdmissionDenied` 做单元级有界延迟重试；同一单元最多重试 3 次，默认延迟 60/120/240 秒。重试次数独立于单元执行次数，不放宽隐私、证据、预算、来源认证或未知调用门禁。发现未知模型调用仍按 `ModelCallsIncomplete` 终止；`BudgetExhausted`、编译硬上限、身份/检查点/来源绑定及封存错误也保持终态。

配置项位于 `src/config.py`，不进入组件配方配置：

| 配置 | 默认值 | 范围 |
|---|---:|---:|
| `PROJECT_BRAIN_UNIT_TRANSIENT_RETRIES` | 3 | 0–6 |
| `PROJECT_BRAIN_TRANSIENT_BACKOFF_SECONDS` | 60 | 5–600 |
| `PROJECT_BRAIN_BREAKER_THRESHOLD` | 6 | 1–100 |
| `PROJECT_BRAIN_BREAKER_WINDOW_SECONDS` | 600 | ≥30 |
| `PROJECT_BRAIN_BREAKER_COOLDOWN_SECONDS` | 600 | ≥30 |
| `PROJECT_BRAIN_BREAKER_MAX_TRIPS` | 3 | 1–10 |

时间窗内达到阈值后，构建进入现有暂停/排空流程。Beat 只会在熔断状态仍为 `tripped`、构建仍为 ACTIVE 且冷却期已满时自动恢复；人工暂停不会自动恢复。超过最大熔断次数后构建以固定 `upstream_unhealthy` 错误终止。人工 API 恢复也会关闭熔断状态。

`brain_units.diagnostic` 仅保存允许的异常类别、白名单标量属性、重试次数和固定名称的数值时序；`brain_builds.diagnostic.breaker` 保存窗口事件、熔断次数/状态和时间。异常消息、URL、模型响应及源码内容不会持久化。`ModelWorkAdmissionDenied` 的剩余/所需秒数只记录为整数。

只读汇总示例：`DATABASE_URL=... python scripts/brain_timing_report.py --build-id <UUID>`。脚本只查询数据库，以 JSON 输出各单元类型的分段样本数、秒数总和、P50/P95、调用次数及模型调用对执行/构建壁钟的覆盖率。

本次验证范围为离线合成测试和一次性 PostgreSQL 集成测试；未运行真实模型、未部署，也不据此宣称真实构建质量或模型恢复能力。

## 2.4.1 跨提交编译复用

仅复用 schema2 构建中按完整 `input_hash` 精确匹配的已完成 compile 检查点；不会按模块索引或 compile key 复用。基础构建必须 ready、同项目/配方且公开 schema 为 2.0；候选结果、证据 ID、当前 finalize/privacy/source-privacy 检查点及当前来源包都重新认证。旧结论经当前隐私历史投影后必须与原字节级摘要一致，否则回到正常语义编译。接受时在完成锁内再次验证父构建/单元/结果，标记仅存于构建诊断，结果不保留内部标记。

privacy 与 source_privacy 阶段仍在每个新构建中完整重跑；增量复用不跳过任何隐私、证据、预算、来源绑定或未知调用门禁。覆盖/用量返回复用与重新编译数量；finalize 的规划 `cache_hits` 仍为 0。后续同 SHA recovery 对无模型调用的增量导入单元采取保守重新编译，不伪称具有 recovery 调用证明。

仅完成离线实现/测试，未部署或真实运行模型。
