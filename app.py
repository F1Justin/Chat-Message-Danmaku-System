from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Query, Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
import asyncio
import json
import sqlalchemy
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy import Column, Integer, String, Text, DateTime, select, JSON, ForeignKey, join, func, and_, desc
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import sys
import logging
from typing import List, Dict, Tuple, Any, Optional
import time

# 加载环境变量
load_dotenv()

# 日志配置：默认 INFO，可通过环境变量 LOG_LEVEL 调整（DEBUG/INFO/WARNING/ERROR）
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))

# 数据库配置
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

# 检查必要的环境变量
required_env_vars = ["DB_USER", "DB_PASSWORD", "DB_HOST", "DB_NAME"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    print(f"错误: 缺少必要的环境变量: {', '.join(missing_vars)}")
    print("请创建.env文件或设置环境变量")
    sys.exit(1)

# 设置默认端口
DB_PORT = DB_PORT or "5432"

SQLALCHEMY_DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# 创建异步引擎
engine = create_async_engine(SQLALCHEMY_DATABASE_URL)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

# 定义会话模型 - 对应 nonebot_plugin_session_orm_sessionmodel
class SessionModel(Base):
    __tablename__ = "nonebot_plugin_session_orm_sessionmodel"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(String(64), nullable=False)
    bot_type = Column(String(32), nullable=False)
    platform = Column(String(32), nullable=False)
    level = Column(Integer, nullable=False)
    id1 = Column(String(64), nullable=False)  # 一般是用户ID
    id2 = Column(String(64), nullable=False)  # 如果level=2，这个是群ID
    id3 = Column(String(64), nullable=False)
    
    # 定义反向关系
    messages = relationship("MessageRecord", back_populates="session")

# 定义消息记录模型 - 对应 nonebot_plugin_chatrecorder_messagerecord
class MessageRecord(Base):
    __tablename__ = "nonebot_plugin_chatrecorder_messagerecord"
    
    id = Column(Integer, primary_key=True, index=True)
    session_persist_id = Column(Integer, ForeignKey("nonebot_plugin_session_orm_sessionmodel.id"), nullable=False)
    time = Column(DateTime, nullable=False)
    type = Column(String(32), nullable=False)
    message_id = Column(String(255), nullable=False)
    message = Column(JSON, nullable=False)
    plain_text = Column(Text, nullable=False)
    
    # 定义外键关系
    session = relationship("SessionModel", back_populates="messages")

app = FastAPI()

# 配置模板和静态文件
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# 仅允许本机访问
ALLOWED_LOCALHOSTS = {"127.0.0.1", "::1", "localhost"}

@app.middleware("http")
async def restrict_localhost_middleware(request: Request, call_next):
    client_host = request.client.host if request.client else None
    if client_host not in ALLOWED_LOCALHOSTS:
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)

# 存储活跃的WebSocket连接，及其关联的群聊过滤
active_connections: List[Tuple[WebSocket, Dict[str, Any]]] = []

# 存储活跃连接的群聊过滤设置
connection_filters = {}

# 存储session_id到group_id的映射缓存
session_to_group_map: Dict[str, str] = {}

# 全局过滤状态（用于新连接继承当前选择的群组过滤）
global_filter_enabled: bool = False
global_allowed_groups: List[str] = []

# 全局活跃群组ID
# active_group_id: Optional[str] = None # This will now be more of a UI hint, not a global filter.
# Still loaded and saved for UI persistence if desired, but not used for filtering new connections.

# 群组别名配置
group_aliases: Dict[str, str] = {}

# 常用群组列表
favorite_groups: List[str] = []

# 弹幕速度（秒）
danmaku_speed: int = 10  # 默认10秒

# 配置文件路径
CONFIG_FILE = "config.json"

# 加载配置
def load_config():
    global group_aliases, favorite_groups, active_group_id, danmaku_speed
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                group_aliases = config.get('group_aliases', {})
                favorite_groups = config.get('favorite_groups', [])
                active_group_id = config.get('active_group_id') # Keep loading for UI hint
                danmaku_speed = config.get('danmaku_speed', 10)
                logging.info(f"已加载配置: 别名={len(group_aliases)} 常用群={len(favorite_groups)} 速度={danmaku_speed}s 提示活跃群={active_group_id}")
        else:
            logging.info("配置文件不存在，使用默认设置")
    except Exception as e:
        logging.error(f"加载配置出错: {e}")

# 保存配置
def save_config():
    global danmaku_speed, active_group_id # ensure active_group_id can be saved for UI hint
    try:
        config = {
            'group_aliases': group_aliases,
            'favorite_groups': favorite_groups,
            'active_group_id': active_group_id, # Keep saving for UI hint
            'danmaku_speed': danmaku_speed
        }
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        logging.info("配置已保存")
    except Exception as e:
        logging.error(f"保存配置出错: {e}")

# 广播统计数据给所有连接
async def broadcast_stats():
    stats = {
        "type": "stats",
        "connections": len(active_connections),
    }
    await broadcast_to_all(json.dumps(stats))

# 广播设置给所有连接
async def broadcast_setting(setting_key: str, setting_value: Any):
    """广播单个设置项给所有连接"""
    message = {
        "type": "setting_update",
        "key": setting_key,
        "value": setting_value
    }
    await broadcast_to_all(json.dumps(message))
    logging.info(f"已广播设置更新: {setting_key}={setting_value}")

# 广播一组设置给所有连接
async def broadcast_settings_to_all(settings: Dict[str, Any]):
    message = {
        "type": "setting_update",
        "settings": settings
    }
    await broadcast_to_all(json.dumps(message))
    logging.info(f"已广播批量设置更新: {settings}")

# 广播消息给所有连接
async def broadcast_to_all(message: str):
    for connection, _ in active_connections:
        try:
            await connection.send_text(message)
        except Exception as e:
            print(f"广播消息失败: {e}")

# 广播活跃群组变更消息
# async def broadcast_group_change(group_id: str): # This function will be removed or significantly changed
#     """广播群组变更消息到所有连接"""
#     message = {
#         "type": "active_group",
#         "group_id": group_id
#     }
#     await broadcast_to_all(json.dumps(message))
#     print(f"已广播群组变更消息: {group_id}")

# 获取群聊ID
async def get_group_id_from_session_id(session_id: str) -> Optional[str]:
    # 尝试从缓存获取
    if session_id in session_to_group_map:
        group_id = session_to_group_map[session_id]
        logging.debug(f"从缓存获取映射: session_id={session_id} -> group_id={group_id}")
        return group_id
    
    # 如果缓存中没有，从数据库获取
    try:
        async with async_session() as session:
            query = select(SessionModel.id2).where(SessionModel.id == int(session_id))
            result = await session.execute(query)
            group_id = result.scalar_one_or_none()
            
            if group_id:
                session_to_group_map[session_id] = group_id
                logging.debug(f"从数据库获取映射: session_id={session_id} -> group_id={group_id}")
                return group_id
            else:
                logging.debug(f"未找到映射: session_id={session_id}")
                return None
    except Exception as e:
        logging.error(f"获取群聊ID出错: {e}")
        return None

# 处理WebSocket消息
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global active_group_id, danmaku_speed, global_filter_enabled, global_allowed_groups # Declare globals

    initial_ui_hint_group_id = active_group_id 
    current_danmaku_speed = danmaku_speed # Read global danmaku_speed for initial settings

    # 鉴权：仅允许本机访问 WebSocket
    client_host = websocket.client.host if websocket.client else None
    if client_host not in ALLOWED_LOCALHOSTS:
        try:
            await websocket.close(code=1008, reason="Forbidden")
        except Exception:
            pass
        return

    await websocket.accept()
    logging.info("connection open")
    
    connection_filter = {
        "enabled": False,
        "allowed_groups": []
    }
    
    try:
        active_connections.append((websocket, connection_filter))
        # 新连接继承全局过滤状态
        connection_filter["enabled"] = global_filter_enabled
        connection_filter["allowed_groups"] = list(global_allowed_groups)
        
        await websocket.send_text(json.dumps({
            "type": "connection",
            "message": "连接成功",
            "settings": {
                "danmaku_speed": current_danmaku_speed # Use the locally captured speed
            }
        }))
        # 将当前全局过滤状态单播给新连接（便于客户端UI与过滤同步）
        await websocket.send_text(json.dumps({
            "type": "broadcast_filter_update",
            "filter_enabled": global_filter_enabled,
            "allowed_groups": global_allowed_groups
        }))
        
        if initial_ui_hint_group_id:
            await websocket.send_text(json.dumps({
                "type": "last_focused_group_hint",
                "group_id": initial_ui_hint_group_id
            }))
        
        await broadcast_stats()
        
        while True:
            try:
                data = await websocket.receive_text()
                message = json.loads(data)
                
                if message["type"] == "command":
                    action = message.get("action")
                    if action == "set_groups":
                        filter_enabled_req = message.get("filter_enabled", False)
                        session_ids_req = message.get("groups", [])
                        
                        logging.info(f"收到 set_groups 命令: 启用={filter_enabled_req}, 群列表(session_ids)={session_ids_req}")
                        
                        # 将session_id映射到实际的group_id
                        target_group_ids = []
                        for session_id_val in session_ids_req:
                            group_id_val = await get_group_id_from_session_id(session_id_val)
                            if group_id_val is not None:
                                gid_str = str(group_id_val)
                                if gid_str not in target_group_ids:
                                    target_group_ids.append(gid_str)
                        
                        # 更新所有活动连接的过滤器设置
                        # 这是关键：确保所有连接（包括主弹幕页和控制面板预览）都采用相同的过滤规则
                        updated_connection_count = 0
                        for conn, conn_filter_ref in active_connections: # conn_filter_ref 是对字典的可变引用
                            conn_filter_ref["enabled"] = filter_enabled_req
                            conn_filter_ref["allowed_groups"] = target_group_ids # 使用实际的group_id列表
                            updated_connection_count += 1

                        # 更新全局过滤状态，供后续新连接继承
                        global_filter_enabled = filter_enabled_req
                        global_allowed_groups = list(target_group_ids)
                        
                        logging.info(f"已为 {updated_connection_count} 个活动连接更新过滤器: enabled={filter_enabled_req}, allowed_group_ids={target_group_ids}")
                        
                        # 向发起命令的客户端发送确认
                        await websocket.send_text(json.dumps({
                            "type": "command_response", "action": "set_groups", "status": "success",
                            "message": f"群聊监听已全局{'启用' if filter_enabled_req else '禁用'}, 已选择 {len(target_group_ids)} 个群",
                            "listened_groups": target_group_ids
                        }))

                        # 广播过滤器更新给所有连接的客户端 (主要供客户端JS更新其本地状态，如danmaku.html)
                        # 虽然后端过滤器已经为所有连接更新了，但这个广播对客户端侧的同步仍然有用。
                        filter_update_payload = {
                            "type": "broadcast_filter_update",
                            "filter_enabled": filter_enabled_req,
                            "allowed_groups": target_group_ids 
                        }
                        await broadcast_to_all(json.dumps(filter_update_payload))
                        logging.info(f"已广播客户端过滤器更新通知: enabled={filter_enabled_req}, groups={target_group_ids}")
                    elif action == "broadcast_settings":
                        settings_payload = message.get("settings", {})
                        if isinstance(settings_payload, dict) and len(settings_payload) > 0:
                            await broadcast_settings_to_all(settings_payload)
                            await websocket.send_text(json.dumps({
                                "type": "command_response",
                                "action": "broadcast_settings",
                                "status": "success",
                                "message": "设置已广播",
                                "debug_info": {"settings": settings_payload}
                            }))
                        else:
                            await websocket.send_text(json.dumps({
                                "type": "command_response",
                                "action": "broadcast_settings",
                                "status": "error",
                                "message": "无效的设置"
                            }))
                    elif action == "set_active_group":
                        session_id_param = message.get("group_id")
                        if session_id_param:
                            target_group_id_val = await get_group_id_from_session_id(session_id_param)
                            if target_group_id_val:
                                connection_filter["enabled"] = True
                                connection_filter["allowed_groups"] = [str(target_group_id_val)]
                                logging.info(f"连接 {websocket.client} 监听群: {target_group_id_val}")
                                
                                # active_group_id is already global due to declaration at function top
                                active_group_id = target_group_id_val # Modifies the global active_group_id
                                save_config()

                                await websocket.send_text(json.dumps({
                                    "type": "command_response",
                                    "action": "set_active_group",
                                    "status": "success",
                                    "message": f"您现在只监听群组: {target_group_id_val}",
                                    "listened_groups": [target_group_id_val]
                                }))
                            else:
                                await websocket.send_text(json.dumps({
                                    "type": "command_response",
                                    "action": "set_active_group",
                                    "status": "error",
                                    "message": "无法获取群组ID"
                                }))
                        else:
                            # Clearing active group for this connection
                            connection_filter["enabled"] = False
                            connection_filter["allowed_groups"] = []
                            # No global active_group_id change when one client clears filter
                            await websocket.send_text(json.dumps({
                                    "type": "command_response",
                                    "action": "set_active_group",
                                    "status": "success",
                                    "message": "已清除此连接的群组监听",
                                    "listened_groups": []
                            }))
                            
                    elif action == "get_active_group":
                        response_group_id_to_send = None
                        # active_group_id is already global, so this reads the global value
                        current_global_hint_for_get = active_group_id 
                        
                        if connection_filter["enabled"] and connection_filter["allowed_groups"]:
                            response_group_id_to_send = connection_filter["allowed_groups"][0]
                        else:
                            response_group_id_to_send = current_global_hint_for_get
                        
                        await websocket.send_text(json.dumps({
                            "type": "active_group_info",
                            "group_id": response_group_id_to_send,
                            "is_filtering_this_connection": connection_filter["enabled"],
                            "listened_groups_this_connection": connection_filter["allowed_groups"]
                        }))
                    elif action == "set_danmaku_speed":
                        new_speed = message.get("speed")
                        try:
                            speed_val = int(new_speed)
                            if 5 <= speed_val <= 60:
                                danmaku_speed = speed_val
                                save_config()
                                await broadcast_setting("danmaku_speed", danmaku_speed)
                                await websocket.send_text(json.dumps({
                                    "type": "command_response",
                                    "action": "set_danmaku_speed",
                                    "status": "success",
                                    "message": f"弹幕速度已设置为 {danmaku_speed} 秒"
                                }))
                            else:
                                raise ValueError("速度必须在 5 到 60 之间")
                        except (ValueError, TypeError):
                            await websocket.send_text(json.dumps({
                                "type": "command_response",
                                "action": "set_danmaku_speed",
                                "status": "error",
                                "message": "无效的速度值，请输入5到60之间的整数"
                            }))
                logging.debug(f"WebSocket loop processed message: {data[:50]}...")
            except json.JSONDecodeError:
                logging.warning("Received invalid JSON message")
            except WebSocketDisconnect:
                logging.info("WS Disconnected in receive loop, breaking.")
                break
            except RuntimeError as re:
                if "Cannot call \"receive\" once a disconnect message has been received" in str(re):
                    logging.warning(f"RuntimeError indicating disconnect in loop, breaking: {re}")
                    break
                logging.error(f"Other RuntimeError in loop, breaking: {re}", exc_info=True)
                break 
            except Exception as e_loop:
                logging.error(f"Generic error in receive loop, breaking: {e_loop}", exc_info=True)
                break

    except WebSocketDisconnect as e:
        logging.info(f"WebSocket connection closed (outer catch). Code: {e.code}, Reason: {e.reason}")
    except Exception as e:
        logging.error(f"Unhandled WebSocket error (outer catch): {str(e)}", exc_info=True)
    finally:
        if (websocket, connection_filter) in active_connections:
            active_connections.remove((websocket, connection_filter))
            logging.info(f"Connection removed in finally. Current connections: {len(active_connections)}")
            # Broadcast stats only if list was modified and is not empty, or handle error if broadcast fails
            try:
                await broadcast_stats()
            except Exception as broadcast_exc:
                logging.error(f"Error broadcasting stats in finally: {broadcast_exc}")
        else:
            logging.info("Connection already removed or was never fully added.")
        logging.info("Exiting websocket_endpoint handler.")

# 主页
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("danmaku.html", {"request": request})

# 控制面板
@app.get("/control", response_class=HTMLResponse)
async def control_panel(request: Request):
    return templates.TemplateResponse("control.html", {"request": request})

# 获取群聊列表
@app.get("/api/groups", response_class=JSONResponse)
async def get_groups():
    try:
        async with async_session() as session:
            # 查询所有群聊会话
            query = select(SessionModel).where(SessionModel.level == 2).order_by(SessionModel.id)
            result = await session.execute(query)
            groups = result.scalars().all()
            
            # 使用字典来确保按group_id去重
            unique_groups = {}
            for group in groups:
                group_id = group.id2
                # 如果这个群ID还没有添加过，或者当前session_id更小（优先使用较早的记录）
                if group_id not in unique_groups or int(group.id) < int(unique_groups[group_id]["id"]):
                    # 获取群别名
                    alias = group_aliases.get(str(group_id), "")
                    # 检查是否为常用群组
                    is_favorite = str(group.id) in favorite_groups
                    
                    unique_groups[group_id] = {
                        "id": str(group.id),
                        "group_id": group_id,
                        "alias": alias,
                        "is_favorite": is_favorite
                    }
            
            # 将字典转换为列表
            group_list = list(unique_groups.values())
            
            # 按id排序
            group_list.sort(key=lambda x: int(x["id"]))
            
            return {
                "status": "success",
                "groups": group_list
                # "active_group_id": active_group_id # Removed global active_group_id from this API response
            }
    except Exception as e:
        print(f"获取群聊列表出错: {e}")
        return {
            "status": "error",
            "message": str(e)
        }

# 获取最近消息
@app.get("/api/recent-messages/{group_id}", response_class=JSONResponse)
async def get_recent_messages(group_id: str):
    try:
        async with async_session() as session:
            # 获取群ID
            group_id_query = select(SessionModel.id2).where(SessionModel.id == int(group_id))
            group_id_result = await session.execute(group_id_query)
            actual_group_id = group_id_result.scalar_one_or_none()
            
            if not actual_group_id:
                return {
                    "status": "error",
                    "message": "群聊不存在"
                }
            
            # 查询最近消息
            query = select(MessageRecord, SessionModel).join(
                SessionModel, MessageRecord.session_persist_id == SessionModel.id
            ).where(
                SessionModel.id2 == actual_group_id,
                MessageRecord.type == "message",
                MessageRecord.plain_text != ""
            ).order_by(
                MessageRecord.time.desc()
            ).limit(20)
            
            result = await session.execute(query)
            messages = result.fetchall()
            
            # 处理消息
            message_list = []
            for message, session_model in reversed(messages):  # 反转顺序，从旧到新
                content = message.plain_text
                
                # 简单处理一下消息内容，去除可能的前缀
                if ": " in content and content.count(": ") == 1:
                    content = content.split(": ", 1)[1]
                elif ":" in content and content.count(":") == 1:
                    content = content.split(":", 1)[1]
                
                message_list.append({
                    "message_id": message.message_id,
                    "user_id": session_model.id1,
                    "group_id": session_model.id2,
                    "time": message.time.isoformat(),
                    "content": content
                })
            
            return {
                "status": "success",
                "messages": message_list
            }
    except Exception as e:
        print(f"获取最近消息出错: {e}")
        return {
            "status": "error",
            "message": str(e)
        }

# 设置群聊别名
@app.post("/api/group-alias", response_class=JSONResponse)
async def set_group_alias(data: Dict[str, Any] = Body(...)):
    try:
        group_id = str(data.get("group_id"))
        alias = data.get("alias", "")
        
        if not group_id:
            return {
                "status": "error",
                "message": "缺少群ID参数"
            }
        
        # 更新别名
        group_aliases[group_id] = alias
        
        # 保存配置
        save_config()
        
        print(f"设置群聊别名: 群ID={group_id}, 别名={alias}")
        
        return {
            "status": "success",
            "message": "群聊别名已更新"
        }
    except Exception as e:
        print(f"设置群聊别名出错: {e}")
        return {
            "status": "error",
            "message": str(e)
        }

# 设置常用群组
@app.post("/api/favorite-group", response_class=JSONResponse)
async def set_favorite_group(data: Dict[str, Any] = Body(...)):
    try:
        group_id = data.get("group_id")
        is_favorite = data.get("is_favorite", False)
        
        if not group_id:
            return {
                "status": "error",
                "message": "缺少群组ID"
            }
        
        # 更新常用群组
        if is_favorite and group_id not in favorite_groups:
            favorite_groups.append(group_id)
        elif not is_favorite and group_id in favorite_groups:
            favorite_groups.remove(group_id)
        
        save_config()
        
        return {
            "status": "success",
            "message": "常用群组已更新"
        }
    except Exception as e:
        print(f"设置常用群组出错: {e}")
        return {
            "status": "error",
            "message": str(e)
        }

# 处理接收到的消息
async def check_for_new_messages():
    # 获取数据库中最新消息的时间，避免依赖本地系统时间
    try:
        async with async_session() as session:
            query = select(func.max(MessageRecord.time)).select_from(MessageRecord)
            result = await session.execute(query)
            latest_time = result.scalar_one_or_none()
            
            if latest_time is None:
                logging.info("数据库中没有消息，使用当前时间")
                # 如果数据库为空，使用当前时间
                initial_check_time = datetime.utcnow() - timedelta(hours=1)
            else:
                logging.debug(f"数据库最新消息时间: {latest_time} (UTC时间)")
                # 重要修改：检查数据库时间是否在未来
                if latest_time > datetime.utcnow():
                    logging.warning("数据库时间在未来！使用当前时间回退24小时作为初始检查时间")
                    initial_check_time = datetime.utcnow() - timedelta(hours=24)
                else:
                    # 使用数据库中最新消息的时间作为起始检查时间
                    initial_check_time = latest_time
    except Exception as e:
        logging.error(f"获取最新消息时间出错: {e}")
        # 如果查询出错，使用当前时间
        initial_check_time = datetime.utcnow() - timedelta(hours=1)
    
    last_check_time = initial_check_time
    logging.info(f"初始检查时间设置为: {last_check_time} (UTC时间)")
    
    while True:
        try:
            # 输出当前系统时间（东八区）和转换后的UTC时间，用于调试
            local_now = datetime.now()
            utc_now = datetime.utcnow()
            logging.debug(f"当前系统时间: {local_now} (东八区), 转换为UTC: {utc_now}")
            
            # 查询自上次检查以来的新消息
            logging.debug(f"查询 {last_check_time} 之后的消息")
            
            async with async_session() as session:
                # 先查询最新的消息时间，用于后续更新last_check_time
                latest_time_query = select(func.max(MessageRecord.time)).select_from(MessageRecord)
                latest_time_result = await session.execute(latest_time_query)
                db_latest_time = latest_time_result.scalar_one_or_none()
                
                if db_latest_time:
                    logging.debug(f"数据库最新消息时间: {db_latest_time} (UTC时间)")
                    
                    # 重要修改：如果数据库时间超前于当前时间，重置查询时间
                    if db_latest_time > utc_now:
                        logging.warning(f"数据库时间 {db_latest_time} 超出当前UTC时间 {utc_now}！")
                        # 如果超过48小时，可能是时区或系统时间问题
                        if (db_latest_time - utc_now) > timedelta(hours=48):
                            logging.warning("数据库时间异常！可能是时区设置或系统时间错误")
                            # 尝试降低查询时间阈值
                            last_check_time = utc_now - timedelta(minutes=5)
                            logging.warning(f"已将查询时间阈值降低为 {last_check_time}")
                
                # 查询新消息
                query = select(MessageRecord, SessionModel).join(
                    SessionModel, MessageRecord.session_persist_id == SessionModel.id
                ).where(
                    MessageRecord.time > last_check_time
                ).order_by(MessageRecord.time)
                
                result = await session.execute(query)
                new_messages = result.fetchall()
                
                if new_messages:
                    logging.debug(f"发现 {len(new_messages)} 条新消息")
                    
                    # 处理每条新消息
                    for i, (message, session) in enumerate(new_messages):
                        message_time_utc = message.time
                        message_time_local = message_time_utc + timedelta(hours=8)  # 转换为东八区时间
                        
                        logging.debug(f"消息{i+1}时间: {message_time_utc} (UTC) / {message_time_local} (本地), 内容: {message.plain_text[:20]}...")
                        
                        # 获取群ID并标准化为字符串
                        group_id = str(session.id2)

                        # 处理消息内容，移除前缀
                        content = message.plain_text
                        # 简单处理一下消息内容，去除可能的前缀
                        if isinstance(content, str):
                            if ": " in content and content.count(": ") == 1:
                                content = content.split(": ", 1)[1]
                            elif ":" in content and content.count(":") == 1:
                                # Handle cases like "User:Message" -> "Message"
                                parts = content.split(":", 1)
                                # Avoid splitting time format like "17:14:03" if it's the whole message
                                if not parts[0].isdigit() or (len(parts) > 1 and not parts[1].isdigit()):
                                    content = parts[1].lstrip() # Use lstrip to remove leading space if any

                        # 调试打印
                        logging.debug(f"处理新消息: 群={group_id}, 用户={session.id1}, 时间={message_time_utc} (UTC) / {message_time_local} (本地), 内容={str(content)[:20]}...")
                        
                        sent_count = 0
                        for connection, filter_settings in active_connections:
                            try:
                                logging.debug(f"连接过滤设置: enabled={filter_settings['enabled']}, allowed_groups={filter_settings['allowed_groups']}, 当前消息群ID={group_id}")
                                
                                # 修正的过滤逻辑：
                                should_send = False
                                if filter_settings['enabled']:
                                    if filter_settings['allowed_groups'] and group_id in filter_settings['allowed_groups']:
                                        should_send = True
                                    # If enabled but allowed_groups is empty, it means listen to nothing specific from this filter type.
                                    # However, our UI for "select groups" implies enabled + empty = listen to none of selected.
                                    # The case of enabled=true and allowed_groups=[] should ideally not happen if UI sends groups when enabling.
                                    # Let's assume if enabled=true, allowed_groups must be non-empty for a match.
                                else: # filter_settings['enabled'] is False
                                    # 修复：如果过滤器未启用，接收所有消息（原始行为）
                                    should_send = True
                                    logging.debug(f"过滤器未启用，将发送所有消息（群ID = {group_id}）")

                                if should_send:
                                    danmaku_message = {
                                        "type": "danmaku",
                                        "group_id": group_id,
                                        "user_id": session.id1,
                                        "time": message_time_utc.isoformat(),
                                        "content": content,
                                        "message_id": message.message_id
                                    }
                                    
                                    await connection.send_text(json.dumps(danmaku_message))
                                    sent_count += 1
                                    logging.debug(f"发送弹幕成功: 群={group_id}, 内容={str(content)[:20]}...")
                            except Exception as e:
                                logging.error(f"发送消息时出错: {str(e)}")
                        
                        logging.debug(f"消息已发送给 {sent_count}/{len(active_connections)} 个连接")
                    
                    # 更新最后检查时间（加上1毫秒避免重复获取同一条消息）
                    # 使用数据库中的最新消息时间，而非本地时间
                    if db_latest_time and db_latest_time <= utc_now:
                        last_check_time = db_latest_time + timedelta(milliseconds=1)
                    else:
                        # 如果数据库时间异常，使用消息列表中的最后一条消息时间
                        last_message_time = new_messages[-1][0].time
                        last_check_time = last_message_time + timedelta(milliseconds=1)
                    
                    logging.debug(f"更新最后检查时间为: {last_check_time} (UTC时间)")
                else:
                    # 重要修改：如果查询了一段时间都没有消息，间隔性重置查询时间，避免永远等待未来
                    now = datetime.utcnow()
                    # 如果上次检查时间超过当前时间5分钟，可能是检测时间阻塞在未来
                    if last_check_time > now + timedelta(minutes=5):
                        logging.warning(f"检查时间 {last_check_time} 异常超前，重置为当前时间前1小时")
                        last_check_time = now - timedelta(hours=1)
        except Exception as e:
            logging.error(f"检查新消息时出错: {str(e)}", exc_info=True)
        
        # 等待1秒再次检查
        await asyncio.sleep(1)

# 每30秒发送一次统计信息
async def send_stats_periodically():
    while True:
        try:
            await broadcast_stats()
        except Exception as e:
            print(f"发送统计信息时出错: {str(e)}")
        await asyncio.sleep(10) # Keep this at 10 seconds as per existing code

@app.on_event("startup")
async def startup_event():
    # 加载配置
    load_config()
    
    # 启动后台任务检查新消息
    asyncio.create_task(check_for_new_messages())
    # 启动后台任务发送统计信息
    asyncio.create_task(send_stats_periodically())
    # 打印应用信息和链接
    print("\n" + "="*50)
    print("群聊弹幕系统已启动")
    print("="*50)
    print("\n弹幕页面: http://localhost:8000")
    print("控制面板: http://localhost:8000/control")
    print("\n使用控制面板选择要监听的群聊")
    print("="*50)

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)