import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

import app
import rooms
from conftest import TOKENS, auth, initial, receive_type, ws_url


def test_auth_roles_and_cross_room(client):
    for path in ("/", "/control", "/api/groups", "/api/status"):
        assert client.get(path + "?room=a").status_code == 401
        assert client.get(path + "?room=b", headers=auth("a")).status_code == 401
    assert client.get("/control?room=a", headers=auth(role="view")).status_code == 401
    assert client.get("/?room=a", headers=auth(role="ingest")).status_code == 401
    assert client.get("/api/groups?room=a", headers=auth(role="ingest")).status_code == 401
    assert client.get("/?room=missing", headers=auth()).status_code == 404
    assert client.get("/?room=a", headers=auth(role="view")).status_code == 200


def test_cookie_preview_does_not_downgrade_admin(client):
    r = client.get(f"/control?room=a&token={TOKENS['a']['admin']}", follow_redirects=False)
    assert r.status_code == 303 and "token=" not in r.headers["location"]
    assert r.headers["cache-control"] == "no-store"
    assert "HttpOnly" in r.headers["set-cookie"]
    client.get(f"/?room=a&token={TOKENS['a']['view']}")
    assert client.get("/control?room=a").status_code == 200
    assert client.get("/control?room=b").status_code == 401
    html = client.get("/control?room=a").text
    assert 'const roomId = "a"' in html
    assert TOKENS["b"]["view"] not in html


def test_group_and_history_grants(client):
    data = client.get("/api/groups?room=a", headers=auth()).json()
    assert [g["group_id"] for g in data["groups"]] == ["101"]
    assert client.get("/api/recent-messages/2?room=a", headers=auth()).json()["status"] == "error"
    assert client.post("/api/group-alias?room=a", headers=auth(),
                       json={"group_id": "202", "alias": "private"}).status_code == 403
    assert client.post("/api/favorite-group?room=a", headers=auth(),
                       json={"group_id": "2", "is_favorite": True}).status_code == 403


def test_websocket_auth_and_origin(client):
    for url in ("/ws?room=a", f"/ws?room=b&token={TOKENS['a']['admin']}", ws_url(role="ingest")):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url):
                pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(ws_url(), headers={"origin": "https://evil.example"}):
            pass
    assert client.post("/api/group-alias?room=a", headers={**auth(), "origin": "https://evil.example"},
                       json={"group_id": "101", "alias": "x"}).status_code == 403


def test_rooms_control_and_injection_isolated(client):
    with client.websocket_connect(ws_url("a", "admin")) as a, client.websocket_connect(ws_url("b")) as b:
        initial(a); initial(b)
        a.send_json({"type": "command", "action": "set_screen_clear", "enabled": True})
        assert receive_type(a, "screen_clear_update")["enabled"] is True
        assert rooms.rooms()["b"].manager.screen_cleared is False
        b.send_json({"type": "command", "action": "set_screen_clear", "enabled": True})
        assert receive_type(b, "command_response")["status"] == "error"
        assert client.post("/api/inject-danmaku?room=a", headers=auth(role="ingest"),
                           json={"group_id": "202", "content": "bad", "message_id": "bad"}).status_code == 403
        data = {"group_id": "202", "content": "room b", "message_id": "b-1"}
        r = client.post("/api/inject-danmaku?room=b", headers=auth("b", "ingest"), json=data)
        assert r.json()["sent"] == 1
        assert receive_type(b, "danmaku")["content"] == "room b"
        assert client.post("/api/inject-danmaku?room=b", headers=auth("b", "ingest"), json=data).json()["duplicate"]
        assert client.post("/api/inject-danmaku?room=b", headers=auth("b", "view"), json=data).status_code == 401


def test_selection_cannot_expand_grants_and_survives_reload(client):
    with client.websocket_connect(ws_url("a", "admin")) as ws:
        initial(ws)
        ws.send_json({"type": "command", "action": "set_groups", "filter_enabled": True, "groups": ["2"]})
        assert receive_type(ws, "command_response")["status"] == "error"
        ws.send_json({"type": "command", "action": "set_groups", "filter_enabled": True, "groups": []})
        assert receive_type(ws, "command_response")["status"] == "success"
        assert rooms.rooms()["a"].manager.global_allowed_groups == []
        ws.send_json({"type": "command", "action": "set_danmaku_speed", "speed": 15})
        assert receive_type(ws, "command_response")["status"] == "success"
    room = rooms.rooms()["a"]
    restored = rooms.Room(room.spec, Path(app.settings.room_state_dir))
    assert restored.manager.global_allowed_groups == []
    assert restored.runtime.danmaku_speed == 15
    assert rooms.rooms()["b"].runtime.danmaku_speed == 10


@pytest.mark.asyncio
async def test_source_boundary_and_empty_grants(client):
    room = rooms.rooms()["a"]
    class Fake:
        def __init__(self): self.data = []
        async def send_text(self, text): self.data.append(json.loads(text))
    from connection_manager import ManagedConnection
    fake = Fake()
    room.manager._connections.append(ManagedConnection(fake))
    message = dict(group_id="101", user_id="u", content="test", message_id="m", timestamp=datetime.now(timezone.utc))
    assert await rooms.broadcast_source("superlily-qq", **message) == 0
    assert await rooms.broadcast_source("nonebot", platform="Telegram", **message) == 0
    assert await rooms.broadcast_source("nonebot", **message) == 1
    room.spec.allowed_groups = []
    assert await rooms.broadcast_source("nonebot", **message) == 0


def test_room_cli_private_file(tmp_path):
    path = tmp_path / "rooms.json"
    r = subprocess.run([sys.executable, "manage_rooms.py", "event", "--group", "101", "--file", str(path)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert path.stat().st_mode & 0o777 == 0o600
    spec = rooms.RoomSpec.model_validate(json.loads(path.read_text())["rooms"][0])
    assert spec.id == "event" and "room=event" in r.stdout
    r = subprocess.run([sys.executable, "manage_rooms.py", "event", "--group", "202", "--file", str(path)], capture_output=True)
    assert r.returncode != 0


def test_invalid_room_specs():
    for overrides in ({"allowed_groups": ["*"]}, {"view_token": "a" * 32}, {"id": "../oops"}):
        data = dict(id="a", admin_token="a" * 32, view_token="b" * 32)
        data.update(overrides)
        with pytest.raises(ValidationError):
            rooms.RoomSpec(**data)


def test_subpath_auth_and_links(client):
    # Keep the same ASGI lifespan/event loop; emulate a proxy-mounted root path.
    client._transport.root_path = "/overlay"
    assert client.get("/overlay/control?room=a").status_code == 401
    assert client.get("/overlay/api/groups?room=a", headers=auth(role="view")).status_code == 401
    r = client.get(f"/overlay/control?room=a&token={TOKENS['a']['admin']}", follow_redirects=False)
    assert r.status_code == 303
    assert "Path=/overlay" in r.headers["set-cookie"]
    r = client.get("/overlay/control?room=a")
    assert r.status_code == 200
    assert 'const appBasePath = "/overlay"' in r.text
    assert client.get("/overlay/api/groups?room=a").json()["groups"][0]["group_id"] == "101"


def test_empty_selection_stays_silent_on_injection(client):
    with client.websocket_connect(ws_url("a", "admin")) as ws:
        initial(ws)
        ws.send_json({"type": "command", "action": "set_groups", "filter_enabled": False, "groups": []})
        assert receive_type(ws, "command_response")["status"] == "success"
        r = client.post("/api/inject-danmaku?room=a", headers=auth(role="ingest"),
                        json={"group_id": "101", "message_id": "quiet", "content": "quiet"})
        assert r.json()["sent"] == 0


def test_request_logs_redact_encoded_credentials():
    import logging
    from logging_utils import RedactTokens
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s %s %s %s %s',
                               ("client", "GET", "/control?room=a&%74oken=secret", "1.1", 303), None)
    RedactTokens().filter(record)
    assert "secret" not in record.getMessage() and "room=a" in record.getMessage()


def test_no_credentials_fails_closed(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app.settings, "rooms_file", str(tmp_path / "absent.json"))
    monkeypatch.setattr(app.settings, "admin_token", None)
    monkeypatch.setattr(app.settings, "view_token", None)
    monkeypatch.setattr(rooms, "_rooms", None)
    assert client.get("/").status_code == 404
    assert client.get("/control").status_code == 404


def test_reused_tokens_across_rooms_rejected(client, tmp_path, monkeypatch):
    path = tmp_path / "collision.json"
    specs = [room.spec.model_dump() for room in rooms.rooms().values()]
    specs[1]["view_token"] = specs[0]["admin_token"]
    path.write_text(json.dumps({"rooms": specs}))
    monkeypatch.setattr(app.settings, "rooms_file", str(path))
    monkeypatch.setattr(rooms, "_rooms", None)
    with pytest.raises(ValueError, match="unique"):
        rooms.rooms()
    # Leave the active lifespan with its original valid room registry.
    monkeypatch.setattr(app.settings, "rooms_file", str(tmp_path / "absent.json"))
    monkeypatch.setattr(rooms, "_rooms", {})
