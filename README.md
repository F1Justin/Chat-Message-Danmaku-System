# 群聊消息弹幕系统

在自己的服务器采集群消息、连接 PostgreSQL，向不同房间的浏览器／OBS 推送弹幕。使用者只需要房间链接，不需要 SSH、Python、数据库密码或机器人帐号。

```text
现有机器人帐号 → 群聊采集 → PostgreSQL → FastAPI → HTTPS / WSS → 浏览器 / OBS
外部消息源（如 SuperLily）→ 按房间授权的注入 API ────────┘
```

## 房间与权限

每个房间按来源、平台和群 ID 授权，独立维护群白名单、控制／观看／注入令牌、群选择、别名、收藏、速度、显示样式和清屏状态。服务端白名单决定权限，控制台只能缩小显示范围；取消全部会停止显示该房间的新消息。

| 令牌 | 能力 |
|---|---|
| 控制 | 本房间控制台、已授权群列表和历史、显示设置、消息注入 |
| 观看 | 本房间弹幕页面和 WebSocket；不能读取历史或控制显示 |
| 注入 | 仅向本房间已授权群提交消息；不能观看、管理或访问其他房间 |

所有房间均需鉴权；空白名单不转发消息。配置错误会阻止服务启动。凭据保存在服务器 `rooms.json`，运行状态保存在 `room-state/`，均不进入 Git。

## 服务器部署

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env` 中的数据库配置，再创建房间。`--group` 填实际群 ID，可重复指定：

```bash
.venv/bin/python manage_rooms.py event-a --name '活动 A' --group 123456 --group 654321
.venv/bin/python manage_rooms.py event-b --name '活动 B' --group 789012
```

命令原子写入权限为 `0600` 的 `rooms.json`，输出各自控制链接和观看链接。只将观看链接交给展示端；需要操作的人员领取对应房间控制链接。修改配置或轮换令牌后重启应用，已有连接也会被关闭。

在源数据库执行通知脚本：

```bash
psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -f migrations/001_create_notify_trigger.sql
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

公网 HTTPS、Cloudflare Tunnel、systemd 配置与旧版迁移见 [部署说明](deploy/README.md)。运行单进程／单 worker；房间广播和监听游标保存在进程内，多 worker 需要额外共享消息总线。

## 展示端

将房间观看链接粘贴到 OBS「浏览器源」，按输出分辨率设置宽高；页面提供透明背景。普通浏览器可以全屏显示。网页没有桌面置顶、鼠标穿透能力，覆盖任意桌面软件需要另外制作原生客户端。

首次访问带令牌链接后写入 HttpOnly Cookie，并跳转到无令牌 URL。各房间、各角色的 Cookie 分开保存，预览页不会覆盖控制权限。链接相当于访问凭据，应用会脱敏 Uvicorn 日志中的令牌参数；反向代理也应避免记录带令牌的查询串。

## 消息源

默认 `source=nonebot` 使用当前 `chatrecorder_v2` 和 `uninfo` 表，通过 `LISTEN/NOTIFY` 唤醒查询；每 2 秒轮询兜底。房间默认平台为生产库已核实的 `QQClient`，其他平台通过 `--platform` 指定，并与数据库 `BotModel.scope` 精确匹配。仅转发 `scene_type=1`、`type=message` 的非空正文。

监听断开后以 1–30 秒退避重连并重新订阅，进程内保留 ID 游标，补读中断期间已提交记录；回看最近 1000 个 ID 并保留最近 20000 个已处理 ID，覆盖邻近事务乱序。它不是持久消息队列：跨进程重启从当前最大 ID 开始，不回放历史；跨度超过回看窗口的长事务不保证补发。数据库只读权限足以运行监听，触发器需由数据库维护者安装。

外部源采用按平台命名的 `source`（例如 `superlily-qq`、`superlily-telegram`），同一群号属于不同来源时使用不同房间。创建外部源房间：

```bash
.venv/bin/python manage_rooms.py lily-event --source superlily-qq --platform qq --group 123456
```

在服务器采集侧用该房间注入令牌提交：

```http
POST /api/inject-danmaku?room=lily-event
Authorization: Bearer <ingest_token>
Content-Type: application/json

{"source":"superlily-qq","platform":"qq","kind":"message","group_id":"123456","user_id":"u1","message_id":"stable-source-event-id","content":"欢迎"}
```

注入正文最多 2000 字符；注入角色必须提供稳定 `message_id`；同房间最近 20000 个注入 ID 在当前进程内去重。注入遵守房间群选择和清屏，不能强制穿透过滤。`sent` 表示 WebSocket 发送成功数，不是画面渲染确认；去重不跨重启。

SuperLily 接入边界目前是此服务端 API。项目不包含其数据库凭据，也未连接实际 SuperLily 入库／推送服务；归档数据的事件筛选、跨源去重、实时延迟仍需在采集适配器中核实。MCP 查询接口不作为弹幕实时订阅通道。

## 运维与验证

`GET /api/status?room=<id>` 使用控制令牌，返回当前房间连接数、来源和数据库监听状态。控制台会区分网页断线与数据库重连。房间凭据、状态目录及 `.env` 需要纳入私有备份。

测试使用隔离 PostgreSQL 和合成数据，见 [测试说明](tests/README.md)。

MIT License.
