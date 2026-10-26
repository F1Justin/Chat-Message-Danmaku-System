import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Never use the developer's real .env database or credentials in tests.
os.environ.update(DB_USER="danmuku_test", DB_PASSWORD="test", DB_HOST="127.0.0.1",
                  DB_PORT="55439", DB_NAME="postgres", ALLOWED_HOSTS='["*"]')

import pytest
from fastapi.testclient import TestClient
import app as application
import rooms

TOKENS = {"a": {"admin": "a" * 32, "view": "b" * 32, "ingest": "c" * 32},
          "b": {"admin": "d" * 32, "view": "e" * 32, "ingest": "f" * 32}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    specs = []
    for room_id, tokens in TOKENS.items():
        specs.append(dict(id=room_id, admin_token=tokens["admin"], view_token=tokens["view"],
                          ingest_token=tokens["ingest"], allowed_groups=["101" if room_id == "a" else "202"]))
    path = tmp_path / "rooms.json"
    path.write_text(json.dumps({"rooms": specs}))
    monkeypatch.setattr(application.settings, "rooms_file", str(path))
    monkeypatch.setattr(application.settings, "room_state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(rooms, "_rooms", None)
    # Most tests exercise actual ASGI routing/WS with deterministic database fixtures.
    monkeypatch.setattr(application, "message_listener", application.MessageListener())
    with TestClient(application.app) as c:
        yield c


def auth(room="a", role="admin"):
    return {"Authorization": "Bearer " + TOKENS[room][role]}


def ws_url(room="a", role="view"):
    return f"/ws?room={room}&token={TOKENS[room][role]}"


def initial(ws):
    while ws.receive_json()["type"] != "screen_clear_update":
        pass


def receive_type(ws, kind):
    for _ in range(30):
        data = ws.receive_json()
        if data["type"] == kind:
            return data
    raise AssertionError(f"Did not receive {kind}")
