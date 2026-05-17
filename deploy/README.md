# 中心部署说明

推荐部署结构：

```text
PostgreSQL 127.0.0.1:5432
FastAPI    127.0.0.1:8000
Cloudflare Tunnel / Caddy / Nginx -> 127.0.0.1:8000
```

## 1. 准备应用

```bash
cd /opt/message-to-danmuku
python3 -m venv .venv
.venv/bin/pip install -U pip setuptools wheel
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：

```env
DB_HOST=127.0.0.1
APP_HOST=127.0.0.1
APP_PORT=8000
ROOT_PATH=
ADMIN_TOKEN=生成一个长随机字符串
VIEW_TOKEN=生成另一个长随机字符串
REQUIRE_VIEW_TOKEN=false
```

如果挂在反代子路径，例如 `https://example.com/danmuku/`，设置：

```env
ROOT_PATH=/danmuku
```

如果弹幕页也不希望公开观看，把 `REQUIRE_VIEW_TOKEN` 改为 `true`。

Cloudflare Tunnel 场景下，`cloudflared` 到应用的来源地址可能不是 `127.0.0.1`，但应用仍然只监听本机。此时可以设置：

```env
ALLOWED_HOSTS=["*"]
```

## 2. 确认数据库触发器

```bash
psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -f migrations/001_create_notify_trigger.sql
```

PostgreSQL 应只监听本机，不需要开放公网 5432。

## 3. systemd

```bash
sudo cp deploy/message-to-danmuku.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now message-to-danmuku
sudo systemctl status message-to-danmuku
```

## 4. Caddy

把 `deploy/Caddyfile.example` 中的域名改成真实域名后合并到 Caddy 配置，然后重载：

```bash
sudo caddy reload
```

访问：

```text
https://你的域名/?token=VIEW_TOKEN
https://你的域名/control?token=ADMIN_TOKEN
```

首次带 token 访问会写入 HttpOnly Cookie，并跳转到不带 token 的 URL。

## 5. Cloudflare Tunnel

推荐公网部署用 Cloudflare Tunnel，而不是开放应用端口。Cloudflare Tunnel 的 public hostname 指向：

```text
http://127.0.0.1:8000
```

如果 QUIC/UDP 出站不稳定，可以让 systemd 中的 `cloudflared` 使用 HTTP/2：

```text
cloudflared --no-autoupdate tunnel --protocol http2 run --token ...
```

当前验证过的访问形态：

```text
https://danmuku.f1justin.com/
https://danmuku.f1justin.com/control?token=ADMIN_TOKEN
wss://danmuku.f1justin.com/ws
```
