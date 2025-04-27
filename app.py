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

# 存储活跃的WebSocket连接，及其关联的群聊过滤
active_connections: List[Tuple[WebSocket, Dict[str, Any]]] = []

# 存储活跃连接的群聊过滤设置
connection_filters = {}

# 存储session_id到group_id的映射缓存
session_to_group_map: Dict[str, str] = {}

# 全局活跃群组ID
active_group_id: Optional[str] = None

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
                active_group_id = config.get('active_group_id')
                danmaku_speed = config.get('danmaku_speed', 10)
                print(f"已加载配置: {len(group_aliases)}个群别名, {len(favorite_groups)}个常用群组, 弹幕速度={danmaku_speed}s")
        else:
            print("配置文件不存在，使用默认设置")
    except Exception as e:
        print(f"加载配置出错: {e}")

# 保存配置
def save_config():
    global danmaku_speed
    try:
        config = {
            'group_aliases': group_aliases,
            'favorite_groups': favorite_groups,
            'active_group_id': active_group_id,
            'danmaku_speed': danmaku_speed
        }
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print("配置已保存")
    except Exception as e:
        print(f"保存配置出错: {e}")

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
    print(f"已广播设置更新: {setting_key}={setting_value}")

# 广播消息给所有连接
async def broadcast_to_all(message: str):
    for connection, _ in active_connections:
        try:
            await connection.send_text(message)
        except Exception as e:
            print(f"广播消息失败: {e}")

# 广播活跃群组变更消息
async def broadcast_group_change(group_id: str):
    """广播群组变更消息到所有连接"""
    message = {
        "type": "active_group",
        "group_id": group_id
    }
    await broadcast_to_all(json.dumps(message))
    print(f"已广播群组变更消息: {group_id}")

# 获取群聊ID
async def get_group_id_from_session_id(session_id: str) -> Optional[str]:
    # 尝试从缓存获取
    if session_id in session_to_group_map:
        group_id = session_to_group_map[session_id]
        print(f"从缓存获取映射: session_id={session_id} -> group_id={group_id}")
        return group_id
    
    # 如果缓存中没有，从数据库获取
    try:
        async with async_session() as session:
            query = select(SessionModel.id2).where(SessionModel.id == int(session_id))
            result = await session.execute(query)
            group_id = result.scalar_one_or_none()
            
            if group_id:
                session_to_group_map[session_id] = group_id
                print(f"从数据库获取映射: session_id={session_id} -> group_id={group_id}")
                return group_id
            else:
                print(f"未找到映射: session_id={session_id}")
                return None
    except Exception as e:
        print(f"获取群聊ID出错: {e}")
        return None

# 处理WebSocket消息
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global active_group_id, danmaku_speed
    await websocket.accept()
    logging.info("connection open")
    
    # 存储每个连接的过滤设置
    connection_filter = {
        "enabled": False,
        "allowed_groups": []
    }
    
    # 如果有全局活跃群组，则自动设置过滤
    if active_group_id:
        connection_filter["enabled"] = False  # 默认不启用过滤
        connection_filter["allowed_groups"] = [active_group_id]
        print(f"新连接自动设置过滤: 启用=False, 群ID={active_group_id}")
    
    try:
        active_connections.append((websocket, connection_filter))
        
        # 发送初始连接成功消息，包含当前设置
        await websocket.send_text(json.dumps({
            "type": "connection",
            "message": "连接成功",
            "settings": {
                "danmaku_speed": danmaku_speed
            }
        }))
        
        # 如果有全局活跃群组，发送群组信息
        if active_group_id:
            await websocket.send_text(json.dumps({
                "type": "active_group",
                "group_id": active_group_id
            }))
        
        # 发送当前连接数量
        await broadcast_stats()
        
        while True:
            try:
                data = await websocket.receive_text()
                message = json.loads(data)
                
                if message["type"] == "command":
                    if message["action"] == "set_groups":
                        # 处理过滤设置命令
                        filter_enabled = message.get("filter_enabled", False)
                        session_ids = message.get("groups", [])
                        
                        print(f"收到过滤设置: 启用={filter_enabled}, 群列表={session_ids}")
                        
                        # 将session_id映射到group_id
                        group_ids = []
                        for session_id in session_ids:
                            group_id = await get_group_id_from_session_id(session_id)
                            if group_id and group_id not in group_ids:  # 确保不重复添加相同的群ID
                                group_ids.append(group_id)
                        
                        connection_filter["enabled"] = filter_enabled
                        connection_filter["allowed_groups"] = group_ids
                        
                        print(f"设置过滤: enabled={connection_filter['enabled']}, session_ids={session_ids}, group_ids={group_ids}")
                        
                        # 发送确认消息
                        await websocket.send_text(json.dumps({
                            "type": "command_response",
                            "action": "set_groups",
                            "status": "success",
                            "message": f"群聊监听已{'启用' if filter_enabled else '禁用'}, 已选择 {len(session_ids)} 个群",
                            "debug_info": {
                                "session_ids": session_ids,
                                "group_ids": group_ids
                            }
                        }))
                    elif message["action"] == "set_active_group":
                        # 设置全局活跃群组
                        session_id = message.get("group_id")
                        
                        if session_id:
                            group_id = await get_group_id_from_session_id(session_id)
                            if group_id:
                                active_group_id = group_id
                                print(f"设置全局活跃群组: {active_group_id}")
                                save_config()
                                
                                # 设置当前连接的过滤
                                connection_filter["enabled"] = False  # 默认不启用过滤
                                connection_filter["allowed_groups"] = [active_group_id]
                                
                                # 广播群组变更消息
                                await broadcast_group_change(active_group_id)
                                
                                await websocket.send_text(json.dumps({
                                    "type": "command_response",
                                    "action": "set_active_group",
                                    "status": "success",
                                    "message": f"已设置活跃群组: {active_group_id}"
                                }))
                            else:
                                await websocket.send_text(json.dumps({
                                    "type": "command_response",
                                    "action": "set_active_group",
                                    "status": "error",
                                    "message": "无法获取群组ID"
                                }))
                        else:
                            active_group_id = None
                            save_config()
                            await websocket.send_text(json.dumps({
                                "type": "command_response",
                                "action": "set_active_group",
                                "status": "success",
                                "message": "已清除活跃群组"
                            }))
                    elif message["action"] == "get_active_group":
                        # 获取当前活跃群组
                        await websocket.send_text(json.dumps({
                            "type": "active_group",
                            "group_id": active_group_id
                        }))
                    elif message["action"] == "set_danmaku_speed":
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
                logging.debug(f"WebSocket loop processed message: {data[:50]}...") # 添加循环日志
            except json.JSONDecodeError:
                logging.warning("Received invalid JSON message") # 用 warning
            except Exception as e:
                logging.error(f"Error processing WebSocket message: {str(e)}", exc_info=True) # 记录完整错误
                
    except WebSocketDisconnect as e:
        # 尝试记录关闭代码和原因
        logging.info(f"WebSocket connection closed. Code: {e.code}, Reason: {e.reason}")
        active_connections.remove((websocket, connection_filter))
        await broadcast_stats()
        logging.info("connection closed")
    except Exception as e:
        # 记录更详细的 WebSocket 错误
        logging.error(f"Unhandled WebSocket error: {str(e)}", exc_info=True)
        if (websocket, connection_filter) in active_connections:
            active_connections.remove((websocket, connection_filter))
            await broadcast_stats()
        logging.info("connection closed")
    finally:
         # 确保连接总是被移除
        if (websocket, connection_filter) in active_connections:
            active_connections.remove((websocket, connection_filter))
            logging.info(f"Connection removed in finally block. Current connections: {len(active_connections)}")
            await broadcast_stats() # 确保广播更新
        logging.info("Exiting websocket_endpoint handler.") # 确认函数结束

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
                "groups": group_list,
                "active_group_id": active_group_id
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
                print("数据库中没有消息，使用当前时间")
                # 如果数据库为空，使用当前时间
                initial_check_time = datetime.utcnow() - timedelta(hours=1)
            else:
                print(f"数据库最新消息时间: {latest_time} (UTC时间)")
                # 使用数据库中最新消息的时间作为起始检查时间
                initial_check_time = latest_time
    except Exception as e:
        print(f"获取最新消息时间出错: {e}")
        # 如果查询出错，使用当前时间
        initial_check_time = datetime.utcnow() - timedelta(hours=1)
    
    last_check_time = initial_check_time
    print(f"初始检查时间设置为: {last_check_time} (UTC时间)")
    
    while True:
        try:
            # 输出当前系统时间（东八区）和转换后的UTC时间，用于调试
            local_now = datetime.now()
            utc_now = datetime.utcnow()
            print(f"当前系统时间: {local_now} (东八区), 转换为UTC: {utc_now}")
            
            # 查询自上次检查以来的新消息
            print(f"查询 {last_check_time} 之后的消息")
            
            async with async_session() as session:
                # 先查询最新的消息时间，用于后续更新last_check_time
                latest_time_query = select(func.max(MessageRecord.time)).select_from(MessageRecord)
                latest_time_result = await session.execute(latest_time_query)
                db_latest_time = latest_time_result.scalar_one_or_none()
                
                if db_latest_time:
                    print(f"数据库最新消息时间: {db_latest_time} (UTC时间)")
                
                # 查询新消息
                query = select(MessageRecord, SessionModel).join(
                    SessionModel, MessageRecord.session_persist_id == SessionModel.id
                ).where(
                    MessageRecord.time > last_check_time
                ).order_by(MessageRecord.time)
                
                result = await session.execute(query)
                new_messages = result.fetchall()
                
                if new_messages:
                    print(f"发现 {len(new_messages)} 条新消息")
                    
                    # 处理每条新消息
                    for i, (message, session) in enumerate(new_messages):
                        message_time_utc = message.time
                        message_time_local = message_time_utc + timedelta(hours=8)  # 转换为东八区时间
                        
                        print(f"消息{i+1}时间: {message_time_utc} (UTC) / {message_time_local} (本地), 内容: {message.plain_text[:20]}...")
                        
                        # 获取群ID
                        group_id = session.id2

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
                        print(f"处理新消息: 群={group_id}, 用户={session.id1}, 时间={message_time_utc} (UTC) / {message_time_local} (本地), 内容={str(content)[:20]}...")
                        
                        # 广播消息到所有活跃的WebSocket连接
                        sent_count = 0
                        for connection, filter_settings in active_connections:
                            try:
                                # 检查过滤设置
                                print(f"连接过滤设置: enabled={filter_settings['enabled']}, allowed_groups={filter_settings['allowed_groups']}, 当前消息群ID={group_id}")
                                
                                # 如果启用了过滤且当前群ID不在允许列表中，则跳过
                                if filter_settings['enabled'] and filter_settings['allowed_groups'] and group_id not in filter_settings['allowed_groups']:
                                    print(f"消息被过滤: 群ID {group_id} 不在允许列表中 {filter_settings['allowed_groups']}")
                                    continue
                                
                                # 发送消息
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
                                print(f"发送弹幕成功: 群={group_id}, 内容={str(content)[:20]}...")
                            except Exception as e:
                                print(f"发送消息时出错: {str(e)}")
                        
                        print(f"消息已发送给 {sent_count}/{len(active_connections)} 个连接")
                    
                    # 更新最后检查时间（加上1毫秒避免重复获取同一条消息）
                    # 使用数据库中的最新消息时间，而非本地时间
                    if db_latest_time:
                        last_check_time = db_latest_time + timedelta(milliseconds=1)
                        print(f"更新最后检查时间为: {last_check_time} (UTC时间)")
        except Exception as e:
            print(f"检查新消息时出错: {str(e)}")
            import traceback
            traceback.print_exc()
        
        # 等待1秒再次检查
        await asyncio.sleep(1)

# 每30秒发送一次统计信息
async def send_stats_periodically():
    while True:
        try:
            await broadcast_stats()
        except Exception as e:
            print(f"发送统计信息时出错: {str(e)}")
        await asyncio.sleep(10)

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
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True) 