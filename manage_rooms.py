"""Create a room locally on the server; credentials never leave via a public API."""
import argparse
import json
import os
from pathlib import Path
import secrets
import tempfile
from urllib.parse import urlencode

from rooms import RoomSpec


def main():
    parser = argparse.ArgumentParser(description="创建独立弹幕房间并生成控制／观看链接")
    parser.add_argument("id")
    parser.add_argument("--group", action="append", required=True, help="真实群 ID，可重复指定")
    parser.add_argument("--name", default="")
    parser.add_argument("--source", default="nonebot")
    parser.add_argument("--platform", default="QQClient", help="NoneBot scope 或外部平台命名")
    parser.add_argument("--base-url", default="https://danmuku.f1justin.com")
    parser.add_argument("--file", default="rooms.json")
    args = parser.parse_args()
    path = Path(args.file)
    data = json.loads(path.read_text()) if path.exists() else {"rooms": []}
    existing = [RoomSpec.model_validate(x) for x in data["rooms"]]
    if any(r.id == args.id for r in existing):
        parser.error("房间已存在；请显式编辑配置以轮换令牌")
    spec = RoomSpec(id=args.id, name=args.name, source=args.source, platform=args.platform,
                    allowed_groups=args.group, admin_token=secrets.token_urlsafe(32),
                    view_token=secrets.token_urlsafe(32), ingest_token=secrets.token_urlsafe(32))
    data["rooms"].append(spec.model_dump())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".rooms-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    base = args.base_url.rstrip("/")
    print("控制：", base + "/control?" + urlencode({"room": spec.id, "token": spec.admin_token}))
    print("观看／OBS：", base + "/?" + urlencode({"room": spec.id, "token": spec.view_token}))
    print("配置已写入", path, "（权限 0600）；重启服务后生效。注入令牌只保存在配置中。")


if __name__ == "__main__":
    main()
