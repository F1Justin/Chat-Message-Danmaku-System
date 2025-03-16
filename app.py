from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
import asyncio
import json
import sqlalchemy
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy import Column, Integer, String, Text, DateTime, select, JSON, ForeignKey, join, func
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import sys
import logging
from typing import List, Dict, Tuple, Any, Optional

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

# 统计信息
stats = {
    "total_messages_processed": 0,
    "total_danmaku_sent": 0
}

@app.get("/", response_class=HTMLResponse)
async def get_danmaku_page(request: Request):
    # 打印访问日志，包括查询参数
    query_params = dict(request.query_params)
    if 'group' in query_params:
        print(f"访问弹幕页面，指定监听群聊ID: {query_params['group']}")
    else:
        print("访问弹幕页面，未指定监听群聊")
    
    return templates.TemplateResponse("danmaku.html", {"request": request})

@app.get("/control", response_class=HTMLResponse)
async def get_control_page(request: Request):
    print("访问控制面板页面")
    return templates.TemplateResponse("control.html", {"request": request})

@app.get("/api/groups", response_class=JSONResponse)
async def get_groups():
    try:
        async with async_session() as session:
            # 查询所有level=2的会话（群聊），但按群ID分组去重
            query = (
                select(SessionModel.id2)
                .where(SessionModel.level == 2)
                .group_by(SessionModel.id2)
                .order_by(SessionModel.id2)
            )
            result = await session.execute(query)
            group_ids = result.scalars().all()
            
            # 对每个群ID查询其session_id（取最新的一个）
            group_list = []
            for group_id in group_ids:
                # 查询该群ID对应的最新session
                session_query = (
                    select(SessionModel.id)
                    .where(
                        SessionModel.level == 2,
                        SessionModel.id2 == group_id
                    )
                    .order_by(SessionModel.id.desc())
                    .limit(1)
                )
                session_result = await session.execute(session_query)
                session_id = session_result.scalar_one_or_none()
                
                if session_id:
                    # 更新缓存
                    session_to_group_map[str(session_id)] = group_id
                    group_list.append({"id": str(session_id), "group_id": group_id})
            
            return {"status": "success", "groups": group_list}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/recent-messages/{session_id}", response_class=JSONResponse)
async def get_recent_messages(session_id: str):
    try:
        async with async_session() as session:
            # 首先获取这个session对应的群ID
            session_query = (
                select(SessionModel.id2)
                .where(SessionModel.id == int(session_id))
            )
            result = await session.execute(session_query)
            group_id = result.scalar_one_or_none()
            
            if not group_id:
                return {"status": "error", "message": "未找到该群聊"}
            
            # 查询该群的所有session_id
            all_sessions_query = (
                select(SessionModel.id)
                .where(
                    SessionModel.level == 2,
                    SessionModel.id2 == group_id
                )
            )
            all_sessions_result = await session.execute(all_sessions_query)
            all_session_ids = all_sessions_result.scalars().all()
            
            if not all_session_ids:
                return {"status": "error", "message": "未找到该群的会话"}
            
            # 查询指定群聊的最近10条消息（跨所有session）
            query = (
                select(MessageRecord, SessionModel)
                .join(SessionModel, MessageRecord.session_persist_id == SessionModel.id)
                .where(
                    MessageRecord.session_persist_id.in_(all_session_ids),
                    MessageRecord.type == "message",
                    MessageRecord.plain_text != ""  # 只获取有文本内容的消息
                )
                .order_by(MessageRecord.time.desc())
                .limit(20)  # 增加数量以提高获取到消息的可能性
            )
            
            result = await session.execute(query)
            messages = result.all()
            
            # 转换为前端需要的格式并按时间正序排列
            message_list = []
            for message_record, session_model in reversed(messages):
                message_list.append({
                    "type": "danmaku",
                    "content": message_record.plain_text,
                    "user_id": session_model.id1,
                    "group_id": session_model.id2,
                    "session_id": str(session_model.id),
                    "time": message_record.time.isoformat()
                })
            
            return {"status": "success", "messages": message_list}
    except Exception as e:
        print(f"获取最近消息出错: {e}")
        return {"status": "error", "message": str(e)}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logging.info("connection open")
    
    # 存储每个连接的过滤设置
    connection_filter = {
        "enabled": False,
        "allowed_groups": []
    }
    
    try:
        active_connections.append((websocket, connection_filter))
        
        # 发送初始连接成功消息
        await websocket.send_text(json.dumps({"type": "connection", "message": "连接成功"}))
        
        # 发送当前连接数量
        await broadcast_stats()
        
        while True:
            data = await websocket.receive_text()
            try:
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
                            if group_id:
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
                    elif message["action"] == "broadcast_settings":
                        # 处理广播设置命令
                        settings = message.get("settings", {})
                        broadcast_message = {
                            "type": "settings",
                            "settings": settings
                        }
                        # 广播设置到所有连接
                        await broadcast_to_all(json.dumps(broadcast_message))
                        # 发送确认消息
                        await websocket.send_text(json.dumps({
                            "type": "command_response",
                            "action": "broadcast_settings",
                            "status": "success",
                            "message": "设置已广播到所有客户端"
                        }))
            except json.JSONDecodeError:
                print("非JSON消息")
            except Exception as e:
                print(f"处理WebSocket消息时出错: {str(e)}")
                
    except WebSocketDisconnect:
        active_connections.remove((websocket, connection_filter))
        await broadcast_stats()
        logging.info("connection closed")
    except Exception as e:
        print(f"WebSocket连接错误: {str(e)}")
        if (websocket, connection_filter) in active_connections:
            active_connections.remove((websocket, connection_filter))
            await broadcast_stats()
        logging.info("connection closed")

@app.get("/api/stats", response_class=JSONResponse)
async def get_stats():
    return {
        "status": "success",
        "stats": {
            "active_connections": len(active_connections),
            "total_messages_processed": stats["total_messages_processed"],
            "total_danmaku_sent": stats["total_danmaku_sent"]
        }
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
                        if ": " in content and content.count(": ") == 1:
                            content = content.split(": ", 1)[1]
                        elif ":" in content and content.count(":") == 1:
                            content = content.split(":", 1)[1]
                        
                        # 调试打印
                        print(f"处理新消息: 群={group_id}, 用户={session.id1}, 时间={message_time_utc} (UTC) / {message_time_local} (本地), 内容={content[:20]}...")
                        
                        # 广播消息到所有活跃的WebSocket连接
                        sent_count = 0
                        for connection, filter_settings in active_connections:
                            try:
                                # 检查过滤设置
                                print(f"连接过滤设置: enabled={filter_settings['enabled']}, allowed_groups={filter_settings['allowed_groups']}, 当前消息群ID={group_id}")
                                
                                # 如果启用了过滤且当前群ID不在允许列表中，则跳过
                                if filter_settings['enabled'] and group_id not in filter_settings['allowed_groups']:
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
                                print(f"发送弹幕成功: 群={group_id}, 内容={content[:20]}...")
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

# 广播统计数据给所有连接
async def broadcast_stats():
    stats = {
        "type": "stats",
        "connections": len(active_connections),
    }
    await broadcast_to_all(json.dumps(stats))

# 广播消息给所有连接
async def broadcast_to_all(message: str):
    for connection, _ in active_connections:
        try:
            await connection.send_text(message)
        except Exception as e:
            print(f"广播消息失败: {e}")

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

@app.on_event("startup")
async def startup_event():
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
    print("\n使用控制面板选择要监听的群聊，或者直接使用以下格式访问特定群聊的弹幕：")
    print("http://localhost:8000/?group=群ID\n")

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True) 