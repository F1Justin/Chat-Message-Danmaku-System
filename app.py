"""
群聊消息弹幕系统
使用 PostgreSQL LISTEN/NOTIFY 实现实时事件驱动
"""

import asyncio
from collections import deque
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import uvicorn
from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from config import get_app_settings, get_db_settings
from connection_manager import get_connection_manager
from rooms import RuntimeProxy, broadcast_source, current_room, get_room, rooms
from logging_utils import install_redaction

install_redaction()

# ============================================================
# 配置初始化
# ============================================================

settings = get_app_settings()
db_settings = get_db_settings()
runtime_config = RuntimeProxy()

# 配置日志
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

AUTH_QUERY_PARAM = "token"
ROLE_ADMIN = "admin"
ROLE_VIEW = "view"
ROLE_ANONYMOUS = "anonymous"

# ============================================================
# 数据库配置
# ============================================================

engine = create_async_engine(db_settings.async_url, echo=False, pool_pre_ping=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


class BotModel(Base):
    """Bot 模型 - 对应 nonebot_plugin_uninfo_botmodel"""
    __tablename__ = "nonebot_plugin_uninfo_botmodel"

    id = Column(Integer, primary_key=True, index=True)
    self_id = Column(String(64), nullable=False)
    adapter = Column(String(64), nullable=False)
    scope = Column(String(64), nullable=False)


class SceneModel(Base):
    """场景模型 - 对应 nonebot_plugin_uninfo_scenemodel"""
    __tablename__ = "nonebot_plugin_uninfo_scenemodel"

    id = Column(Integer, primary_key=True, index=True)
    bot_persist_id = Column(Integer, ForeignKey("nonebot_plugin_uninfo_botmodel.id"), nullable=False)
    parent_scene_persist_id = Column(Integer, nullable=True)
    scene_id = Column(String(64), nullable=False)
    scene_type = Column(Integer, nullable=False)
    scene_data = Column(JSON, nullable=False)


class UserModel(Base):
    """用户模型 - 对应 nonebot_plugin_uninfo_usermodel"""
    __tablename__ = "nonebot_plugin_uninfo_usermodel"

    id = Column(Integer, primary_key=True, index=True)
    bot_persist_id = Column(Integer, ForeignKey("nonebot_plugin_uninfo_botmodel.id"), nullable=False)
    user_id = Column(String(64), nullable=False)
    user_data = Column(JSON, nullable=False)


class UninfoSessionModel(Base):
    """新版会话模型 - 对应 nonebot_plugin_uninfo_sessionmodel"""
    __tablename__ = "nonebot_plugin_uninfo_sessionmodel"

    id = Column(Integer, primary_key=True, index=True)
    bot_persist_id = Column(Integer, ForeignKey("nonebot_plugin_uninfo_botmodel.id"), nullable=False)
    scene_persist_id = Column(Integer, ForeignKey("nonebot_plugin_uninfo_scenemodel.id"), nullable=False)
    user_persist_id = Column(Integer, ForeignKey("nonebot_plugin_uninfo_usermodel.id"), nullable=False)
    member_data = Column(JSON, nullable=True)


class MessageRecord(Base):
    """消息记录模型 - 对应 nonebot_plugin_chatrecorder_messagerecord_v2"""
    __tablename__ = "nonebot_plugin_chatrecorder_messagerecord_v2"
    
    id = Column(Integer, primary_key=True, index=True)
    session_persist_id = Column(
        Integer, 
        ForeignKey("nonebot_plugin_uninfo_sessionmodel.id"),
        nullable=False
    )
    time = Column(DateTime(timezone=True), nullable=False)
    type = Column(String(32), nullable=False)
    message_id = Column(String(255), nullable=False)
    message = Column(JSON, nullable=False)
    plain_text = Column(Text, nullable=False)


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
        self._task = None
        self._wake = asyncio.Event()
        self._cursor = None
        self._baseline = None
        self._seen = set()
        self._seen_order = deque()
        self.connected = False

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _notified(self, connection, pid, channel, payload):
        self._wake.set()

    async def _drain(self):
        # Revisit the last 1000 IDs to cover nearby out-of-order commits.
        # Cursor and deduplication survive database reconnects within this process.
        rows = await self._connection.fetch(
            "SELECT id FROM nonebot_plugin_chatrecorder_messagerecord_v2 "
            "WHERE id > $1 ORDER BY id LIMIT 1500",
            max(self._baseline, self._cursor - 1000),
        )
        previous = self._cursor
        for row in rows:
            message_id = row["id"]
            if message_id not in self._seen:
                await self._fetch_and_broadcast_message(message_id)
                self._seen.add(message_id)
                self._seen_order.append(message_id)
                if len(self._seen_order) > 20000:
                    self._seen.discard(self._seen_order.popleft())
            self._cursor = max(self._cursor, message_id)
        return len(rows) == 1500 and self._cursor > previous

    async def _run(self):
        delay = 1
        while True:
            try:
                self._connection = await asyncpg.connect(db_settings.dsn, timeout=10)
                await self._connection.add_listener("new_message", self._notified)
                if self._cursor is None:
                    self._cursor = await self._connection.fetchval(
                        "SELECT COALESCE(MAX(id), 0) FROM nonebot_plugin_chatrecorder_messagerecord_v2"
                    )
                    self._baseline = self._cursor
                self.connected = True
                delay = 1
                while True:
                    self._wake.clear()
                    if await self._drain():
                        continue
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("消息监听连接失败，%s 秒后重试", delay)
            finally:
                self.connected = False
                if self._connection is not None:
                    try:
                        await self._connection.close(timeout=3)
                    except Exception:
                        self._connection.terminate()
                    self._connection = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _fetch_and_broadcast_message(self, message_id: int) -> None:
        """获取消息详情并广播"""
        async with async_session() as session:
            query = (
                select(MessageRecord, UninfoSessionModel, SceneModel, UserModel, BotModel)
                .join(UninfoSessionModel, MessageRecord.session_persist_id == UninfoSessionModel.id)
                .join(SceneModel, UninfoSessionModel.scene_persist_id == SceneModel.id)
                .join(UserModel, UninfoSessionModel.user_persist_id == UserModel.id)
                .join(BotModel, SceneModel.bot_persist_id == BotModel.id)
                .where(MessageRecord.id == message_id, SceneModel.scene_type == 1,
                       MessageRecord.type == "message", MessageRecord.plain_text != "")
            )
            result = await session.execute(query)
            row = result.first()
            
            if not row:
                logger.warning(f"未找到消息: id={message_id}")
                return
            
            message, session_model, scene_model, user_model, bot_model = row
            
            # 处理消息内容
            content = self._process_content(message.plain_text)
            
            # 获取时间（确保是 UTC）
            message_time = message.time
            if message_time.tzinfo is None:
                # 如果是 naive datetime，假设是 UTC
                message_time = message_time.replace(tzinfo=timezone.utc)
            
            # 缓存 session 映射

            
            # 广播弹幕
            await broadcast_source("nonebot", platform=bot_model.scope,
                group_id=scene_model.scene_id,
                user_id=user_model.user_id,
                content=content,
                message_id=f"{bot_model.scope}:{bot_model.self_id}:{message.message_id}",
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
    rooms()  # Validate room grants and credentials before accepting requests.

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
    try:
        await stats_task
    except asyncio.CancelledError:
        pass
    await message_listener.stop()
    await engine.dispose()
    logger.info("系统已关闭")


app = FastAPI(lifespan=lifespan, root_path=settings.root_path)

# 配置模板和静态文件
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ============================================================
# 访问控制
# ============================================================

def _constant_time_equals(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _role_for_token(token: Optional[str]) -> Optional[str]:
    """返回 token 对应的权限角色。"""
    if not token:
        return None
    spec = get_room().spec
    if _constant_time_equals(token, spec.admin_token):
        return ROLE_ADMIN
    if _constant_time_equals(token, spec.view_token):
        return ROLE_VIEW
    if spec.ingest_token and _constant_time_equals(token, spec.ingest_token):
        return "ingest"
    return None


def _request_auth(request: Request) -> tuple[str, Optional[str]]:
    """从 URL token、Bearer header 或 Cookie 中提取认证角色。"""
    query_token = request.query_params.get(AUTH_QUERY_PARAM)
    query_role = _role_for_token(query_token)
    if query_role:
        return query_role, query_token

    auth_header = request.headers.get("authorization", "")
    auth_scheme, _, auth_token = auth_header.partition(" ")
    if auth_scheme.lower() == "bearer":
        header_role = _role_for_token(auth_token.strip())
        if header_role:
            return header_role, None

    for role in (ROLE_ADMIN, ROLE_VIEW):
        cookie_token = request.cookies.get(_cookie_name(role))
        cookie_role = _role_for_token(cookie_token)
        if cookie_role == role:
            return cookie_role, None

    return ROLE_ANONYMOUS, None


def _websocket_auth(websocket: WebSocket) -> str:
    query_token = websocket.query_params.get(AUTH_QUERY_PARAM)
    query_role = _role_for_token(query_token)
    if query_role:
        return query_role

    for role in (ROLE_ADMIN, ROLE_VIEW):
        cookie_token = websocket.cookies.get(_cookie_name(role))
        cookie_role = _role_for_token(cookie_token)
        if cookie_role == role:
            return cookie_role

    return ROLE_ANONYMOUS


def _cookie_name(role):
    return f"{settings.auth_cookie_name}_{get_room().spec.id}_{role}"


def _can_admin(role: str) -> bool:
    return role == ROLE_ADMIN


def _can_view(role: str) -> bool:
    return role in {ROLE_ADMIN, ROLE_VIEW}


def _is_allowed_client(client_host: Optional[str]) -> bool:
    return "*" in settings.allowed_hosts or client_host in settings.allowed_hosts


def _strip_auth_token(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != AUTH_QUERY_PARAM],
        doseq=True
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _set_auth_cookie(response, request: Request, token: str) -> None:
    if _role_for_token(token) == "ingest":
        return
    forwarded_proto = request.headers.get("x-forwarded-proto")
    is_secure = (forwarded_proto or request.url.scheme) == "https"
    response.set_cookie(
        key=_cookie_name(_role_for_token(token)),
        value=token,
        max_age=60 * 60 * 24 * 30,
        path=request.scope.get("root_path", "").rstrip("/") or "/",
        httponly=True,
        secure=is_secure,
        samesite="lax",
    )


def _unauthorized(message: str = "Unauthorized") -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=401)


class RoomContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        params = dict(parse_qsl(scope.get("query_string", b"").decode()))
        room = rooms().get(params.get("room", "default"))
        if room is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "Room not found"}, status_code=404)(scope, receive, send)
            return
        token = current_room.set(room)
        try:
            await self.app(scope, receive, send)
        finally:
            current_room.reset(token)


@app.middleware("http")
async def access_control_middleware(request: Request, call_next):
    """限制来源并保护公网管理入口。"""
    client_host = request.client.host if request.client else None
    if not _is_allowed_client(client_host):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)

    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and urlsplit(origin).netloc != request.headers.get("host"):
        return JSONResponse({"detail": "Origin forbidden"}, status_code=403)
    role, valid_query_token = _request_auth(request)
    path = request.scope.get("path", request.url.path)
    root_path = request.scope.get("root_path", "").rstrip("/")
    if root_path and (path == root_path or path.startswith(root_path + "/")):
        path = path[len(root_path):] or "/"

    if path == "/control" and not _can_admin(role):
        return _unauthorized("Admin token required")
    if path == "/api/inject-danmaku" and role not in {ROLE_ADMIN, "ingest"}:
        return _unauthorized("Ingest token required")
    if path.startswith("/api/") and path != "/api/inject-danmaku" and not _can_admin(role):
        return _unauthorized("Admin token required")
    if path == "/" and not _can_view(role):
        return _unauthorized("View token required")

    if request.method == "GET" and valid_query_token:
        response = RedirectResponse(_strip_auth_token(str(request.url)), status_code=303)
        _set_auth_cookie(response, request, valid_query_token)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    response = await call_next(request)
    if valid_query_token:
        _set_auth_cookie(response, request, valid_query_token)
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(RoomContextMiddleware)


# ============================================================
# 后台任务
# ============================================================

async def periodic_stats_broadcast():
    """定期广播统计信息"""
    while True:
        try:
            await asyncio.sleep(10)
            for room in rooms().values():
                await room.manager.broadcast_stats()
                await room.manager.broadcast_to_all({
                    "type": "source_status", "connected": message_listener.connected
                    if room.spec.source == "nonebot" else None,
                })
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"广播统计信息时出错: {e}")


# ============================================================
# 辅助函数
# ============================================================

async def get_group_id_from_session_id(session_id: str) -> Optional[str]:
    """从房间授权平台的 session_id 获取 group_id。"""
    if get_room().spec.source != "nonebot":
        return str(session_id) if get_room().permits(session_id) else None
    manager = get_connection_manager()
    
    # 尝试从缓存获取
    cached = manager.get_cached_group_id(session_id)
    if cached:
        return cached
    
    # 从数据库查询
    try:
        async with async_session() as session:
            query = (
                select(SceneModel.scene_id)
                .join(UninfoSessionModel, UninfoSessionModel.scene_persist_id == SceneModel.id)
                .join(BotModel, SceneModel.bot_persist_id == BotModel.id)
                .where(UninfoSessionModel.id == int(session_id), BotModel.scope == get_room().spec.platform)
            )
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
    if not _is_allowed_client(client_host):
        await websocket.close(code=1008, reason="Forbidden")
        return

    origin = websocket.headers.get("origin")
    if origin and urlsplit(origin).netloc != websocket.headers.get("host"):
        await websocket.close(code=1008, reason="Origin forbidden")
        return
    auth_role = _websocket_auth(websocket)
    if not _can_view(auth_role):
        await websocket.close(code=1008, reason="View token required")
        return
    
    manager = get_connection_manager()
    connection = await manager.connect(websocket, auth_role=auth_role)
    
    try:
        # 发送初始状态
        await connection.send_json({
            "type": "connection",
            "message": "连接成功",
            "settings": {
                "danmaku_speed": runtime_config.danmaku_speed,
                "display": runtime_config.display_settings
            }
        })
        
        await connection.send_json({"type": "source_status", "connected": message_listener.connected
                                    if get_room().spec.source == "nonebot" else None})

        # 发送当前过滤状态
        await connection.send_json({
            "type": "broadcast_filter_update",
            "filter_enabled": manager.global_filter_enabled,
            "allowed_groups": manager.global_allowed_groups
        })

        await connection.send_json({
            "type": "screen_clear_update",
            "enabled": manager.screen_cleared
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

        if not _can_admin(getattr(connection, "auth_role", ROLE_ANONYMOUS)):
            await connection.send_json({
                "type": "command_response",
                "action": message.get("action", "unknown"),
                "status": "error",
                "message": "需要管理员权限"
            })
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

        elif action == "set_screen_clear":
            await handle_set_screen_clear(connection, message, manager)
        
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
    
    if any(not get_room().permits(g) for g in target_group_ids):
        await connection.send_json({"type": "command_response", "action": "set_groups",
                                    "status": "error", "message": "群聊未授权"})
        return
    # Empty selection means silence; room grants are never disabled.
    manager.set_global_filter(True, target_group_ids)
    
    runtime_config.selected_groups = manager.global_allowed_groups
    runtime_config.save()

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
    # Legacy command follows the same room-wide permission checks as the UI.
    session_id = message.get("group_id")
    await handle_set_groups(connection, {"filter_enabled": True,
                           "groups": [session_id] if session_id else []}, get_connection_manager())


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
    bounds = {"maxDanmaku": (1, 1000), "fontSize": (12, 160)}
    if (isinstance(settings_payload, dict) and settings_payload
            and all(k in bounds and type(v) is int and bounds[k][0] <= v <= bounds[k][1]
                    for k, v in settings_payload.items())):
        runtime_config.display_settings.update(settings_payload)
        runtime_config.save()
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


async def handle_set_screen_clear(connection, message: dict, manager) -> None:
    """处理全局清屏/解除清屏。"""
    enabled = bool(message.get("enabled", False))
    runtime_config.screen_cleared = enabled
    runtime_config.save()
    manager.set_screen_cleared(enabled)
    await manager.broadcast_screen_clear_update()
    await connection.send_json({
        "type": "command_response",
        "action": "set_screen_clear",
        "status": "success",
        "message": "已清屏，新的弹幕会被暂时屏蔽" if enabled else "已解除清屏，恢复显示新弹幕"
    })


# ============================================================
# HTTP 端点
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """弹幕显示页面"""
    return templates.TemplateResponse(
        request,
        name="danmaku.html",
        context={"request": request, "root_path": request.scope.get("root_path", "").rstrip("/"), "room_id": get_room().spec.id},
    )


@app.get("/control", response_class=HTMLResponse)
async def control_panel(request: Request):
    """控制面板页面"""
    return templates.TemplateResponse(
        request,
        name="control.html",
        context={
            "request": request,
            "root_path": request.scope.get("root_path", "").rstrip("/"),
            "view_token": get_room().spec.view_token,
            "room_id": get_room().spec.id,
            "room_name": get_room().spec.name or get_room().spec.id,
        },
    )


@app.get("/api/status")
async def status():
    return {"room": get_room().spec.id, "source": get_room().spec.source,
            "database_connected": message_listener.connected if get_room().spec.source == "nonebot" else None,
            "connections": get_room().manager.connection_count}


@app.get("/api/groups", response_class=JSONResponse)
async def get_groups():
    """获取群聊列表"""
    if get_room().spec.source != "nonebot":
        return {"status": "success", "groups": [
            {"id": group, "group_id": group, "alias": runtime_config.group_aliases.get(group, ""),
             "is_favorite": group in runtime_config.favorite_groups}
            for group in get_room().spec.allowed_groups]}
    try:
        async with async_session() as session:
            query = (
                select(UninfoSessionModel, SceneModel)
                .join(SceneModel, UninfoSessionModel.scene_persist_id == SceneModel.id)
                .join(BotModel, SceneModel.bot_persist_id == BotModel.id)
                .where(SceneModel.scene_type == 1, BotModel.scope == get_room().spec.platform,
                       SceneModel.scene_id.in_(get_room().spec.allowed_groups))
                .order_by(UninfoSessionModel.id)
            )
            result = await session.execute(query)
            groups = result.fetchall()
            
            # 去重
            unique_groups = {}
            for session_model, scene_model in groups:
                group_id = scene_model.scene_id
                if group_id not in unique_groups or int(session_model.id) < int(unique_groups[group_id]["id"]):
                    alias = runtime_config.group_aliases.get(str(group_id), "")
                    is_favorite = str(session_model.id) in runtime_config.favorite_groups
                    
                    unique_groups[group_id] = {
                        "id": str(session_model.id),
                        "group_id": group_id,
                        "alias": alias,
                        "is_favorite": is_favorite
                    }
            
            group_list = sorted(unique_groups.values(), key=lambda x: int(x["id"]))
            
            return {"status": "success", "groups": group_list}
            
    except Exception as e:
        logger.error(f"获取群聊列表出错: {e}")
        return {"status": "error", "message": "操作失败，请查看服务端日志"}


@app.get("/api/recent-messages/{group_id}", response_class=JSONResponse)
async def get_recent_messages(group_id: str):
    """获取最近消息"""
    try:
        async with async_session() as session:
            # 获取实际的群ID
            group_id_query = (
                select(SceneModel.scene_id)
                .join(UninfoSessionModel, UninfoSessionModel.scene_persist_id == SceneModel.id)
                .join(BotModel, SceneModel.bot_persist_id == BotModel.id)
                .where(UninfoSessionModel.id == int(group_id), BotModel.scope == get_room().spec.platform)
            )
            result = await session.execute(group_id_query)
            actual_group_id = result.scalar_one_or_none()
            
            if not actual_group_id or not get_room().permits(actual_group_id) or get_room().spec.source != "nonebot":
                return {"status": "error", "message": "群聊不存在"}
            
            # 查询最近消息
            query = (
                select(MessageRecord, SceneModel, UserModel, BotModel)
                .join(UninfoSessionModel, MessageRecord.session_persist_id == UninfoSessionModel.id)
                .join(SceneModel, UninfoSessionModel.scene_persist_id == SceneModel.id)
                .join(UserModel, UninfoSessionModel.user_persist_id == UserModel.id)
                .join(BotModel, SceneModel.bot_persist_id == BotModel.id)
                .where(
                    BotModel.scope == get_room().spec.platform,
                    SceneModel.scene_type == 1,
                    SceneModel.scene_id == actual_group_id,
                    MessageRecord.type == "message",
                    MessageRecord.plain_text != ""
                )
                .order_by(MessageRecord.time.desc())
                .limit(20)
            )
            
            result = await session.execute(query)
            messages = result.fetchall()
            
            message_list = []
            for message, scene_model, user_model, bot_model in reversed(messages):
                content = MessageListener._process_content(message.plain_text)
                
                # 统一时间格式
                msg_time = message.time
                if msg_time.tzinfo is None:
                    msg_time = msg_time.replace(tzinfo=timezone.utc)
                
                message_list.append({
                    "message_id": f"{bot_model.scope}:{bot_model.self_id}:{message.message_id}",
                    "user_id": user_model.user_id,
                    "group_id": scene_model.scene_id,
                    "time": msg_time.isoformat(),
                    "content": content
                })
            
            return {"status": "success", "messages": message_list}
            
    except Exception as e:
        logger.error(f"获取最近消息出错: {e}")
        return {"status": "error", "message": "操作失败，请查看服务端日志"}


@app.post("/api/inject-danmaku", response_class=JSONResponse)
async def inject_danmaku(request: Request, data: Dict[str, Any] = Body(...)):
    """从外部系统注入一条即时弹幕。"""
    if not isinstance(data.get("content"), str):
        return JSONResponse({"detail": "Content must be text"}, status_code=400)
    content = data["content"].strip()
    if not content:
        return JSONResponse({"status": "error", "message": "缺少弹幕内容"}, status_code=400)

    room = get_room()
    if (data.get("source", room.spec.source) != room.spec.source
            or data.get("platform", room.spec.platform) != room.spec.platform
            or data.get("kind", "message") != "message"):
        return JSONResponse({"detail": "Source or event type not authorized"}, status_code=403)
    role, _ = _request_auth(request)
    if role == "ingest" and not data.get("message_id"):
        return JSONResponse({"detail": "Stable message_id required"}, status_code=400)
    now = datetime.now(timezone.utc)
    message_id = str(data.get("message_id") or f"inject-{int(now.timestamp() * 1000)}")
    group_id = str(data.get("group_id") or "external")
    user_id = str(data.get("user_id") or "external")
    if len(message_id) > 255 or len(user_id) > 128:
        return JSONResponse({"detail": "Identifier too long"}, status_code=400)
    if not get_room().permits(group_id):
        return JSONResponse({"detail": "Group not authorized"}, status_code=403)
    if len(content) > 2000:
        return JSONResponse({"detail": "Content exceeds 2000 characters"}, status_code=400)

    if message_id in room.ingested_ids:
        return {"status": "success", "message_id": message_id, "sent": 0, "duplicate": True}
    # Claim before awaiting broadcast, so concurrent retries cannot send twice.
    room.ingested_ids[message_id] = None
    if len(room.ingested_ids) > 20000:
        room.ingested_ids.popitem(last=False)
    sent_count = await get_connection_manager().broadcast_danmaku(
        group_id=group_id,
        user_id=user_id,
        content=content,
        message_id=message_id,
        timestamp=now,
        force=False,
    )

    return {
        "status": "success",
        "message_id": message_id,
        "sent": sent_count,
        "force": False,
    }


@app.post("/api/group-alias", response_class=JSONResponse)
async def set_group_alias(data: Dict[str, Any] = Body(...)):
    """设置群聊别名"""
    try:
        group_id = str(data.get("group_id", ""))
        alias = data.get("alias", "")
        
        if not group_id:
            return {"status": "error", "message": "缺少群ID参数"}
        
        if not get_room().permits(group_id):
            return JSONResponse({"detail": "Group not authorized"}, status_code=403)
        runtime_config.set_group_alias(group_id, alias)
        logger.info(f"设置群聊别名: 群ID={group_id}, 别名={alias}")
        
        return {"status": "success", "message": "群聊别名已更新"}
        
    except Exception as e:
        logger.error(f"设置群聊别名出错: {e}")
        return {"status": "error", "message": "操作失败，请查看服务端日志"}


@app.post("/api/favorite-group", response_class=JSONResponse)
async def set_favorite_group(data: Dict[str, Any] = Body(...)):
    """设置常用群组"""
    try:
        group_id = data.get("group_id")
        is_favorite = data.get("is_favorite", False)
        
        if not group_id:
            return {"status": "error", "message": "缺少群组ID"}
        
        actual_group = await get_group_id_from_session_id(str(group_id))
        if not actual_group or not get_room().permits(actual_group):
            return JSONResponse({"detail": "Group not authorized"}, status_code=403)
        runtime_config.toggle_favorite(group_id, is_favorite)
        
        return {"status": "success", "message": "常用群组已更新"}
        
    except Exception as e:
        logger.error(f"设置常用群组出错: {e}")
        return {"status": "error", "message": "操作失败，请查看服务端日志"}


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
