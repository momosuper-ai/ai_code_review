# 临时公网 HTTPS Webhook 验证

本机无法配置网络设备端口转发时，在已有 WSL 测试栈上增加出站隧道。公网 URL 由 Cloudflare Quick Tunnel 分配，不需要账号或域名；停止隧道后失效，重建会更换域名，适用于临时测试。[Cloudflare 官方说明](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

链路为 GitLab → Cloudflare HTTPS → brain-tunnel → brain-webhook-proxy → api。事件与 Webhook Secret 会经过 Cloudflare，本轮用户已经明确授权所生成的测试目的地。切换到其他运营方或其他目的地时重新核对授权范围。模型请求、GitLab 读取/发布凭据、源码快照和知识产物不交给隧道容器。

## 一、启动前提

先按 [本机验证说明](project-brain-local-test.md) 初始化独立配置、构建业务镜像、完成迁移并启动 API/Worker/Beat。本辅助入口没有新增数据库迁移，也不修改业务镜像；已有库仍需处于 006_brain_auto。原有审查/发布 Worker 和 GitLab 同步继续关闭。

两个公共辅助镜像已固定摘要。Nginx 仅允许 GET /health 和 POST /api/v1/webhooks/gitlab，其余路径 404，方法不符 405；Webhook Secret 仍由 API 验证。代理日志只保存时间、固定路由类别、方法、状态与耗时，不保存原始 URL、查询参数、请求头或正文。两容器均只读、禁用提权和额外 capabilities，不加载 .env.brain-local，也不挂业务/产物卷。[Nginx 代理说明](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)

## 二、启动与配置同步

在 WSL 执行：

```bash
cd /mnt/d/MO/code/obc_ai_code_review
dc_public() {
  docker compose --env-file .env.brain-local \
    -f compose.brain-local.yml -f compose.brain-public.yml --profile tunnel "$@"
}

# 若已有旧的测试隧道，先停止它们，避免产生两个不同公网地址。
dc_public stop brain-tunnel brain-webhook-proxy
```

备份 `.env.brain-local` 后将 BRAIN_LOCAL_BIND_ADDRESS 设置为 127.0.0.1；保留已有 GITLAB_WEBHOOK_SECRET、API_KEYS、模型和密码。仅更改本机监听配置时，停止并重新创建 API；业务代码变更时仍按本机文档执行完整重建和迁移。

```bash
dc_public stop api
dc_public up -d --no-deps --force-recreate api
curl -fsS http://127.0.0.1:18080/health

# 公共辅助服务只拉取固定镜像，不需要构建业务镜像或执行新迁移。
dc_public config --quiet
dc_public pull brain-webhook-proxy brain-tunnel
dc_public run --rm --no-deps brain-webhook-proxy -t
dc_public up -d brain-webhook-proxy brain-tunnel
dc_public ps
dc_public logs --tail=60 brain-tunnel
```

日志中出现 Registered tunnel connection 后，取本次生成的 https://...trycloudflare.com 域名。不要使用旧隧道的域名，也不要把本机 18080 端口附在 HTTPS 地址上。本机防火墙无需新增入站规则；仍需本机出站网络能连接隧道服务。

## 三、验证与 GitLab 配置

先检查新域名 /health 返回 200 和 status=ok，/docs、/openapi.json、/api/v1/reviews、知识查询返回 404。缺少 Secret 的 Webhook POST 返回 401。认证探测仅使用不会触发构建的合成 tag_push，并在明确授权的 HTTPS 地址上发送，不打印 Secret；它只证明公网入口及认证通，不代表 GitLab 服务器实际投递已通过。

在 GitLab 项目现有 Webhook 修改：

| 配置 | 值 |
|---|---|
| URL | 本次域名 + /api/v1/webhooks/gitlab |
| Secret | 保留本机 .env.brain-local 的 GITLAB_WEBHOOK_SECRET |
| 事件 | Push events、Merge request events |
| SSL verification | 保持开启 |
| 分支 | 实际目标分支；应用当前 MAIN_BRANCHES 为 main,master |

执行 Test → Push events，并检查 Recent events 的状态码和响应。成功后做一次实际源码变更，验证首建/增量；仍只生成知识，不写文档或发表评论。若没有代理/API 接收记录，继续检查 GitLab 的外网 DNS/HTTPS 出站和投递结果；若返回 401，核对 Secret；若返回 200 却未构建，检查事件类型、分支和 Brain 状态。

观察命令：

```bash
dc_public logs --since=5m --tail=100 api brain-worker brain-webhook-proxy brain-tunnel
```

不要打印配置、模型上下文和业务正文。隧道重启需重新核对域名；API 重建后代理通过 Docker DNS 自动更新目标。日志跟进若已在应用中暂停，保留暂停，需用户明确恢复才能继续定时运行。

容器running不证明公网入口可用。WSL重启后本轮曾持续收到Cloudflare `Unauthorized`，临时域名在Windows/WSL及公共DNS均无法解析；这与GitLab Webhook Secret是不同认证链路。只重建brain-tunnel后重新申请临时入口，再确认注册连接、域名、health=200与docs=404，更新ignored入口记录和GitLab Webhook URL。不要把日志中尚未验证的新域名作为可用地址；不更改GitLab Secret或扩大防火墙来解决隧道认证错误。

## 四、停止与回滚

仅停公网入口，不停止知识生成或删除数据：

```bash
dc_public stop brain-tunnel brain-webhook-proxy
dc_public rm -f brain-tunnel brain-webhook-proxy
curl -fsS http://127.0.0.1:18080/health
```

删除/禁用 GitLab 中对应临时 Webhook；本机仍保持回环监听。若要停止整个测试栈，包含全部 overlay/profile，保留卷：

```bash
dc_public --profile review --profile publication down
```

恢复时先按本机文档核对配置、重建业务镜像、确认迁移、验证健康，再启动公共入口并更新 GitLab URL。不执行 down -v、全局 Docker/WSL 重启或数据库降级；需要恢复内网方式时，停公共入口，再按原文档配置限定来源的入站规则和监听地址。基础 Compose 单独运行时可能提示公共容器 orphan，不要用 --remove-orphans 删除正在使用的入口。
