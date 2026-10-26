# 中心部署与迁移

```text
机器人／采集器 → PostgreSQL 127.0.0.1:5432
                       ↓
               FastAPI 127.0.0.1:8000
                       ↓
          Cloudflare Tunnel 或 Caddy / Nginx
                       ↓ HTTPS / WSS
              外部浏览器／OBS
```

展示电脑不建立 SSH 隧道、不连接 PostgreSQL。服务器可以保留 SSH 供维护使用。

## 新部署

按根目录 README 安装依赖、填写 `.env`、创建房间、安装数据库通知触发器。`rooms.json` 与 `room-state/` 必须能被服务运行用户读取／写入，建议目录权限 `0700`、凭据文件 `0600`。

systemd 模板默认工作目录为 `/opt/message-to-danmuku`。已有安装使用 `/home/justin/message-to-danmuku` 时保留实际路径，调整 `WorkingDirectory`、`EnvironmentFile` 和 `ExecStart` 后安装：

```bash
sudo cp deploy/message-to-danmuku.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now message-to-danmuku
sudo systemctl status message-to-danmuku
```

保持一个 worker。应用默认只监听 `127.0.0.1`，数据库不开放公网端口。`ALLOWED_HOSTS` 实际是客户端地址白名单；代理透传真实客户端 IP 时设为 `["*"]`，访问权限由房间令牌控制。

Cloudflare Tunnel 的 public hostname 指向 `http://127.0.0.1:8000`。网络不支持 QUIC 时可使用 `cloudflared tunnel --protocol http2`。使用 Caddy 时修改 `Caddyfile.example` 域名再合并到已有配置。

子路径部署设置 `ROOT_PATH=/danmuku`，反向代理必须剥离此前缀再转发；创建房间时传 `--base-url https://example.com/danmuku`。页面、API、预览和 WebSocket 会保留房间参数及此前缀。

## 从原单房间版本迁移

1. 私有备份原应用、`.env` 和 `config.json`，记录当前选中的实际群 ID。
2. 创建 `id=default` 的房间，写入明确的 `allowed_groups`，`platform` 保持现有 `QQClient`。若需保持原控制／观看链接有效，可在服务器本机将原 `ADMIN_TOKEN`、`VIEW_TOKEN` 写入该房间（均至少 32 字符且不能相同）；否则使用新生成链接。
3. 原 `config.json` 可复制到 `room-state/default.json` 保留别名、收藏和速度。旧文件中的群选择不会扩大新白名单。
4. 检查配置后重启服务，再检查控制台、观看端及真实消息上屏。撤销任一房间时移除配置并重启服务。

没有 `rooms.json` 时，应用可从 `.env` 的 `ADMIN_TOKEN`、`VIEW_TOKEN`、`ALLOWED_GROUPS=["实际群ID"]` 建立 `default` 房间，原不带 `room` 的 URL 仍指向它。缺少令牌时无可访问房间；缺少白名单时不转发消息。旧 `REQUIRE_VIEW_TOKEN=false` 不再允许匿名观看。

多个房间启用后，原全局令牌不会自动获得所有房间权限。不得将原本全库可见的权限复制给外部使用者。扩展消息源通过对应房间的注入令牌接入，无需把数据库帐号交给展示端。

## 日志与回滚

首次认证链接带有令牌；不要启用包含完整查询串的代理访问日志。应用响应使用 `Referrer-Policy: no-referrer` 和 `Cache-Control: no-store`。反向代理仅信任本机代理的转发头。

查看 `journalctl -u message-to-danmuku` 定位数据库重连；`/api/status?room=default` 需控制令牌。恢复旧程序时同步恢复旧配置；旧版本会重新启用原有全局权限语义，不能沿用新版的隔离保证。
