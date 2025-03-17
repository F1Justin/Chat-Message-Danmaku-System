# 群聊消息弹幕系统 (Chat Message Danmaku System)

这是一个将群聊消息转换为弹幕显示在大屏幕上的应用程序。该程序从PostgreSQL数据库实时获取群聊消息，并通过WebSocket将其作为弹幕发送到前端页面，可与OBS等直播软件结合使用。

## 功能特点

- 从数据库实时获取群聊消息
- 支持选择特定群聊进行监听
- 显示最近的消息历史
- 转换消息为HTML弹幕
- WebSocket实时推送
- 支持GPU加速的弹幕动画
- 可调整弹幕显示效果
- 兼容OBS浏览器源
- 平滑的弹幕动画，确保弹幕完整穿过屏幕
- 通过消息参数控制弹幕样式和位置

## 系统要求

- Python 3.7+
- PostgreSQL数据库
- 现代浏览器（支持WebSocket和Web Animation API）

## 安装

1. 克隆此仓库：
   ```
   git clone https://github.com/yourusername/Message-to-danmuku.git
   cd Message-to-danmuku
   ```

2. 安装依赖：
   ```
   pip install -r requirements.txt
   ```

## 配置

本项目使用环境变量管理敏感配置信息：

1. 复制`.env.example`文件并重命名为`.env`：
   ```
   cp .env.example .env
   ```

2. 编辑`.env`文件，必须填入您的数据库连接信息：
   ```
   DB_USER=your_database_username
   DB_PASSWORD=your_database_password
   DB_HOST=your_database_host
   DB_PORT=5432
   DB_NAME=your_database_name
   ```

3. 所有标记为必须的环境变量都必须设置，否则应用将无法启动

本项目默认与NoneBot聊天记录插件兼容，使用以下表：
- `nonebot_plugin_chatrecorder_messagerecord`（消息记录表）
- `nonebot_plugin_session_orm_sessionmodel`（会话信息表）

如果您使用不同的数据库结构，请相应修改`app.py`中的模型定义。

## 使用方法

1. 启动应用程序：
   ```
   python app.py
   ```

2. 服务器将在`http://localhost:8000`上运行

3. 在浏览器中访问该地址，或在OBS中添加浏览器源

4. 从界面选择要监听的群聊，点击"监听该群"按钮

5. 点击"显示最近消息"可以查看该群最近的消息记录

6. 现在，该群的新消息将自动显示为弹幕

## 自定义

通过页面上的控制面板，您可以：
- 更改动画方式（Transform或Left）
- 调整最大弹幕数量
- 清空当前弹幕
- 重新连接WebSocket

## 技术架构

- 后端：FastAPI + SQLAlchemy + asyncpg
- 前端：HTML + JavaScript + WebSocket
- 动画：CSS动画 + Transform属性

## 动画实现

弹幕动画使用CSS动画实现，具有以下特点：
- 使用CSS `transform` 属性实现平滑的动画效果
- 弹幕从屏幕右侧完整进入，穿过整个屏幕后再消失
- 动画持续时间根据文本长度自动调整，确保阅读体验
- 支持固定位置（顶部/底部居中）和滚动弹幕两种模式
- 防止弹幕在移动到屏幕左侧时提前消失

## 消息参数控制

系统支持通过在消息中添加特定参数来控制弹幕的样式和位置。用户可以在发送消息时添加以下参数：

### 位置控制
在消息后添加以下关键词可以控制弹幕的显示位置：
- `居中` - 将弹幕固定在屏幕顶部中央
- `下居中` - 将弹幕固定在屏幕底部中央

### 颜色控制
在消息后添加以下颜色关键词可以改变弹幕颜色：
- `红` - 鲜红色 (#FF3B2F)
- `橙` - 橙色 (#FF9500)
- `黄` - 黄色 (#FFCC02)
- `绿` - 绿色 (#35C759)
- `蓝` - 蓝色 (#31ADE6)
- `靛` - 靛青色 (#5856D7)
- `紫` - 紫色 (#AF52DE)
- `灰` - 中性灰色 (#9E9E9E)

### 使用示例
- `你好 红` - 显示红色的"你好"弹幕
- `通知 居中 黄` - 在屏幕顶部居中显示黄色的"通知"弹幕
- `重要提醒 下居中 红` - 在屏幕底部居中显示红色的"重要提醒"弹幕

系统会自动识别这些参数并应用相应的样式，同时从显示内容中移除这些参数。对于深色弹幕（如靛青色、紫色、绿色和蓝色），系统会自动添加白色描边以提高可读性。

## 注意事项

- 本项目处理了UTC与本地时区的差异，确保消息时间正确显示
  - 数据库中的时间以UTC时间存储
  - 应用程序在查询时会自动将本地时间（UTC+8）转换为UTC
  - 显示时会将UTC时间转回本地时间
- 为提高性能，使用了缓存减少数据库查询次数
- 弹幕动画使用CSS transform实现，确保在各种设备上的平滑表现

## 贡献指南

欢迎贡献代码或提出建议！请遵循以下步骤：

1. Fork本仓库
2. 创建您的特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交您的更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启一个Pull Request

## 问题反馈

如果您发现任何问题或有改进建议，请在GitHub Issues中提出，或直接联系项目维护者。

## 许可证

本项目采用MIT许可证，详情请参阅[LICENSE](LICENSE)文件。 
