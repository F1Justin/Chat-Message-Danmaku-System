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
from sqlalchemy import Column, Integer, String, Text, DateTime, select, JSON, ForeignKey, join
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import sys

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
active_connections = []

# 存储活跃连接的群聊过滤设置
connection_filters = {}

# 存储session_id到group_id的映射缓存
session_to_group_map = {}

@app.get("/", response_class=HTMLResponse)
async def get_danmaku_page(request: Request):
    return templates.TemplateResponse("danmaku.html", {"request": request})

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
    active_connections.append(websocket)
    # 初始化不过滤任何群聊
    connection_filters[websocket] = {"groups": [], "filter_enabled": False, "group_ids": []}
    
    try:
        # 发送初始连接成功消息
        await websocket.send_json({"type": "connection", "message": "连接成功"})
        
        # 保持连接并接收消息
        while True:
            data = await websocket.receive_text()
            try:
                # 处理客户端发送的控制消息
                message_data = json.loads(data)
                if message_data.get("type") == "command":
                    if message_data.get("action") == "set_groups":
                        # 设置要过滤的群聊
                        groups = message_data.get("groups", [])
                        filter_enabled = message_data.get("filter_enabled", False)
                        
                        print(f"收到过滤设置: 启用={filter_enabled}, 群列表={groups}")
                        
                        # 获取对应的群ID列表
                        group_ids = []
                        for session_id in groups:
                            # 尝试从缓存获取
                            if session_id in session_to_group_map:
                                group_id = session_to_group_map[session_id]
                                group_ids.append(group_id)
                                print(f"从缓存获取映射: session_id={session_id} -> group_id={group_id}")
                            else:
                                # 如果缓存中没有，尝试从数据库获取
                                async with async_session() as session:
                                    query = select(SessionModel.id2).where(SessionModel.id == int(session_id))
                                    result = await session.execute(query)
                                    group_id = result.scalar_one_or_none()
                                    if group_id:
                                        session_to_group_map[session_id] = group_id
                                        group_ids.append(group_id)
                                        print(f"从数据库获取映射: session_id={session_id} -> group_id={group_id}")
                                    else:
                                        print(f"未找到映射: session_id={session_id}")
                        
                        connection_filters[websocket] = {
                            "groups": groups,
                            "filter_enabled": filter_enabled,
                            "group_ids": group_ids  # 存储关联的群ID列表
                        }
                        
                        print(f"设置过滤: enabled={filter_enabled}, session_ids={groups}, group_ids={group_ids}")
                        
                        await websocket.send_json({
                            "type": "command_response", 
                            "action": "set_groups",
                            "status": "success",
                            "message": f"群聊监听已{'启用' if filter_enabled else '禁用'}, 已选择 {len(groups)} 个群",
                            "debug_info": {
                                "group_ids": group_ids
                            }
                        })
            except json.JSONDecodeError:
                pass
            except Exception as e:
                print(f"处理WebSocket消息时出错: {e}")
            
    except WebSocketDisconnect:
        active_connections.remove(websocket)
        if websocket in connection_filters:
            del connection_filters[websocket]
    except Exception as e:
        print(f"WebSocket连接出错: {e}")
        if websocket in active_connections:
            active_connections.remove(websocket)
        if websocket in connection_filters:
            del connection_filters[websocket]

# 检查数据库中的新消息并发送到客户端
async def check_for_new_messages():
    # 获取数据库中最新消息的时间，避免依赖本地系统时间
    try:
        async with async_session() as session:
            # 查询最近的一条消息，获取其时间作为起始时间
            latest_msg_query = (
                select(MessageRecord.time)
                .order_by(MessageRecord.time.desc())
                .limit(1)
            )
            result = await session.execute(latest_msg_query)
            latest_time = result.scalar_one_or_none()
            
            if latest_time:
                # 使用最新消息时间减去10分钟作为起始时间（不需要转换时区，因为都是数据库时间）
                last_check_time = latest_time - timedelta(minutes=10)
                print(f"从数据库获取最新消息时间: {latest_time} (UTC时间)")
                print(f"设置初始查询时间为: {last_check_time} (UTC时间)")
            else:
                # 如果没有消息，使用一个较早的时间，需要转换为UTC时间
                now_utc = datetime.now() - timedelta(hours=8)  # 东八区转UTC
                last_check_time = now_utc - timedelta(days=1)  # 过去24小时
                print(f"数据库无消息，设置初始查询时间为: {last_check_time} (UTC时间)")
    except Exception as e:
        print(f"获取初始时间出错: {e}")
        # 默认使用一个较早的时间，转换为UTC
        now_utc = datetime.now() - timedelta(hours=8)  # 东八区转UTC
        last_check_time = now_utc - timedelta(hours=1)
        print(f"使用默认初始查询时间: {last_check_time} (UTC时间)")
    
    print(f"开始监听新消息，初始时间: {last_check_time} (UTC时间)")
    
    while True:
        try:
            # 获取当前UTC时间（从东八区转换）
            current_time_utc = datetime.now() - timedelta(hours=8)
            print(f"当前系统时间: {datetime.now()} (东八区), 转换为UTC: {current_time_utc}")
            print(f"查询 {last_check_time} 之后的消息")
            
            # 查询新消息
            async with async_session() as session:
                # 查询最近的一条消息，确认数据库中的最新时间
                latest_check = (
                    select(MessageRecord.time)
                    .order_by(MessageRecord.time.desc())
                    .limit(1)
                )
                latest_result = await session.execute(latest_check)
                db_latest_time = latest_result.scalar_one_or_none()
                
                if db_latest_time:
                    print(f"数据库最新消息时间: {db_latest_time} (UTC时间)")
            
                # 获取群聊消息 (level=2 代表群聊)
                query = (
                    select(MessageRecord, SessionModel)
                    .join(SessionModel, MessageRecord.session_persist_id == SessionModel.id)
                    .where(
                        MessageRecord.time > last_check_time,
                        MessageRecord.type == "message",
                        MessageRecord.plain_text != "",  # 只获取有文本内容的消息
                        SessionModel.level == 2  # 只获取群聊消息
                    )
                    .order_by(MessageRecord.time)
                )
                
                result = await session.execute(query)
                messages = result.all()
                
                if messages:
                    print(f"发现 {len(messages)} 条新消息")
                    for idx, (msg, _) in enumerate(messages):
                        local_time = msg.time + timedelta(hours=8)  # 转换为东八区显示
                        print(f"消息{idx+1}时间: {msg.time} (UTC) / {local_time} (本地), 内容: {msg.plain_text[:30]}...")
                
                # 处理并发送新消息
                for message_record, session_model in messages:
                    if active_connections:
                        # 解析消息内容，寻找文本内容
                        message_content = message_record.plain_text
                        
                        # 获取用户和群信息
                        user_id = session_model.id1
                        group_id = session_model.id2
                        session_id = str(session_model.id)
                        
                        # 转换为本地时间显示
                        local_time = message_record.time + timedelta(hours=8)
                        print(f"处理新消息: 群={group_id}, 用户={user_id}, 时间={message_record.time} (UTC) / {local_time} (本地), 内容={message_content[:20]}...")
                        
                        # 更新缓存
                        session_to_group_map[session_id] = group_id
                        
                        # 构建弹幕数据
                        danmaku_data = {
                            "type": "danmaku",
                            "content": message_content,
                            "user_id": user_id,
                            "group_id": group_id,
                            "session_id": session_id,
                            "color": "#ffffff",  # 默认白色，也可以基于用户ID设置颜色
                            "size": 32  # 默认中等大小
                        }
                        
                        # 向所有活跃连接发送弹幕（根据过滤设置）
                        sent_count = 0
                        for connection in active_connections:
                            try:
                                # 检查该连接是否启用了群聊过滤
                                filters = connection_filters.get(connection, {})
                                filter_enabled = filters.get("filter_enabled", False)
                                allowed_group_ids = filters.get("group_ids", [])
                                
                                # 打印调试信息
                                print(f"连接过滤设置: enabled={filter_enabled}, allowed_groups={allowed_group_ids}, 当前消息群ID={group_id}")
                                
                                # 如果过滤已启用，但此消息不在允许的群中，则跳过
                                if filter_enabled and allowed_group_ids and group_id not in allowed_group_ids:
                                    print(f"消息被过滤: 群ID {group_id} 不在允许列表中 {allowed_group_ids}")
                                    continue
                                
                                await connection.send_json(danmaku_data)
                                sent_count += 1
                                print(f"发送弹幕成功: 群={group_id}, 内容={message_content[:20]}...")
                            except Exception as e:
                                print(f"发送消息时出错: {e}")
                        
                        print(f"消息已发送给 {sent_count}/{len(active_connections)} 个连接")
                
                # 更新最后检查时间
                if messages:
                    # 获取最后一条消息的时间加上1毫秒，避免重复获取消息
                    last_message_time = max(record.time for record, _ in messages)
                    last_check_time = last_message_time + timedelta(milliseconds=1)
                    print(f"更新最后检查时间为: {last_check_time} (UTC时间)")
                else:
                    # 如果没有新消息，看看是否需要更新检查时间
                    # 如果当前检查时间太老，更新到更近的时间点
                    time_diff = current_time_utc - last_check_time
                    if time_diff > timedelta(minutes=30):  # 如果时间差超过30分钟
                        last_check_time = current_time_utc - timedelta(minutes=5)  # 更新到更近的时间点
                        print(f"检查时间过老，更新为: {last_check_time} (UTC时间)")
                
            # 等待一段时间后再次检查
            await asyncio.sleep(1)  # 每秒检查一次
            
        except Exception as e:
            print(f"检查新消息时出错: {e}")
            await asyncio.sleep(5)  # 出错后等待5秒再尝试

@app.on_event("startup")
async def startup_event():
    # 启动后台任务检查新消息
    asyncio.create_task(check_for_new_messages())

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True) 