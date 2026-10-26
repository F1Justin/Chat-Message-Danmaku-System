"""Seed only the dedicated temporary PostgreSQL test database."""
import asyncio
from conftest import application
from sqlalchemy import text

async def main():
    async with application.engine.begin() as c:
        await c.run_sync(application.Base.metadata.create_all)
        await c.execute(text("""
          INSERT INTO nonebot_plugin_uninfo_botmodel VALUES (1,'bot','OneBot V11','QQClient') ON CONFLICT DO NOTHING;
        """))
        await c.execute(text("UPDATE nonebot_plugin_uninfo_botmodel SET scope='QQClient' WHERE id=1"))
        for i, group in ((1, '101'), (2, '202')):
            await c.execute(text("INSERT INTO nonebot_plugin_uninfo_scenemodel VALUES (:i,1,NULL,:g,1,'{}') ON CONFLICT DO NOTHING"), {"i": i, "g": group})
            await c.execute(text("INSERT INTO nonebot_plugin_uninfo_usermodel VALUES (:i,1,:u,'{}') ON CONFLICT DO NOTHING"), {"i": i,"u": f"u{i}"})
            await c.execute(text("INSERT INTO nonebot_plugin_uninfo_sessionmodel VALUES (:i,1,:i,:i,'{}') ON CONFLICT DO NOTHING"), {"i": i})
    await application.engine.dispose()
    import asyncpg
    from pathlib import Path
    connection = await asyncpg.connect(application.db_settings.dsn)
    try:
        await connection.execute(Path("migrations/001_create_notify_trigger.sql").read_text())
    finally:
        await connection.close()

asyncio.run(main())
