# 安全政策

## 报告漏洞

如果你发现安全漏洞，**请不要**公开的 issue 中披露。请优先通过 GitHub 的私有漏洞报告功能（**Security Advisories**）提交，或直接联系项目维护者。

报告时请尽量包含：

- 受影响版本 / commit
- 漏洞类型与影响描述
- 复现步骤（最小化示例）
- 可能的修复建议

维护者会在 48 小时内确认收到，并尽快给出修复计划。修复前请勿公开细节，以保护尚未升级的使用者。

## 已知安全边界（设计结论）

本项目是一个部署在**受信任内网**的自动化代码审查服务，以下边界是有意设计，使用时需知晓：

| 边界 | 说明 |
|------|------|
| 无账号体系 | 服务自身不提供用户认证；安全依赖网络边界（防火墙/VPN）与密钥保护 |
| 开发模式默认放行 | `GITLAB_WEBHOOK_SECRET` / `API_KEYS` / `IP_WHITELIST` 未配置时所有请求默认放行（仅限开发环境，生产必须配置） |
| 速率限制 fail-open | Redis 故障时限流自动放行，由部署方评估是否接受 |
| Webhook 信任面 | 任何持有 `X-Gitlab-Token` 的调用方都可触发审查任务，Token 需妥善保管 |
| 隐私过滤依赖检测 | 敏感数据脱敏依赖规则引擎 + 本地模型扫描；`PRIVACY_INTERNAL_DOMAINS` 等配置为空时检测面变小，生产环境应显式声明内部域名/网段 |
| 评论权限 | GitLab 评论使用配置的 Token 权限，Token 应只授予所需的最小 scope |
| AI_README 自动提交 | 文档自动生成会向仓库提交内容（直接 commit 或自动合并 MR），启用 `README_GENERATION_ENABLED` 前请确认分支保护与自动合并策略符合预期 |

## 部署建议

- 生产环境必须配置 `GITLAB_WEBHOOK_SECRET`、`API_KEYS`、`IP_WHITELIST`，并通过网络边界限制访问
- 使用带密码的 `REDIS_URL`（参考 `config.py` 注释）
- 数据库密码统一在 `.env` 管理，`.env` 永不提交
- 定期轮换 GitLab Token、API Key 与数据库/Redis 密码
