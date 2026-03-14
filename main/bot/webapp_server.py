import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from aiohttp import web

from . import db
from .config import BOT_TOKEN

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"


def _validate_init_data(init_data: str, max_age_seconds: int = 24 * 3600) -> dict[str, Any]:
    if not init_data:
        raise PermissionError("missing init data")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise PermissionError("missing hash")

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs.keys()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise PermissionError("bad hash")

    auth_date_raw = pairs.get("auth_date")
    if auth_date_raw:
        try:
            auth_date = int(auth_date_raw)
            if abs(int(time.time()) - auth_date) > max_age_seconds:
                raise PermissionError("stale auth")
        except ValueError as exc:
            raise PermissionError("bad auth_date") from exc

    user_raw = pairs.get("user")
    if not user_raw:
        raise PermissionError("missing user")
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise PermissionError("bad user json") from exc
    if "id" not in user:
        raise PermissionError("missing user id")
    return user


def _extract_user_id(request: web.Request) -> int:
    init_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    user = _validate_init_data(init_data)
    return int(user["id"])


async def _index(request: web.Request) -> web.Response:
    return web.FileResponse(WEBAPP_DIR / "index.html")


async def _app_js(request: web.Request) -> web.Response:
    return web.FileResponse(WEBAPP_DIR / "app.js")


async def _app_css(request: web.Request) -> web.Response:
    return web.FileResponse(WEBAPP_DIR / "app.css")


async def _api_groups(request: web.Request) -> web.Response:
    user_id = _extract_user_id(request)
    rows = db.list_user_groups(user_id)
    return web.json_response(
        {
            "groups": [
                {
                    "group_id": int(row["group_id"]),
                    "title": str(row["title"]),
                }
                for row in rows
            ]
        }
    )


async def _api_users(request: web.Request) -> web.Response:
    user_id = _extract_user_id(request)

    group_id_raw = request.query.get("group_id", "").strip()
    if not group_id_raw:
        return web.json_response({"error": "group_id is required"}, status=400)
    try:
        group_id = int(group_id_raw)
    except ValueError:
        return web.json_response({"error": "bad group_id"}, status=400)

    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    query = request.query.get("q", "").strip()
    try:
        offset = max(0, int(request.query.get("offset", "0")))
    except ValueError:
        offset = 0
    try:
        limit = int(request.query.get("limit", "20"))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 50))

    rows, total = db.list_group_users_for_whitelist(group_id, query, limit=limit, offset=offset)
    whitelist_set = set(db.list_whitelist(group_id))

    users = []
    for row in rows:
        uid = int(row["user_id"])
        users.append(
            {
                "user_id": uid,
                "full_name": str(row["full_name"] or ""),
                "username": str(row["username"] or ""),
                "at_username": str(row["at_username"] or ""),
                "phone": str(row["phone"] or ""),
                "status": str(row["status"] or ""),
                "is_whitelisted": uid in whitelist_set,
            }
        )

    return web.json_response({"users": users, "total": total, "offset": offset, "limit": limit})


async def _api_grant(request: web.Request) -> web.Response:
    user_id = _extract_user_id(request)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    try:
        group_id = int(payload.get("group_id"))
        target_user_id = int(payload.get("user_id"))
    except Exception:
        return web.json_response({"error": "group_id and user_id are required"}, status=400)

    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    db.add_whitelist(group_id, target_user_id, user_id)
    return web.json_response({"ok": True})


async def _api_revoke(request: web.Request) -> web.Response:
    user_id = _extract_user_id(request)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    try:
        group_id = int(payload.get("group_id"))
        target_user_id = int(payload.get("user_id"))
    except Exception:
        return web.json_response({"error": "group_id and user_id are required"}, status=400)

    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    db.remove_whitelist(group_id, target_user_id)
    return web.json_response({"ok": True})


async def start_webapp_server(host: str, port: int) -> tuple[web.AppRunner, web.TCPSite]:
    app = web.Application()
    app.router.add_get("/webapp", _index)
    app.router.add_get("/webapp/app.js", _app_js)
    app.router.add_get("/webapp/app.css", _app_css)
    app.router.add_get("/webapp/api/groups", _api_groups)
    app.router.add_get("/webapp/api/users", _api_users)
    app.router.add_post("/webapp/api/whitelist/grant", _api_grant)
    app.router.add_post("/webapp/api/whitelist/revoke", _api_revoke)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    return runner, site
