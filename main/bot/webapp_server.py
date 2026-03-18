"""Aiohttp backend for Telegram WebApp moderation flows."""

import hashlib
import hmac
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot

from . import config, db, state
from .actions import apply_permanent_mute, apply_unmute, delete_message_safe
from .config import BOT_TOKEN
from .handlers import CATEGORY_LABELS, _refresh_alerts_for_group
from .logging_utils import emit_structured_log

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
WORD_TRIGGER_RE = re.compile(r"^[A-Za-zА-Яа-яІіЇїЄєҐґЁё'’\s]+$")
PHRASE_TRIGGER_RE = re.compile(r"^[A-Za-zА-Яа-яІіЇїЄєҐґЁё'’\s]+$")


def _normalize_trigger_value(value: str) -> str:
    """Normalize trigger text to a canonical lowercase whitespace-collapsed form."""
    return " ".join(str(value or "").strip().lower().split())


def _validate_word_trigger_input(raw: str) -> list[str]:
    """Validate and split a word-trigger input field into individual normalized words."""
    normalized = _normalize_trigger_value(raw)
    if not normalized:
        raise ValueError("empty trigger")
    if not WORD_TRIGGER_RE.fullmatch(normalized):
        raise ValueError("bad trigger input")
    words = [word for word in normalized.split(" ") if word]
    if not words:
        raise ValueError("empty trigger")
    return words


def _validate_phrase_trigger_input(raw: str) -> str:
    """Validate and normalize a phrase trigger entered from the WebApp."""
    normalized = _normalize_trigger_value(raw)
    if not normalized:
        raise ValueError("empty trigger")
    if not PHRASE_TRIGGER_RE.fullmatch(normalized):
        raise ValueError("bad trigger input")
    return normalized


def _validate_init_data(init_data: str, max_age_seconds: int = 24 * 3600) -> dict[str, Any]:
    """Validate Telegram WebApp init data and return decoded user payload."""
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
    """Extract authenticated Telegram user id from WebApp request headers."""
    init_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    user = _validate_init_data(init_data)
    return int(user["id"])


def _extract_group_id(request: web.Request, key: str = "group_id") -> int:
    """Extract required integer group id from request query parameters."""
    raw = request.query.get(key, "").strip()
    if not raw:
        raise ValueError(f"{key} is required")
    return int(raw)


def _message_link(chat_id: int, message_id: int) -> str | None:
    """Build a `t.me/c/...` message link for supergroups when possible."""
    chat_str = str(chat_id)
    if chat_str.startswith("-100"):
        return f"https://t.me/c/{chat_str[4:]}/{message_id}"
    return None


def _profile_url(user_id: int) -> str:
    """Build a Telegram profile deep link for a user."""
    return f"tg://user?id={user_id}"


def _serialize_ad(row: Any) -> dict[str, Any]:
    """Convert an ad DB row into WebApp JSON payload."""
    return {
        "ad_id": int(row["ad_id"]),
        "group_id": int(row["group_id"]),
        "user_id": int(row["user_id"]),
        "text": str(row["text"] or ""),
        "has_media": bool(row["has_media"]),
        "category": str(row["category"]),
        "category_label": CATEGORY_LABELS.get(str(row["category"]), str(row["category"])),
        "decision": str(row["decision"] or ""),
        "requires_action": bool(row["requires_action"]),
        "created_at": int(row["created_at"] or 0),
        "resolved_at": int(row["resolved_at"] or 0) if row["resolved_at"] else None,
        "source_chat_id": int(row["source_chat_id"]),
        "source_message_id": int(row["source_message_id"]),
        "message_url": _message_link(int(row["source_chat_id"]), int(row["source_message_id"])),
        "profile_url": _profile_url(int(row["user_id"])),
        "full_name": str(row["full_name"] or ""),
        "username": str(row["username"] or ""),
        "at_username": str(row["at_username"] or ""),
        "phone": str(row["phone"] or ""),
        "user_status": str(row["user_status"] or ""),
    }


async def _refresh_group_alerts(bot: Bot, group_id: int) -> None:
    """Refresh Telegram alert messages for all moderation categories of a group."""
    for category in ("blocked", "suspect", "pending", "confirmed"):
        await _refresh_alerts_for_group(bot, group_id, category)


async def _index(request: web.Request) -> web.Response:
    """Serve the WebApp entry HTML without caching."""
    response = web.FileResponse(WEBAPP_DIR / "index.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


async def _root(request: web.Request) -> web.Response:
    """Redirect root path to the WebApp entrypoint."""
    raise web.HTTPFound("/webapp")


async def _favicon(request: web.Request) -> web.Response:
    """Return an empty favicon response to avoid noisy 404s."""
    return web.Response(status=204)


async def _health(request: web.Request) -> web.Response:
    """Health endpoint used by Docker healthchecks."""
    return web.json_response({"ok": True})


async def _app_js(request: web.Request) -> web.Response:
    """Serve WebApp JavaScript without caching."""
    response = web.FileResponse(WEBAPP_DIR / "app.js")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


async def _app_css(request: web.Request) -> web.Response:
    """Serve WebApp CSS without caching."""
    response = web.FileResponse(WEBAPP_DIR / "app.css")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


async def _api_groups(request: web.Request) -> web.Response:
    """Return groups available to the authenticated moderator."""
    user_id = _extract_user_id(request)
    rows = db.list_user_groups(user_id)
    return web.json_response(
        {
            "user_id": user_id,
            "selected_group_id": db.get_selected_group(user_id),
            "groups": [
                {
                    "group_id": int(row["group_id"]),
                    "title": str(row["title"]),
                    "is_paused": bool(row["is_paused"]),
                    "notify_pending": bool(row["notify_pending"]),
                    "blocked_alert_sound": bool(row["blocked_alert_sound"]),
                    "swipe_requires_confirm": bool(row["swipe_requires_confirm"]),
                    "hide_confirmed_blocked": bool(row["hide_confirmed_blocked"]),
                }
                for row in rows
            ]
        }
    )


async def _api_overview(request: web.Request) -> web.Response:
    """Return current group settings and unresolved moderation counters."""
    user_id = _extract_user_id(request)
    try:
        group_id = _extract_group_id(request)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    group = db.get_group(group_id)
    if group is None:
        return web.json_response({"error": "group not found"}, status=404)

    return web.json_response(
        {
            "group": {
                "group_id": group_id,
                "title": str(group["title"]),
                "created_by": int(group["created_by"]),
                "is_paused": bool(group["is_paused"]),
                "notify_pending": bool(group["notify_pending"]),
                "blocked_alert_sound": bool(group["blocked_alert_sound"]),
                "swipe_requires_confirm": bool(group["swipe_requires_confirm"]),
                "hide_confirmed_blocked": bool(group["hide_confirmed_blocked"]),
                "moderator_count": len(db.list_moderators(group_id)),
            },
            "counts": db.get_unresolved_counts(group_id),
        }
    )


async def _api_ads(request: web.Request) -> web.Response:
    """Return a paginated list of ads for the requested moderation category."""
    user_id = _extract_user_id(request)
    try:
        group_id = _extract_group_id(request)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    category = request.query.get("category", "suspect").strip().lower()
    if category not in {"blocked", "suspect", "pending", "confirmed"}:
        return web.json_response({"error": "bad category"}, status=400)

    query = request.query.get("q", "").strip()
    sort = request.query.get("sort", "newest").strip().lower()
    try:
        offset = max(0, int(request.query.get("offset", "0")))
    except ValueError:
        offset = 0
    try:
        limit = int(request.query.get("limit", "20"))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 50))

    rows, total = db.list_ads_for_web(
        group_id,
        category,
        search=query,
        sort=sort,
        limit=limit,
        offset=offset,
        hide_confirmed_blocked=db.get_hide_confirmed_blocked(group_id),
    )
    return web.json_response(
        {
            "category": category,
            "category_label": CATEGORY_LABELS.get(category, category),
            "total": total,
            "offset": offset,
            "limit": limit,
            "counts": db.get_unresolved_counts(group_id),
            "items": [_serialize_ad(row) for row in rows],
        }
    )


async def _api_ads_action(request: web.Request) -> web.Response:
    """Apply a single moderation action to a selected ad from the WebApp."""
    user_id = _extract_user_id(request)
    bot: Bot = request.app["bot"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    try:
        group_id = int(payload.get("group_id"))
        ad_id = int(payload.get("ad_id"))
    except Exception:
        return web.json_response({"error": "group_id and ad_id are required"}, status=400)

    action = str(payload.get("action") or "").strip().lower()
    if action not in {"approve", "ack", "block", "unmute"}:
        return web.json_response({"error": "bad action"}, status=400)

    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)
    if db.is_group_paused(group_id):
        return web.json_response({"error": "moderation paused"}, status=409)

    ad = db.get_ad(ad_id)
    if ad is None or int(ad["group_id"]) != group_id:
        return web.json_response({"error": "ad not found"}, status=404)

    if action == "approve":
        db.update_ad_decision(ad_id=ad_id, decision="approved", moderator_id=user_id, requires_action=False)
    elif action == "ack":
        db.update_ad_decision(ad_id=ad_id, decision="acknowledged", moderator_id=user_id, requires_action=False)
    elif action == "block":
        if not config.TEST_MODE:
            await delete_message_safe(bot, int(ad["source_chat_id"]), int(ad["source_message_id"]))
            await apply_permanent_mute(bot, int(ad["source_chat_id"]), int(ad["user_id"]))
        state.add_strike(group_id, int(ad["user_id"]))
        db.update_ad_decision(
            ad_id=ad_id,
            decision="muted_manual",
            moderator_id=user_id,
            requires_action=False,
            category="blocked",
        )
    elif action == "unmute":
        if not config.TEST_MODE:
            await apply_unmute(bot, int(ad["source_chat_id"]), int(ad["user_id"]))
        db.update_ad_decision(
            ad_id=ad_id,
            decision="unmuted",
            moderator_id=user_id,
            requires_action=False,
            category="blocked",
        )

    db.record_audit_event(
        event_type="webapp_ad_action",
        source="webapp",
        actor_user_id=user_id,
        target_user_id=int(ad["user_id"]),
        group_id=group_id,
        ad_id=ad_id,
        payload={
            "action": action,
            "previous_category": str(ad["category"]),
            "previous_decision": str(ad["decision"]),
        },
    )
    emit_structured_log(
        "webapp_ad_action",
        logger_name=__name__,
        actor_user_id=user_id,
        target_user_id=int(ad["user_id"]),
        group_id=group_id,
        ad_id=ad_id,
        action=action,
    )
    await _refresh_group_alerts(bot, group_id)
    updated = db.get_ad(ad_id)
    return web.json_response({"ok": True, "counts": db.get_unresolved_counts(group_id), "item": _serialize_ad(updated) if updated else None})


async def _api_ads_confirm_all(request: web.Request) -> web.Response:
    """Bulk-confirm ads in a category that still require moderator action."""
    user_id = _extract_user_id(request)
    bot: Bot = request.app["bot"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    try:
        group_id = int(payload.get("group_id"))
    except Exception:
        return web.json_response({"error": "group_id is required"}, status=400)

    category = str(payload.get("category") or "").strip().lower()
    if category not in {"blocked", "pending", "confirmed"}:
        return web.json_response({"error": "bad category"}, status=400)

    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)
    if db.is_group_paused(group_id):
        return web.json_response({"error": "moderation paused"}, status=409)

    if category == "blocked":
        updated = db.confirm_all(group_id, category, user_id, decision="acknowledged", action="acknowledged_all")
    else:
        updated = db.confirm_all(group_id, category, user_id)

    db.record_audit_event(
        event_type="webapp_confirm_all",
        source="webapp",
        actor_user_id=user_id,
        group_id=group_id,
        payload={"category": category, "updated": updated},
    )
    emit_structured_log(
        "webapp_confirm_all",
        logger_name=__name__,
        actor_user_id=user_id,
        group_id=group_id,
        category=category,
        updated=updated,
    )
    await _refresh_group_alerts(bot, group_id)
    return web.json_response({"ok": True, "updated": updated, "counts": db.get_unresolved_counts(group_id)})


async def _api_users(request: web.Request) -> web.Response:
    """Return known users of the group for whitelist and moderator flows."""
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


async def _api_settings_toggle(request: web.Request) -> web.Response:
    """Toggle a single group setting from the WebApp."""
    user_id = _extract_user_id(request)
    bot: Bot = request.app["bot"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    try:
        group_id = int(payload.get("group_id"))
    except Exception:
        return web.json_response({"error": "group_id is required"}, status=400)

    setting = str(payload.get("setting") or "").strip().lower()
    if setting not in {"notify_pending", "blocked_alert_sound", "is_paused", "swipe_requires_confirm", "hide_confirmed_blocked"}:
        return web.json_response({"error": "bad setting"}, status=400)

    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    if setting == "notify_pending":
        db.set_notify_pending(group_id, not db.get_notify_pending(group_id))
        await _refresh_group_alerts(bot, group_id)
    elif setting == "blocked_alert_sound":
        db.set_blocked_alert_sound(group_id, not db.get_blocked_alert_sound(group_id))
    elif setting == "is_paused":
        db.set_group_paused(group_id, not db.is_group_paused(group_id))
    elif setting == "swipe_requires_confirm":
        db.set_swipe_requires_confirm(group_id, not db.get_swipe_requires_confirm(group_id))
    elif setting == "hide_confirmed_blocked":
        db.set_hide_confirmed_blocked(group_id, not db.get_hide_confirmed_blocked(group_id))

    group = db.get_group(group_id)
    db.record_audit_event(
        event_type="webapp_group_setting_toggled",
        source="webapp",
        actor_user_id=user_id,
        group_id=group_id,
        payload={
            "setting": setting,
            "new_value": bool(group[setting]) if group and setting in group.keys() else None,
        },
    )
    return web.json_response(
        {
            "ok": True,
            "group": {
                "group_id": group_id,
                "title": str(group["title"]) if group else str(group_id),
                "is_paused": bool(group["is_paused"]) if group else False,
                "notify_pending": bool(group["notify_pending"]) if group else True,
                "blocked_alert_sound": bool(group["blocked_alert_sound"]) if group else False,
                "swipe_requires_confirm": bool(group["swipe_requires_confirm"]) if group else True,
                "hide_confirmed_blocked": bool(group["hide_confirmed_blocked"]) if group else False,
            },
            "counts": db.get_unresolved_counts(group_id),
        }
    )


async def _api_list_triggers(request: web.Request) -> web.Response:
    """Return custom word or phrase triggers for the group."""
    user_id = _extract_user_id(request)
    try:
        group_id = _extract_group_id(request)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    trigger_type = request.query.get("type", "").strip().lower()
    search = request.query.get("q", "").strip()
    sort = request.query.get("sort", "alpha").strip().lower()
    if trigger_type not in {"word", "phrase"}:
        return web.json_response({"error": "bad trigger type"}, status=400)
    if sort not in {"alpha", "newest", "oldest"}:
        return web.json_response({"error": "bad trigger sort"}, status=400)
    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    rows = db.list_group_triggers(group_id, trigger_type, search=search, sort=sort)
    return web.json_response(
        {
            "items": [
                {
                    "trigger_id": int(row["trigger_id"]),
                    "trigger_type": str(row["trigger_type"]),
                    "value": str(row["value"]),
                }
                for row in rows
            ]
        }
    )


async def _api_add_trigger(request: web.Request) -> web.Response:
    """Create one or more custom triggers for the selected group."""
    user_id = _extract_user_id(request)
    try:
        payload = await request.json()
        group_id = int(payload.get("group_id"))
    except Exception:
        return web.json_response({"error": "group_id is required"}, status=400)

    trigger_type = str(payload.get("trigger_type") or "").strip().lower()
    value = str(payload.get("value") or "")
    if trigger_type not in {"word", "phrase"}:
        return web.json_response({"error": "bad trigger type"}, status=400)
    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        if trigger_type == "word":
            for word in _validate_word_trigger_input(value):
                db.add_group_trigger(group_id, "word", word, user_id)
        else:
            db.add_group_trigger(group_id, "phrase", _validate_phrase_trigger_input(value), user_id)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    db.record_audit_event(
        event_type="webapp_trigger_added",
        source="webapp",
        actor_user_id=user_id,
        group_id=group_id,
        payload={"trigger_type": trigger_type, "value": value},
    )
    rows = db.list_group_triggers(group_id, trigger_type)
    return web.json_response(
        {
            "items": [
                {
                    "trigger_id": int(row["trigger_id"]),
                    "trigger_type": str(row["trigger_type"]),
                    "value": str(row["value"]),
                }
                for row in rows
            ]
        }
    )


async def _api_update_trigger(request: web.Request) -> web.Response:
    """Update an existing custom trigger."""
    user_id = _extract_user_id(request)
    try:
        payload = await request.json()
        group_id = int(payload.get("group_id"))
        trigger_id = int(payload.get("trigger_id"))
    except Exception:
        return web.json_response({"error": "group_id and trigger_id are required"}, status=400)

    trigger_type = str(payload.get("trigger_type") or "").strip().lower()
    value = str(payload.get("value") or "")
    if trigger_type not in {"word", "phrase"}:
        return web.json_response({"error": "bad trigger type"}, status=400)
    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        normalized = _validate_phrase_trigger_input(value)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    ok = db.update_group_trigger(group_id, trigger_id, trigger_type, normalized)
    if not ok:
        return web.json_response({"error": "trigger not found"}, status=404)

    db.record_audit_event(
        event_type="webapp_trigger_updated",
        source="webapp",
        actor_user_id=user_id,
        group_id=group_id,
        payload={"trigger_id": trigger_id, "trigger_type": trigger_type, "value": normalized},
    )
    return web.json_response({"ok": True})


async def _api_delete_trigger(request: web.Request) -> web.Response:
    """Delete a custom trigger by id."""
    user_id = _extract_user_id(request)
    try:
        payload = await request.json()
        group_id = int(payload.get("group_id"))
        trigger_id = int(payload.get("trigger_id"))
    except Exception:
        return web.json_response({"error": "group_id and trigger_id are required"}, status=400)

    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    ok = db.delete_group_trigger(group_id, trigger_id)
    if not ok:
        return web.json_response({"error": "trigger not found"}, status=404)
    db.record_audit_event(
        event_type="webapp_trigger_deleted",
        source="webapp",
        actor_user_id=user_id,
        group_id=group_id,
        payload={"trigger_id": trigger_id},
    )
    return web.json_response({"ok": True})


async def _api_grant(request: web.Request) -> web.Response:
    """Grant whitelist status to a group user."""
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
    db.record_audit_event(
        event_type="webapp_whitelist_granted",
        source="webapp",
        actor_user_id=user_id,
        target_user_id=target_user_id,
        group_id=group_id,
    )
    return web.json_response({"ok": True})


async def _api_add_moderator(request: web.Request) -> web.Response:
    """Add a moderator to the selected group."""
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

    if target_user_id <= 0:
        return web.json_response({"error": "bad user_id"}, status=400)

    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    db.add_moderator(group_id, target_user_id, user_id)
    db.record_audit_event(
        event_type="webapp_moderator_added",
        source="webapp",
        actor_user_id=user_id,
        target_user_id=target_user_id,
        group_id=group_id,
    )
    return web.json_response({"ok": True})


async def _api_list_moderators(request: web.Request) -> web.Response:
    """Return moderators of the group for management UI."""
    user_id = _extract_user_id(request)
    try:
        group_id = _extract_group_id(request)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if not db.is_moderator(group_id, user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    query = request.query.get("q", "").strip()
    rows = db.list_group_moderators(group_id, query)
    items = []
    for row in rows:
        moderator_id = int(row["user_id"])
        items.append(
            {
                "user_id": moderator_id,
                "full_name": str(row["full_name"] or ""),
                "username": str(row["username"] or ""),
                "at_username": str(row["at_username"] or ""),
                "phone": str(row["phone"] or ""),
                "status": str(row["status"] or ""),
                "is_owner": moderator_id == int(row["created_by"]),
                "can_remove": moderator_id != int(row["created_by"]) and moderator_id != user_id,
            }
        )
    return web.json_response({"items": items, "total": len(items)})


async def _api_remove_moderator(request: web.Request) -> web.Response:
    """Remove a non-owner moderator from the selected group."""
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

    ok, text = db.remove_moderator(group_id, target_user_id, user_id)
    if not ok:
        return web.json_response({"error": text}, status=400)
    db.record_audit_event(
        event_type="webapp_moderator_removed",
        source="webapp",
        actor_user_id=user_id,
        target_user_id=target_user_id,
        group_id=group_id,
        payload={"message": text},
    )
    return web.json_response({"ok": True, "message": text})


async def _api_revoke(request: web.Request) -> web.Response:
    """Revoke whitelist status from a group user."""
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
    db.record_audit_event(
        event_type="webapp_whitelist_revoked",
        source="webapp",
        actor_user_id=user_id,
        target_user_id=target_user_id,
        group_id=group_id,
    )
    return web.json_response({"ok": True})


async def start_webapp_server(bot: Bot, host: str, port: int) -> tuple[web.AppRunner, web.TCPSite]:
    """Create and start the aiohttp WebApp server bound to the requested host and port."""
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", _root)
    app.router.add_get("/health", _health)
    app.router.add_get("/favicon.ico", _favicon)
    app.router.add_get("/webapp", _index)
    app.router.add_get("/webapp/app.js", _app_js)
    app.router.add_get("/webapp/app.css", _app_css)
    app.router.add_get("/webapp/api/groups", _api_groups)
    app.router.add_get("/webapp/api/overview", _api_overview)
    app.router.add_get("/webapp/api/ads", _api_ads)
    app.router.add_post("/webapp/api/ads/action", _api_ads_action)
    app.router.add_post("/webapp/api/ads/confirm-all", _api_ads_confirm_all)
    app.router.add_get("/webapp/api/users", _api_users)
    app.router.add_post("/webapp/api/settings/toggle", _api_settings_toggle)
    app.router.add_get("/webapp/api/triggers", _api_list_triggers)
    app.router.add_post("/webapp/api/triggers/add", _api_add_trigger)
    app.router.add_post("/webapp/api/triggers/update", _api_update_trigger)
    app.router.add_post("/webapp/api/triggers/delete", _api_delete_trigger)
    app.router.add_post("/webapp/api/whitelist/grant", _api_grant)
    app.router.add_post("/webapp/api/whitelist/revoke", _api_revoke)
    app.router.add_post("/webapp/api/moderators/add", _api_add_moderator)
    app.router.add_get("/webapp/api/moderators", _api_list_moderators)
    app.router.add_post("/webapp/api/moderators/remove", _api_remove_moderator)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    return runner, site
