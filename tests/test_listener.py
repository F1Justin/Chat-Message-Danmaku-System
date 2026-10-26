import asyncio
import asyncpg
import app
from conftest import initial, receive_type, ws_url


async def wait_connected():
    async def wait():
        while not app.message_listener.connected:
            await asyncio.sleep(0.02)
    await asyncio.wait_for(wait(), 8)


async def insert(content, session=1, kind="message"):
    c = await asyncpg.connect(app.db_settings.dsn)
    try:
        return await c.fetchval("""
            INSERT INTO nonebot_plugin_chatrecorder_messagerecord_v2
            (session_persist_id,time,type,message_id,message,plain_text)
            VALUES ($1,now(),$2,$3,'{}',$4) RETURNING id
        """, session, kind, content, content)
    finally:
        await c.close()


def test_real_insert_notify_and_reconnect(client):
    client.portal.call(wait_connected)
    with client.websocket_connect(ws_url()) as ws:
        initial(ws)
        client.portal.call(insert, "live-first")
        assert receive_type(ws, "danmaku")["content"] == "live-first"
        # Terminate only this test listener's connection, then commit during outage.
        async def outage():
            listener = app.message_listener
            c = await asyncpg.connect(app.db_settings.dsn)
            try:
                await c.execute("SELECT pg_terminate_backend($1)", listener._connection.get_server_pid())
                await insert("after-disconnect")
            finally:
                await c.close()
        client.portal.call(outage)
        assert receive_type(ws, "danmaku")["content"] == "after-disconnect"
        # Replayed overlap must not re-emit either previous record.
        client.portal.call(insert, "after-recovery")
        assert receive_type(ws, "danmaku")["content"] == "after-recovery"


def test_only_authorized_group_messages_reach_screen(client):
    client.portal.call(wait_connected)
    with client.websocket_connect(ws_url()) as ws:
        initial(ws)
        client.portal.call(insert, "other-group", 2)
        client.portal.call(insert, "non-message-event", 1, "notice")
        client.portal.call(insert, "allowed-message")
        assert receive_type(ws, "danmaku")["content"] == "allowed-message"


def test_drain_retries_failure_without_advancing_cursor():
    async def run():
        listener = app.MessageListener()
        listener._baseline = 0
        listener._cursor = 0
        class FakeDB:
            async def fetch(self, *args): return [{"id": 1}, {"id": 2}]
        listener._connection = FakeDB()
        calls = []
        fail = True
        async def broadcast(message_id):
            nonlocal fail
            if message_id == 2 and fail:
                fail = False
                raise RuntimeError("temporary read failure")
            calls.append(message_id)
        listener._fetch_and_broadcast_message = broadcast
        try:
            await listener._drain()
        except RuntimeError:
            pass
        assert listener._cursor == 1
        await listener._drain()
        assert calls == [1, 2] and listener._cursor == 2
    asyncio.run(run())
