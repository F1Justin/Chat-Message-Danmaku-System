"""
群聊消息弹幕系统
使用 PostgreSQL LISTEN/NOTIFY 实现实时事件驱动
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg
import uvicorn
from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from config import get_app_settings, get_db_settings, get_runtime_config
from connection_manager import get_connection_manager

# ============================================================
# 配置初始化
# ============================================================

settings = get_app_settings()
db_settings = get_db_settings()
runtime_config = get_runtime_config()

# 配置日志
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ============================================================
# 数据库配置
# ============================================================

engine = create_async_engine(db_settings.async_url, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


class SessionModel(Base):
    """会话模型 - 对应 nonebot_plugin_session_orm_sessionmodel"""
    __tablename__ = "nonebot_plugin_session_orm_sessionmodel"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(String(64), nullable=False)
    bot_type = Column(String(32), nullable=False)
    platform = Column(String(32), nullable=False)
    level = Column(Integer, nullable=False)
    id1 = Column(String(64), nullable=False)  # 用户ID
    id2 = Column(String(64), nullable=False)  # 群ID (level=2时)
    id3 = Column(String(64), nullable=False)
    
    messages = relationship("MessageRecord", back_populates="session")


class MessageRecord(Base):
    """消息记录模型 - 对应 nonebot_plugin_chatrecorder_messagerecord"""
    __tablename__ = "nonebot_plugin_chatrecorder_messagerecord"
    
    id = Column(Integer, primary_key=True, index=True)
    session_persist_id = Column(
        Integer, 
        ForeignKey("nonebot_plugin_session_orm_sessionmodel.id"), 
        nullable=False
    )
    time = Column(DateTime(timezone=True), nullable=False)
    type = Column(String(32), nullable=False)
    message_id = Column(String(255), nullable=False)
    message = Column(JSON, nullable=False)
    plain_text = Column(Text, nullable=False)
    
    session = relationship("SessionModel", back_populates="messages")


# ============================================================
# PostgreSQL LISTEN/NOTIFY 监听器
# ============================================================

class MessageListener:
    """
    PostgreSQL LISTEN/NOTIFY 消息监听器
    实时监听数据库消息插入事件
    """
    
    def __init__(self):
        self._connection: Optional[asyncpg.Connection] = None
        self._running = False
        self._manager = get_connection_manager()
    
    async def start(self) -> None:
        """启动监听器"""
        if self._running:
            logger.warning("监听器已在运行")
            return
        
        try:
            # 创建独立的数据库连接用于监听
            self._connection = await asyncpg.connect(db_settings.dsn)
            
            # 注册监听器
            await self._connection.add_listener("new_message", self._handle_notification)
            
            self._running = True
            logger.info("PostgreSQL LISTEN/NOTIFY 监听器已启动")
            
        except Exception as e:
            logger.error(f"启动监听器失败: {e}")
            raise
    
    async def stop(self) -> None:
        """停止监听器"""
        self._running = False
        
        if self._connection:
            try:
                await self._connection.remove_listener("new_message", self._handle_notification)
                await self._connection.close()
                logger.info("监听器已停止")
            except Exception as e:
                logger.error(f"停止监听器时出错: {e}")
            finally:
                self._connection = None
    
    async def _handle_notification(
        self, 
        connection: asyncpg.Connection,
        pid: int,
        channel: str,
        payload: str
    ) -> None:
        """处理数据库通知"""
        try:
            data = json.loads(payload)
            message_id = data.get("id")
            session_persist_id = data.get("session_persist_id")
            
            logger.debug(f"收到新消息通知: id={message_id}, session={session_persist_id}")
            
            # 从数据库获取完整的消息信息
            await self._fetch_and_broadcast_message(message_id)
            
        except json.JSONDecodeError as e:
            logger.error(f"解析通知负载失败: {e}, payload={payload}")
        except Exception as e:
            logger.error(f"处理通知时出错: {e}")
    
    async def _fetch_and_broadcast_message(self, message_id: int) -> None:
        """获取消息详情并广播"""
        async with async_session() as session:
            query = (
                select(MessageRecord, SessionModel)
                .join(SessionModel, MessageRecord.session_persist_id == SessionModel.id)
                .where(MessageRecord.id == message_id)
            )
            result = await session.execute(query)
            row = result.first()
            
            if not row:
                logger.warning(f"未找到消息: id={message_id}")
                return
            
            message, session_model = row
            
            # 处理消息内容
            content = self._process_content(message.plain_text)
            
            # 获取时间（确保是 UTC）
            message_time = message.time
            if message_time.tzinfo is None:
                # 如果是 naive datetime，假设是 UTC
                message_time = message_time.replace(tzinfo=timezone.utc)
            
            # 缓存 session 映射
            self._manager.cache_session_mapping(
                str(session_model.id), 
                str(session_model.id2)
            )
            
            # 广播弹幕
            await self._manager.broadcast_danmaku(
                group_id=session_model.id2,
                user_id=session_model.id1,
                content=content,
                message_id=message.message_id,
                timestamp=message_time
            )
    
    @staticmethod
    def _process_content(content: str) -> str:
        """处理消息内容，去除可能的前缀"""
        if not isinstance(content, str):
            return str(content)
        
        # 去除 "用户: 消息" 格式的前缀
        if ": " in content and content.count(": ") == 1:
            return content.split(": ", 1)[1]
        elif ":" in content and content.count(":") == 1:
            parts = content.split(":", 1)
            # 避免分割时间格式
            if not parts[0].isdigit():
                return parts[1].lstrip()
        
        return content


# 全局监听器实例
message_listener = MessageListener()


# ============================================================
# FastAPI 应用
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动
    logger.info("=" * 50)
    logger.info("群聊弹幕系统启动中...")
    logger.info("=" * 50)
    
    # 启动消息监听器
    await message_listener.start()
    
    # 启动统计广播任务
    stats_task = asyncio.create_task(periodic_stats_broadcast())
    
    logger.info(f"\n弹幕页面: http://{settings.host}:{settings.port}")
    logger.info(f"控制面板: http://{settings.host}:{settings.port}/control")
    logger.info("=" * 50)
    
    yield
    
    # 关闭
    logger.info("正在关闭...")
    stats_task.cancel()
    await message_listener.stop()
    logger.info("系统已关闭")


app = FastAPI(lifespan=lifespan)

# 配置模板和静态文件
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ============================================================
# 中间件
# ============================================================

@app.middleware("http")
async def restrict_localhost_middleware(request: Request, call_next):
    """限制只允许本机访问"""
    client_host = request.client.host if request.client else None
    if client_host not in settings.allowed_hosts:
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)


# ============================================================
# 后台任务
# ============================================================

async def periodic_stats_broadcast():
    """定期广播统计信息"""
    manager = get_connection_manager()
    while True:
        try:
            await asyncio.sleep(10)
            await manager.broadcast_stats()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"广播统计信息时出错: {e}")


# ============================================================
# 辅助函数
# ============================================================

async def get_group_id_from_session_id(session_id: str) -> Optional[str]:
    """从 session_id 获取 group_id"""
    manager = get_connection_manager()
    
    # 尝试从缓存获取
    cached = manager.get_cached_group_id(session_id)
    if cached:
        return cached
    
    # 从数据库查询
    try:
        async with async_session() as session:
            query = select(SessionModel.id2).where(SessionModel.id == int(session_id))
            result = await session.execute(query)
            group_id = result.scalar_one_or_none()
            
            if group_id:
                manager.cache_session_mapping(session_id, group_id)
                return group_id
    except Exception as e:
        logger.error(f"获取群聊ID出错: {e}")
    
    return None


# ============================================================
# WebSocket 端点
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 连接处理"""
    # 安全检查
    client_host = websocket.client.host if websocket.client else None
    if client_host not in settings.allowed_hosts:
        await websocket.close(code=1008, reason="Forbidden")
        return
    
    manager = get_connection_manager()
    connection = await manager.connect(websocket)
    
    try:
        # 发送初始状态
        await connection.send_json({
            "type": "connection",
            "message": "连接成功",
            "settings": {
                "danmaku_speed": runtime_config.danmaku_speed
            }
        })
        
        # 发送当前过滤状态
        await connection.send_json({
            "type": "broadcast_filter_update",
            "filter_enabled": manager.global_filter_enabled,
            "allowed_groups": manager.global_allowed_groups
        })
        
        # 发送上次聚焦的群组提示
        if runtime_config.active_group_id:
            await connection.send_json({
                "type": "last_focused_group_hint",
                "group_id": runtime_config.active_group_id
            })
        
        # 消息循环
        while True:
            data = await websocket.receive_text()
            await handle_websocket_message(connection, data)
            
    except WebSocketDisconnect:
        logger.info("WebSocket 连接已断开")
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
    finally:
        manager.disconnect(connection)
        await manager.broadcast_stats()


async def handle_websocket_message(connection, data: str) -> None:
    """处理 WebSocket 消息"""
    try:
        message = json.loads(data)
        
        if message.get("type") != "command":
            return
        
        action = message.get("action")
        manager = get_connection_manager()
        
        if action == "set_groups":
            await handle_set_groups(connection, message, manager)
        
        elif action == "set_active_group":
            await handle_set_active_group(connection, message)
        
        elif action == "get_active_group":
            await handle_get_active_group(connection)
        
        elif action == "set_danmaku_speed":
            await handle_set_danmaku_speed(connection, message, manager)
        
        elif action == "broadcast_settings":
            await handle_broadcast_settings(connection, message, manager)
        
    except json.JSONDecodeError:
        logger.warning("收到无效的 JSON 消息")


async def handle_set_groups(connection, message: dict, manager) -> None:
    """处理设置监听群组"""
    filter_enabled = message.get("filter_enabled", False)
    session_ids = message.get("groups", [])
    
    logger.info(f"设置监听群组: enabled={filter_enabled}, sessions={session_ids}")
    
    # 转换 session_id 到 group_id
    target_group_ids = []
    for session_id in session_ids:
        group_id = await get_group_id_from_session_id(session_id)
        if group_id and group_id not in target_group_ids:
            target_group_ids.append(group_id)
    
    # 更新全局过滤器
    manager.set_global_filter(filter_enabled, target_group_ids)
    
    # 发送响应
    await connection.send_json({
        "type": "command_response",
        "action": "set_groups",
        "status": "success",
        "message": f"群聊监听已{'启用' if filter_enabled else '禁用'}，已选择 {len(target_group_ids)} 个群",
        "listened_groups": target_group_ids
    })
    
    # 广播过滤器更新
    await manager.broadcast_filter_update()


async def handle_set_active_group(connection, message: dict) -> None:
    """处理设置活跃群组"""
    session_id = message.get("group_id")
    
    if session_id:
        group_id = await get_group_id_from_session_id(session_id)
        if group_id:
            connection.filter.enabled = True
            connection.filter.allowed_groups = {str(group_id)}
            runtime_config.active_group_id = group_id
            runtime_config.save()
            
            await connection.send_json({
                "type": "command_response",
                "action": "set_active_group",
                "status": "success",
                "message": f"已设置监听群组: {group_id}",
                "listened_groups": [group_id]
            })
        else:
            await connection.send_json({
                "type": "command_response",
                "action": "set_active_group",
                "status": "error",
                "message": "无法获取群组ID"
            })
    else:
        connection.filter.enabled = False
        connection.filter.allowed_groups = set()
        await connection.send_json({
            "type": "command_response",
            "action": "set_active_group",
            "status": "success",
            "message": "已清除群组监听",
            "listened_groups": []
        })


async def handle_get_active_group(connection) -> None:
    """处理获取活跃群组"""
    response_group_id = None
    
    if connection.filter.enabled and connection.filter.allowed_groups:
        response_group_id = next(iter(connection.filter.allowed_groups))
    else:
        response_group_id = runtime_config.active_group_id
    
    await connection.send_json({
        "type": "active_group_info",
        "group_id": response_group_id,
        "is_filtering_this_connection": connection.filter.enabled,
        "listened_groups_this_connection": list(connection.filter.allowed_groups)
    })


async def handle_set_danmaku_speed(connection, message: dict, manager) -> None:
    """处理设置弹幕速度"""
    try:
        speed = int(message.get("speed", 10))
        if runtime_config.set_danmaku_speed(speed):
            await manager.broadcast_setting("danmaku_speed", speed)
            await connection.send_json({
                "type": "command_response",
                "action": "set_danmaku_speed",
                "status": "success",
                "message": f"弹幕速度已设置为 {speed} 秒"
            })
        else:
            raise ValueError("速度必须在 5-60 之间")
    except (ValueError, TypeError):
        await connection.send_json({
            "type": "command_response",
            "action": "set_danmaku_speed",
            "status": "error",
            "message": "无效的速度值，请输入 5-60 之间的整数"
        })


async def handle_broadcast_settings(connection, message: dict, manager) -> None:
    """处理广播设置"""
    settings_payload = message.get("settings", {})
    if isinstance(settings_payload, dict) and settings_payload:
        await manager.broadcast_to_all({
            "type": "setting_update",
            "settings": settings_payload
        })
        await connection.send_json({
            "type": "command_response",
            "action": "broadcast_settings",
            "status": "success",
            "message": "设置已广播"
        })
    else:
        await connection.send_json({
            "type": "command_response",
            "action": "broadcast_settings",
            "status": "error",
            "message": "无效的设置"
        })


# ============================================================
# HTTP 端点
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """弹幕显示页面"""
    return templates.TemplateResponse("danmaku.html", {"request": request})


@app.get("/control", response_class=HTMLResponse)
async def control_panel(request: Request):
    """控制面板页面"""
    return templates.TemplateResponse("control.html", {"request": request})


@app.get("/api/groups", response_class=JSONResponse)
async def get_groups():
    """获取群聊列表"""
    try:
        async with async_session() as session:
            query = (
                select(SessionModel)
                .where(SessionModel.level == 2)
                .order_by(SessionModel.id)
            )
            result = await session.execute(query)
            groups = result.scalars().all()
            
            # 去重
            unique_groups = {}
            for group in groups:
                group_id = group.id2
                if group_id not in unique_groups or int(group.id) < int(unique_groups[group_id]["id"]):
                    alias = runtime_config.group_aliases.get(str(group_id), "")
                    is_favorite = str(group.id) in runtime_config.favorite_groups
                    
                    unique_groups[group_id] = {
                        "id": str(group.id),
                        "group_id": group_id,
                        "alias": alias,
                        "is_favorite": is_favorite
                    }
            
            group_list = sorted(unique_groups.values(), key=lambda x: int(x["id"]))
            
            return {"status": "success", "groups": group_list}
            
    except Exception as e:
        logger.error(f"获取群聊列表出错: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/recent-messages/{group_id}", response_class=JSONResponse)
async def get_recent_messages(group_id: str):
    """获取最近消息"""
    try:
        async with async_session() as session:
            # 获取实际的群ID
            group_id_query = select(SessionModel.id2).where(SessionModel.id == int(group_id))
            result = await session.execute(group_id_query)
            actual_group_id = result.scalar_one_or_none()
            
            if not actual_group_id:
                return {"status": "error", "message": "群聊不存在"}
            
            # 查询最近消息
            query = (
                select(MessageRecord, SessionModel)
                .join(SessionModel, MessageRecord.session_persist_id == SessionModel.id)
                .where(
                    SessionModel.id2 == actual_group_id,
                    MessageRecord.type == "message",
                    MessageRecord.plain_text != ""
                )
                .order_by(MessageRecord.time.desc())
                .limit(20)
            )
            
            result = await session.execute(query)
            messages = result.fetchall()
            
            message_list = []
            for message, session_model in reversed(messages):
                content = MessageListener._process_content(message.plain_text)
                
                # 统一时间格式
                msg_time = message.time
                if msg_time.tzinfo is None:
                    msg_time = msg_time.replace(tzinfo=timezone.utc)
                
                message_list.append({
                    "message_id": message.message_id,
                    "user_id": session_model.id1,
                    "group_id": session_model.id2,
                    "time": msg_time.isoformat(),
                    "content": content
                })
            
            return {"status": "success", "messages": message_list}
            
    except Exception as e:
        logger.error(f"获取最近消息出错: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/group-alias", response_class=JSONResponse)
async def set_group_alias(data: Dict[str, Any] = Body(...)):
    """设置群聊别名"""
    try:
        group_id = str(data.get("group_id", ""))
        alias = data.get("alias", "")
        
        if not group_id:
            return {"status": "error", "message": "缺少群ID参数"}
        
        runtime_config.set_group_alias(group_id, alias)
        logger.info(f"设置群聊别名: 群ID={group_id}, 别名={alias}")
        
        return {"status": "success", "message": "群聊别名已更新"}
        
    except Exception as e:
        logger.error(f"设置群聊别名出错: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/favorite-group", response_class=JSONResponse)
async def set_favorite_group(data: Dict[str, Any] = Body(...)):
    """设置常用群组"""
    try:
        group_id = data.get("group_id")
        is_favorite = data.get("is_favorite", False)
        
        if not group_id:
            return {"status": "error", "message": "缺少群组ID"}
        
        runtime_config.toggle_favorite(group_id, is_favorite)
        
        return {"status": "success", "message": "常用群组已更新"}
        
    except Exception as e:
        logger.error(f"设置常用群组出错: {e}")
        return {"status": "error", "message": str(e)}


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=settings.host,
        port=settings.port,
        reload=True
    )
