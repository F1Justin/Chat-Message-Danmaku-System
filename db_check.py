import asyncio
import asyncpg
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 数据库配置
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

# 设置默认端口
DB_PORT = DB_PORT or "5432"

async def check_connection():
    try:
        # 尝试连接数据库
        conn_str = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
        print(f"尝试连接到: {conn_str}")
        conn = await asyncpg.connect(conn_str)
        print("数据库连接成功!")
        
        # 执行简单查询，检查最新消息时间
        query = "SELECT MAX(time) FROM nonebot_plugin_chatrecorder_messagerecord"
        latest_time = await conn.fetchval(query)
        print(f"数据库最新消息时间: {latest_time}")
        
        # 获取最近5条消息
        messages_query = """
        SELECT m.time, m.plain_text, s.id2 as group_id 
        FROM nonebot_plugin_chatrecorder_messagerecord m
        JOIN nonebot_plugin_session_orm_sessionmodel s ON m.session_persist_id = s.id
        WHERE m.type = 'message'
        ORDER BY m.time DESC LIMIT 5
        """
        messages = await conn.fetch(messages_query)
        print(f"\n最近 {len(messages)} 条消息:")
        for msg in messages:
            print(f"时间: {msg['time']}, 群ID: {msg['group_id']}, 内容: {msg['plain_text'][:30]}...")
        
        await conn.close()
        return True
    except Exception as e:
        print(f"数据库连接失败: {str(e)}")
        return False

asyncio.run(check_connection()) 