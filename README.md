# 群聊消息弹幕系统

将群聊消息实时转换为弹幕显示，可与 OBS 等直播软件结合使用。

## ✨ 特性

- **实时推送**：基于 PostgreSQL LISTEN/NOTIFY，延迟 <50ms
- **Canvas 渲染**：高性能弹幕引擎，支持高密度显示
- **多群监听**：支持同时监听多个群聊
- **样式控制**：通过消息参数控制颜色和位置
- **OBS 兼容**：透明背景，直接作为浏览器源使用

## 🛠️ 技术栈

| 组件 | 技术 |
|------|------|
| 后端 | FastAPI + asyncpg + SQLAlchemy |
| 前端 | Canvas 2D + WebSocket |
| 数据库 | PostgreSQL (LISTEN/NOTIFY) |
| 配置 | Pydantic Settings |

## 📦 安装

```bash
# 克隆仓库
git clone https://github.com/yourusername/Message-to-danmuku.git
cd Message-to-danmuku

# 安装依赖
pip install -r requirements.txt
```

## ⚙️ 配置

### 1. 环境变量

创建 `.env` 文件：

```env
# 数据库配置 (必填)
DB_USER=your_username
DB_PASSWORD=your_password
DB_HOST=your_host
DB_PORT=5432
DB_NAME=your_database

# 应用配置 (可选)
APP_HOST=127.0.0.1
APP_PORT=8000
LOG_LEVEL=INFO
```

### 2. 数据库触发器

在 PostgreSQL 中执行触发器脚本以启用实时推送：

```bash
psql -h <host> -U <user> -d <database> -f migrations/001_create_notify_trigger.sql
```

或在数据库管理工具中执行 `migrations/001_create_notify_trigger.sql` 的内容。

## 🚀 启动

```bash
# 方式 1：直接运行
python app.py

# 方式 2：使用启动脚本
./start.sh        # 正常启动
./start.sh dev    # 开发模式 (热重载)
./start.sh stop   # 停止服务
```

访问：
- **弹幕页面**：http://localhost:8000
- **控制面板**：http://localhost:8000/control

## 🎨 弹幕样式控制

在消息中添加关键词控制弹幕样式：

### 位置
| 关键词 | 效果 |
|--------|------|
| `居中` | 顶部居中固定 |
| `下居中` | 底部居中固定 |

### 颜色
| 关键词 | 颜色 |
|--------|------|
| `红` | 🔴 #FF3B2F |
| `橙` | 🟠 #FF9500 |
| `黄` | 🟡 #FFCC02 |
| `绿` | 🟢 #35C759 |
| `蓝` | 🔵 #31ADE6 |
| `靛` | 🟣 #5856D7 |
| `紫` | 🟣 #AF52DE |
| `灰` | ⚪ #9E9E9E |

### 示例
```
重要公告 居中 红     → 顶部居中显示红色文字
欢迎新人 下居中 绿   → 底部居中显示绿色文字
普通消息 蓝         → 滚动显示蓝色文字
```

## 📁 项目结构

```
Message-to-danmuku/
├── app.py                 # 主应用程序
├── config.py              # Pydantic 配置管理
├── connection_manager.py  # WebSocket 连接管理器
├── config.json            # 运行时配置 (群别名等)
├── requirements.txt       # Python 依赖
├── start.sh               # 启动脚本
├── migrations/
│   └── 001_create_notify_trigger.sql  # 数据库触发器
├── templates/
│   ├── danmaku.html       # 弹幕显示页面 (Canvas)
│   └── control.html       # 控制面板
└── static/                # 静态资源目录
```

## 🔧 数据库兼容性

本项目默认与 [NoneBot 聊天记录插件](https://github.com/noneplugin/nonebot-plugin-chatrecorder) 兼容：

- `nonebot_plugin_chatrecorder_messagerecord`
- `nonebot_plugin_session_orm_sessionmodel`

如使用其他数据库结构，请修改 `app.py` 中的模型定义。

## 🔒 安全

- 默认仅允许本机访问 (`127.0.0.1`, `::1`, `localhost`)
- WebSocket 和 HTTP 均有访问限制

## 📄 许可证

MIT License - 详见 [LICENSE](LICENSE)
