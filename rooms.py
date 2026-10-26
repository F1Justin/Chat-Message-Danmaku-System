"""Server-owned room credentials, group grants and persistent display state."""
from collections import OrderedDict
from contextvars import ContextVar
import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from config import RuntimeConfig, get_app_settings


class RoomSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")
    name: str = ""
    admin_token: str = Field(min_length=32)
    view_token: str = Field(min_length=32)
    ingest_token: Optional[str] = Field(default=None, min_length=32)
    source: str = Field(default="nonebot", pattern=r"^[a-z0-9_-]+$")
    platform: str = Field(default="QQClient", min_length=1, max_length=64)
    allowed_groups: list[str] = Field(default_factory=list)

    @field_validator("allowed_groups")
    @classmethod
    def validate_groups(cls, groups):
        if any(not g or g == "*" for g in groups):
            raise ValueError("Use explicit group IDs; wildcard grants are forbidden")
        return list(dict.fromkeys(groups))

    @model_validator(mode="after")
    def distinct_tokens(self):
        tokens = [self.admin_token, self.view_token, self.ingest_token]
        tokens = [t for t in tokens if t]
        if len(tokens) != len(set(tokens)):
            raise ValueError("Room roles must use different tokens")
        return self


class Room:
    def __init__(self, spec, state_dir):
        from connection_manager import ConnectionManager
        self.spec = spec
        self.runtime = RuntimeConfig(str(state_dir / f"{spec.id}.json"))
        self.manager = ConnectionManager()
        selected = self.runtime.selected_groups
        self.manager.set_global_filter(True, spec.allowed_groups if selected is None else
                                       [g for g in selected if g in spec.allowed_groups])
        self.manager.set_screen_cleared(self.runtime.screen_cleared)
        self.ingested_ids = OrderedDict()
        if self.runtime.active_group_id not in spec.allowed_groups:
            self.runtime.active_group_id = None

    def permits(self, group_id):
        return str(group_id) in self.spec.allowed_groups


current_room: ContextVar[Room] = ContextVar("current_room")
_rooms: Optional[dict[str, Room]] = None


def rooms():
    global _rooms
    if _rooms is not None:
        return _rooms
    settings = get_app_settings()
    path = Path(settings.rooms_file)
    if path.exists():
        specs = [RoomSpec.model_validate(x) for x in json.loads(path.read_text())["rooms"]]
    else:
        # Existing URLs remain valid. Unconfigured credentials fail closed.
        if not settings.admin_token or not settings.view_token:
            specs = []
        else:
            specs = [RoomSpec(id="default", admin_token=settings.admin_token,
                              view_token=settings.view_token, allowed_groups=settings.allowed_groups)]
    ids, tokens = set(), set()
    for spec in specs:
        if spec.id in ids:
            raise ValueError("Duplicate room ID")
        ids.add(spec.id)
        for token in (spec.admin_token, spec.view_token, spec.ingest_token):
            if token and token in tokens:
                raise ValueError("Tokens must be unique across all rooms")
            if token:
                tokens.add(token)
    state_dir = Path(settings.room_state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _rooms = {spec.id: Room(spec, state_dir) for spec in specs}
    return _rooms


def get_room():
    return current_room.get()


class RuntimeProxy:
    def __getattr__(self, name):
        return getattr(get_room().runtime, name)

    def __setattr__(self, name, value):
        setattr(get_room().runtime, name, value)


async def broadcast_source(source, platform="QQClient", **message):
    sent = 0
    for room in rooms().values():
        if room.spec.source == source and room.spec.platform == platform and room.permits(message["group_id"]):
            sent += await room.manager.broadcast_danmaku(**message)
    return sent
