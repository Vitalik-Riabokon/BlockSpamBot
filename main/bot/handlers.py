"""Telegram bot handlers and private alert lifecycle for moderation flows."""

import asyncio
import html
import logging
import re
from datetime import date, datetime, timezone
from urllib.parse import urlencode

from aiogram import Bot, F, Router, types
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.filters.command import CommandStart

from . import config, db, state
from .actions import apply_block_action, apply_permanent_mute, apply_unmute, delete_message_safe, log_action
from .classifier import (
    classify_message,
    contains_suspicious_domain,
    get_text,
    hard_illegal_detected,
    has_any_keyword,
    normalize_text_for_match,
)
from .models import GroupPolicy, ModerationResult, ModerationStatus
from .rules import CTA_KEYWORDS, SCAM_JOB_KEYWORDS, mention_regex, phone_regex
from .tunnel_notifier import get_public_webapp_base_url
from .config import TUNNEL_NOTIFY_USER_ID

router = Router()

CATEGORY_LABELS = {
    "blocked": "Заблоковані",
    "suspect": "Проблемна",
    "pending": "Не санкціонована",
    "confirmed": "Легалізовані",
}

CATEGORY_ORDER = ["blocked", "suspect", "pending", "confirmed"]

_PRIVATE_LAST_BOT_MESSAGE: dict[int, int] = {}
_PRIVATE_LAST_USER_COMMAND: dict[int, int] = {}
_LAST_DAILY_PRIVATE_CLEANUP_DATE: date | None = None


async def _send_context_message(
    message: types.Message,
    text: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> types.Message:
    """Send one contextual private message while replacing previous menu state."""
    if message.chat.type != "private" or not message.from_user:
        return await message.reply(text=text, reply_markup=reply_markup, parse_mode=parse_mode)

    user_id = message.from_user.id
    bot = message.bot

    persisted = db.get_private_context_state(user_id)
    old_bot_message_id = _PRIVATE_LAST_BOT_MESSAGE.get(user_id)
    if old_bot_message_id is None and persisted is not None:
        value = persisted["last_bot_message_id"]
        old_bot_message_id = int(value) if value is not None else None

    if old_bot_message_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=old_bot_message_id)
        except TelegramAPIError:
            pass
        db.untrack_private_bot_message(message.chat.id, old_bot_message_id)

    old_user_message_id = _PRIVATE_LAST_USER_COMMAND.get(user_id)
    if old_user_message_id is None and persisted is not None:
        value = persisted["last_user_message_id"]
        old_user_message_id = int(value) if value is not None else None

    if old_user_message_id and old_user_message_id != message.message_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=old_user_message_id)
        except TelegramAPIError:
            pass

    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except TelegramAPIError:
        pass

    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        disable_notification=True,
    )
    db.track_private_bot_message(message.chat.id, user_id, sent.message_id, "context")
    _PRIVATE_LAST_BOT_MESSAGE[user_id] = sent.message_id
    _PRIVATE_LAST_USER_COMMAND[user_id] = message.message_id
    db.upsert_private_context_state(
        user_id=user_id,
        chat_id=message.chat.id,
        last_bot_message_id=sent.message_id,
        last_user_message_id=message.message_id,
    )
    return sent


async def _callback_answer_message(
    callback: types.CallbackQuery,
    text: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> types.Message | None:
    """Send a callback follow-up message and track it for private cleanup when needed."""
    if not callback.message:
        return None
    kwargs = {
        "text": text,
        "reply_markup": reply_markup,
        "parse_mode": parse_mode,
    }
    if callback.message.chat.type == "private":
        kwargs["disable_notification"] = True
    sent = await callback.message.answer(**kwargs)
    if callback.from_user and callback.message.chat.type == "private":
        db.track_private_bot_message(callback.message.chat.id, callback.from_user.id, sent.message_id, "callback")
    return sent


def _command_arg(message: types.Message) -> str:
    """Extract raw command argument text after the command itself."""
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def _parse_bool(value: str) -> bool | None:
    """Parse a user-facing on/off style token into boolean form."""
    normalized = value.strip().lower()
    if normalized in {"1", "on", "yes", "true", "enable", "enabled"}:
        return True
    if normalized in {"0", "off", "no", "false", "disable", "disabled"}:
        return False
    return None


def _reply_user_id(message: types.Message) -> int | None:
    """Return replied user id when the command is used as a reply."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    return None


def _parse_target_user_and_groupspec(message: types.Message) -> tuple[int | None, str]:
    """Parse moderation target user id and optional group selector from a command."""
    raw = _command_arg(message)
    reply_id = _reply_user_id(message)
    if not raw:
        return reply_id, ""

    parts = raw.split(maxsplit=1)
    try:
        user_id = int(parts[0])
        group_spec = parts[1].strip() if len(parts) > 1 else ""
        return user_id, group_spec
    except ValueError:
        if reply_id is not None:
            return reply_id, raw
        return None, ""


def _resolve_selected_group_for_quick_action(user_id: int, chat_id: int) -> tuple[int | None, str | None]:
    """Resolve the current selected group for private quick actions."""
    selected = db.get_selected_group(user_id)
    if selected is not None and db.is_moderator(selected, user_id):
        return selected, None

    groups = db.list_user_groups(user_id)
    if not groups:
        return None, "У вас немає груп. Додайте бота в потрібну групу."
    if len(groups) > 1:
        return None, "У вас кілька груп. Спочатку оберіть групу через /menu."

    group_id = int(groups[0]["group_id"])
    db.set_selected_group(user_id, chat_id, group_id)
    return group_id, None


async def _set_dynamic_private_commands(bot: Bot, user_id: int) -> None:
    """Set a minimal slash-command list for a private chat."""
    commands = [
        types.BotCommand(command="start", description="Початок роботи з ботом"),
    ]
    try:
        await bot.set_my_commands(commands, scope=types.BotCommandScopeChat(chat_id=user_id))
    except TelegramAPIError:
        logging.exception("Failed to set dynamic commands for user %s", user_id)


async def _is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Return whether a Telegram user is currently admin/creator in the group."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {"creator", "administrator"}
    except Exception:
        logging.exception("Failed to check chat admin rights")
        return False


def _resolve_target_groups(message: types.Message, requester_id: int, group_spec: str) -> tuple[list[int], str | None]:
    """Resolve one or more target groups for commands supporting group scoping."""
    if message.chat.type in ("group", "supergroup") and not group_spec:
        group_id = message.chat.id
        if not db.is_moderator(group_id, requester_id):
            return [], "Ви не модератор цієї групи."
        return [group_id], None

    user_groups = db.list_user_groups(requester_id)
    available_ids = [int(row["group_id"]) for row in user_groups]
    if not available_ids:
        return [], "У вас немає прив'язаних груп. Додайте бота в потрібну групу."

    if not group_spec:
        if len(available_ids) == 1:
            return [available_ids[0]], None
        return [], "У вас кілька груп. Додайте group_id або all."

    spec = group_spec.strip().lower()
    if spec == "all":
        return available_ids, None

    parsed_ids: list[int] = []
    for token in re.split(r"[\s,]+", group_spec.strip()):
        if not token:
            continue
        try:
            parsed_ids.append(int(token))
        except ValueError:
            return [], f"Невірний group_id: {token}"

    if not parsed_ids:
        return [], "Не вказані group_id."

    unique_ids = sorted(set(parsed_ids))
    forbidden = [gid for gid in unique_ids if gid not in available_ids]
    if forbidden:
        return [], f"Немає доступу до group_id: {forbidden}"
    return unique_ids, None


def _utc_time(ts: int | None) -> str:
    """Format a unix timestamp as UTC text for moderator-facing messages."""
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _safe_username(username: str | None) -> str:
    """Return a Telegram-style username with leading @ or fallback dash."""
    if not username:
        return "-"
    if username.startswith("@"):
        return username
    return f"@{username}"


def _short_text(text: str, limit: int = 1400) -> str:
    """Trim text for alert cards while preserving readability."""
    value = text.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _build_webapp_url(group_id: int | None = None, section: str = "ads", category: str | None = None, ad_id: int | None = None) -> str:
    base = get_public_webapp_base_url()
    if not base:
        return ""
    params: dict[str, str | int] = {"section": section}
    if group_id is not None:
        params["group_id"] = group_id
    if category:
        params["category"] = category
    if ad_id is not None:
        params["ad_id"] = ad_id
    return f"{base}/webapp?{urlencode(params)}"


def _main_menu_text(group_id: int) -> str:
    group = db.get_group(group_id)
    title = str(group["title"]) if group else str(group_id)
    counts = db.get_unresolved_counts(group_id)
    return "\n".join(
        [
            f"Назва групи: {title}",
            "Стан реклам:",
            f"• Проблемна: {counts.get('suspect', 0)}",
            f"• Не санкціонована: {counts.get('pending', 0)}",
            f"• Легалізовані: {counts.get('confirmed', 0)}",
            f"• Заблоковані: {counts.get('blocked', 0)}",
        ]
    )


def _main_menu_kb(group_id: int) -> types.InlineKeyboardMarkup:
    webapp_url = _build_webapp_url(group_id=group_id, section="ads")
    keyboard: list[list[types.InlineKeyboardButton]] = []
    if webapp_url:
        keyboard.append([types.InlineKeyboardButton(text="Відкрити застосунок", web_app=types.WebAppInfo(url=webapp_url))])
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _ad_card_text(category: str, ad, idx: int, total: int, unresolved_count: int) -> str:
    label = CATEGORY_LABELS.get(category, category)
    user_id = int(ad["user_id"])
    username = _safe_username(ad["at_username"] or ad["username"])
    full_name = ad["full_name"] or "-"
    status = ad["user_status"] or "-"
    decision = ad["decision"] or "-"
    created = _utc_time(ad["created_at"])
    text = _short_text(str(ad["text"] or ""), 1800)

    return "\n".join(
        [
            f"<b>{html.escape(label)}</b> | {idx + 1}/{total}",
            f"Невирішених у категорії: {unresolved_count}",
            f"ad_id: {int(ad['ad_id'])}",
            f"Користувач: {html.escape(str(full_name))} ({user_id}) {html.escape(username)}",
            f"Поточний статус користувача: {html.escape(str(status))}",
            f"Рішення: {html.escape(str(decision))}",
            f"Час: {created}",
            "",
            "<b>Текст повідомлення:</b>",
            html.escape(text),
        ]
    )


def _alert_nav_kb(group_id: int, category: str, ad, idx: int, total: int) -> types.InlineKeyboardMarkup:
    ad_id = int(ad["ad_id"])
    webapp_url = _build_webapp_url(group_id=group_id, section="ads", category=category, ad_id=ad_id)
    keyboard: list[list[types.InlineKeyboardButton]] = []

    if webapp_url:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text="Відкрити застосунок",
                    web_app=types.WebAppInfo(url=webapp_url),
                )
            ]
        )

    if total > 1:
        prev_idx = max(0, idx - 1)
        next_idx = min(total - 1, idx + 1)
        keyboard.append(
            [
                types.InlineKeyboardButton(text="< Назад", callback_data=f"nav:{group_id}:{category}:{prev_idx}"),
                types.InlineKeyboardButton(text="Далі >", callback_data=f"nav:{group_id}:{category}:{next_idx}"),
            ]
        )
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _alert_ads_for_category(group_id: int, category: str) -> list:
    if category == "confirmed":
        return []
    return db.list_ads(group_id, category, unresolved_only=True)


async def _send_or_edit_private(
    bot: Bot,
    moderator_id: int,
    group_id: int,
    category: str,
    text: str,
    keyboard: types.InlineKeyboardMarkup,
    disable_notification: bool,
) -> None:
    state_row = db.get_alert_state(moderator_id, group_id, category)
    if state_row is not None:
        try:
            await bot.edit_message_text(
                chat_id=int(state_row["chat_id"]),
                message_id=int(state_row["message_id"]),
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        except TelegramAPIError:
            try:
                await bot.delete_message(chat_id=int(state_row["chat_id"]), message_id=int(state_row["message_id"]))
            except TelegramAPIError:
                pass
            db.untrack_private_bot_message(int(state_row["chat_id"]), int(state_row["message_id"]))
            db.clear_alert_state(moderator_id, group_id, category)

    try:
        sent = await bot.send_message(
            chat_id=moderator_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_notification=disable_notification,
        )
        db.set_alert_state(moderator_id, group_id, category, sent.message_id, moderator_id)
        db.track_private_bot_message(moderator_id, moderator_id, sent.message_id, f"alert:{category}")
    except TelegramAPIError:
        logging.info(
            "Cannot deliver alert to moderator=%s for group=%s. User likely did not /start in private.",
            moderator_id,
            group_id,
        )


async def _refresh_alert_for_moderator(bot: Bot, moderator_id: int, group_id: int, category: str) -> None:
    if category == "confirmed":
        row = db.get_alert_state(moderator_id, group_id, category)
        if row is not None:
            try:
                await bot.delete_message(chat_id=int(row["chat_id"]), message_id=int(row["message_id"]))
            except TelegramAPIError:
                pass
            db.untrack_private_bot_message(int(row["chat_id"]), int(row["message_id"]))
            db.clear_alert_state(moderator_id, group_id, category)
        return

    if category == "pending" and not db.get_notify_pending(group_id):
        row = db.get_alert_state(moderator_id, group_id, category)
        if row is not None:
            try:
                await bot.delete_message(chat_id=int(row["chat_id"]), message_id=int(row["message_id"]))
            except TelegramAPIError:
                pass
            db.untrack_private_bot_message(int(row["chat_id"]), int(row["message_id"]))
            db.clear_alert_state(moderator_id, group_id, category)
        return

    unresolved_ads = _alert_ads_for_category(group_id, category)
    unresolved_count = len(unresolved_ads)
    if unresolved_count == 0:
        return

    ad = unresolved_ads[0]
    text = _ad_card_text(category, ad, 0, unresolved_count, unresolved_count)
    keyboard = _alert_nav_kb(group_id, category, ad, 0, unresolved_count)
    disable_notification = False
    if category == "blocked":
        disable_notification = not db.get_blocked_alert_sound(group_id)

    await _send_or_edit_private(
        bot=bot,
        moderator_id=moderator_id,
        group_id=group_id,
        category=category,
        text=text,
        keyboard=keyboard,
        disable_notification=disable_notification,
    )


async def _refresh_alerts_for_group(bot: Bot, group_id: int, category: str) -> None:
    for moderator_id in db.list_moderators(group_id):
        await _refresh_alert_for_moderator(bot, moderator_id, group_id, category)


async def _clear_alerts_for_group(bot: Bot, group_id: int, category: str) -> None:
    for moderator_id in db.list_moderators(group_id):
        row = db.get_alert_state(moderator_id, group_id, category)
        if row is None:
            continue
        try:
            await bot.delete_message(chat_id=int(row["chat_id"]), message_id=int(row["message_id"]))
        except TelegramAPIError:
            pass
        db.untrack_private_bot_message(int(row["chat_id"]), int(row["message_id"]))
        db.clear_alert_state(moderator_id, group_id, category)


def _daily_summary_disable_notification(group_id: int) -> bool:
    counts = db.get_unresolved_counts(group_id)
    if counts.get("suspect", 0) > 0:
        return False
    if counts.get("pending", 0) > 0 and db.get_notify_pending(group_id):
        return False
    if counts.get("blocked", 0) > 0 and db.get_blocked_alert_sound(group_id):
        return False
    return True


def _pick_summary_group_for_user(user_id: int) -> int | None:
    selected_group_id = db.get_selected_group(user_id)
    if selected_group_id is not None and db.is_moderator(int(selected_group_id), user_id):
        return int(selected_group_id)

    groups = db.list_user_groups(user_id)
    if not groups:
        return None
    return int(groups[0]["group_id"])


async def _wipe_private_chat_history(bot: Bot, chat_id: int, window: int = 250) -> None:
    """Delete a recent window of messages in private chat to hard-reset the bot conversation."""
    try:
        probe = await bot.send_message(chat_id=chat_id, text="…", disable_notification=True)
    except TelegramAPIError:
        return

    start_message_id = max(1, int(probe.message_id) - window)
    for message_id in range(int(probe.message_id), start_message_id - 1, -1):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            continue


async def _run_daily_private_cleanup(bot: Bot) -> None:
    active_user_ids = db.list_private_users_with_activity()
    chat_ids_to_wipe: set[int] = set()
    for user_id in active_user_ids:
        row = db.get_private_context_state(user_id)
        if row and row["chat_id"] is not None:
            chat_ids_to_wipe.add(int(row["chat_id"]))
        else:
            chat_ids_to_wipe.add(int(user_id))

    tracked_messages = db.list_tracked_private_bot_messages()
    for row in tracked_messages:
        chat_id = int(row["chat_id"])
        message_id = int(row["message_id"])
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            pass
        db.untrack_private_bot_message(chat_id, message_id)

    db.clear_all_alert_states()
    db.clear_all_private_context_bot_messages()
    db.clear_tracked_private_bot_messages()
    _PRIVATE_LAST_BOT_MESSAGE.clear()

    for chat_id in chat_ids_to_wipe:
        await _wipe_private_chat_history(bot, chat_id)

    for user_id in active_user_ids:
        row = db.get_private_context_state(user_id)
        chat_id = int(row["chat_id"]) if row and row["chat_id"] is not None else user_id
        group_id = _pick_summary_group_for_user(user_id)

        if group_id is None:
            webapp_url = _build_webapp_url(section="ads")
            keyboard_rows: list[list[types.InlineKeyboardButton]] = []
            if webapp_url:
                keyboard_rows.append(
                    [types.InlineKeyboardButton(text="Відкрити застосунок", web_app=types.WebAppInfo(url=webapp_url))]
                )
            sent = await bot.send_message(
                chat_id=chat_id,
                text="Немає прив'язаних груп.\nДодайте бота в потрібну групу або надішліть свій id адміну.",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None,
                disable_notification=True,
            )
            db.track_private_bot_message(chat_id, user_id, sent.message_id, "daily_state")
            db.upsert_private_context_state(
                user_id=user_id,
                chat_id=chat_id,
                last_bot_message_id=sent.message_id,
                last_user_message_id=None,
            )
            _PRIVATE_LAST_BOT_MESSAGE[user_id] = sent.message_id
            continue

        sent = await bot.send_message(
            chat_id=chat_id,
            text=_main_menu_text(group_id),
            reply_markup=_main_menu_kb(group_id),
            disable_notification=_daily_summary_disable_notification(group_id),
        )
        db.track_private_bot_message(chat_id, user_id, sent.message_id, "daily_state")
        db.upsert_private_context_state(
            user_id=user_id,
            chat_id=chat_id,
            last_bot_message_id=sent.message_id,
            last_user_message_id=None,
            selected_group_id=group_id,
        )
        _PRIVATE_LAST_BOT_MESSAGE[user_id] = sent.message_id


async def periodic_private_context_cleanup(
    bot: Bot,
    check_every_seconds: int = 60,
) -> None:
    """Run daily private-message cleanup at 23:00 Europe/Berlin."""
    global _LAST_DAILY_PRIVATE_CLEANUP_DATE
    while True:
        try:
            now = datetime.now().astimezone()
            if now.hour == 23 and (_LAST_DAILY_PRIVATE_CLEANUP_DATE is None or _LAST_DAILY_PRIVATE_CLEANUP_DATE != now.date()):
                await _run_daily_private_cleanup(bot)
                _LAST_DAILY_PRIVATE_CLEANUP_DATE = now.date()
        except Exception:
            logging.exception("private context cleanup failed")
        await asyncio.sleep(check_every_seconds)


async def _show_groups_menu(message: types.Message, intro: bool = False) -> None:
    if not message.from_user:
        return
    if message.chat.type == "private":
        await _set_dynamic_private_commands(message.bot, message.from_user.id)

    rows = db.list_user_groups(message.from_user.id)
    if not rows:
        webapp_url = _build_webapp_url(section="ads")
        keyboard_rows = []
        if webapp_url:
            keyboard_rows.append([types.InlineKeyboardButton(text="Відкрити застосунок", web_app=types.WebAppInfo(url=webapp_url))])
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None
        await _send_context_message(
            message,
            "Немає прив'язаних груп.\nДодайте бота в потрібну групу або надішліть свій id адміну.",
            reply_markup=keyboard,
        )
        return
    selected_group_id = db.get_selected_group(message.from_user.id)
    if selected_group_id is None or not db.is_moderator(int(selected_group_id), message.from_user.id):
        selected_group_id = int(rows[0]["group_id"])
        db.set_selected_group(message.from_user.id, message.chat.id, selected_group_id)
    await _send_context_message(message, _main_menu_text(selected_group_id), reply_markup=_main_menu_kb(selected_group_id))


async def _refresh_private_context_for_user(bot: Bot, user_id: int) -> bool:
    row = db.get_private_context_state(user_id)
    if row is None:
        return False

    chat_id = int(row["chat_id"])
    last_bot_message_id = row["last_bot_message_id"]
    if last_bot_message_id is not None:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(last_bot_message_id))
        except TelegramAPIError:
            pass
        db.untrack_private_bot_message(chat_id, int(last_bot_message_id))

    selected_group_id = row["selected_group_id"]
    if selected_group_id is not None and db.is_moderator(int(selected_group_id), user_id):
        group = db.get_group(int(selected_group_id))
        if group is not None:
            text = _main_menu_text(int(selected_group_id))
            keyboard = _main_menu_kb(int(selected_group_id))
            sent = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                disable_notification=True,
            )
            db.upsert_private_context_state(
                user_id=user_id,
                chat_id=chat_id,
                last_bot_message_id=sent.message_id,
                last_user_message_id=None,
                selected_group_id=int(selected_group_id),
            )
            db.track_private_bot_message(chat_id, user_id, sent.message_id, "context")
            _PRIVATE_LAST_BOT_MESSAGE[user_id] = sent.message_id
            return True

    groups = db.list_user_groups(user_id)
    if not groups:
        webapp_url = _build_webapp_url(section="ads")
        rows = []
        if webapp_url:
            rows.append([types.InlineKeyboardButton(text="Відкрити застосунок", web_app=types.WebAppInfo(url=webapp_url))])
        sent = await bot.send_message(
            chat_id=chat_id,
            text="Немає прив'язаних груп.\nДодайте бота в потрібну групу або надішліть свій id адміну.",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
            disable_notification=True,
        )
        db.upsert_private_context_state(
            user_id=user_id,
            chat_id=chat_id,
            last_bot_message_id=sent.message_id,
            last_user_message_id=None,
        )
        db.track_private_bot_message(chat_id, user_id, sent.message_id, "context")
        _PRIVATE_LAST_BOT_MESSAGE[user_id] = sent.message_id
        return True

    target_group_id = int(selected_group_id) if selected_group_id and db.is_moderator(int(selected_group_id), user_id) else int(groups[0]["group_id"])
    db.set_selected_group(user_id, chat_id, target_group_id)
    sent = await bot.send_message(chat_id=chat_id, text=_main_menu_text(target_group_id), reply_markup=_main_menu_kb(target_group_id), disable_notification=True)
    db.upsert_private_context_state(
        user_id=user_id,
        chat_id=chat_id,
        last_bot_message_id=sent.message_id,
        last_user_message_id=None,
        selected_group_id=target_group_id,
    )
    db.track_private_bot_message(chat_id, user_id, sent.message_id, "context")
    _PRIVATE_LAST_BOT_MESSAGE[user_id] = sent.message_id
    return True


async def _can_enforce_group_actions(bot: Bot, chat_id: int) -> bool:
    if config.TEST_MODE:
        return False
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
    except Exception:
        return False

    if member.status == "creator":
        return True
    if member.status != "administrator":
        return False

    can_delete = bool(getattr(member, "can_delete_messages", False))
    can_restrict = bool(getattr(member, "can_restrict_members", False))
    return can_delete and can_restrict


def _should_block_split_chain(policy: GroupPolicy, recent_items: list[tuple[int, str]]) -> tuple[bool, str]:
    """
    Split-message protection:
    if last N messages (within window) together look like aggressive scam ad,
    block immediately.
    """
    if len(recent_items) < config.SPLIT_MAX_MESSAGES:
        return False, ""

    combined_text = " ".join([text for _, text in recent_items if text]).strip()
    if not combined_text:
        return False, ""

    normalized = normalize_text_for_match(combined_text)
    has_contact = contains_suspicious_domain(normalized) or bool(mention_regex.search(normalized) or phone_regex.search(normalized))
    has_cta = has_any_keyword(normalized, CTA_KEYWORDS)
    has_scam_job = has_any_keyword(normalized, SCAM_JOB_KEYWORDS)

    if hard_illegal_detected(normalized, policy) and has_contact:
        return True, combined_text
    if has_scam_job and has_cta and has_contact:
        return True, combined_text
    return False, combined_text


@router.message(CommandStart())
async def start_handler(message: types.Message) -> None:
    await _show_groups_menu(message, intro=True)


@router.message(Command("menu"))
async def menu_handler(message: types.Message) -> None:
    await _show_groups_menu(message, intro=False)


@router.message(Command("help"))
@router.message(Command("mod_help"))
async def help_handler(message: types.Message) -> None:
    await _send_context_message(message, 
        "\n".join(
            [
                "Основні:",
                "/start, /menu, /my_id, /chat_id, /my_groups",
                "",
                "Групи:",
                "Додавання нової групи: автоматично після додавання бота в групу",
                "/delete_group [group_id] - видалити групу і вивести бота",
                "/pause_group [group_id|all] - 1 команда для увімк/вимк модерації",
                "",
                "Модератори:",
                "Додавання модератора: через кнопку в Налаштуваннях групи",
                "/list_moderators [group_id]",
                "",
                "Список підтверджених (whitelist):",
                "Надання постійної легалізації: через кнопку в Налаштуваннях групи",
                "/list_whitelist [group_id]",
                "",
                "Жорсткі тригери (hardwords):",
                "/add_hardword <слово/фраза> [group_id|all]",
                "/remove_hardword <слово/фраза> [group_id|all]",
                "/list_hardwords [group_id]",
                "",
                "Сповіщення:",
                "/toggle_pending - перемкнути сповіщення (1 клік, для обраної групи)",
                "/toggle_blocked_sound - перемкнути звук блокувань (1 клік, для обраної групи)",
                "/set_pending_alerts <on|off> [group_id|all]",
                "/set_blocked_sound <on|off> [group_id|all]",
                "",
                "Як додати модератора: /start -> /my_id -> /add_moderator",
            ]
        )
    )


@router.message(Command("my_id"))
async def my_id(message: types.Message) -> None:
    if message.from_user:
        await _send_context_message(message, f"Ваш ідентифікатор користувача: {message.from_user.id}")


@router.message(Command("chat_id"))
async def chat_id(message: types.Message) -> None:
    await _send_context_message(message, f"Поточний ідентифікатор чату: {message.chat.id}")


@router.message(Command("register_group"))
async def register_group(message: types.Message, bot: Bot) -> None:
    if message.chat.type not in ("group", "supergroup"):
        await _send_context_message(message, "Команда працює тільки в групі.")
        return
    if not message.from_user:
        return

    is_admin = await _is_group_admin(bot, message.chat.id, message.from_user.id)
    if not is_admin:
        await _send_context_message(message, "Тільки адмін групи може зареєструвати групу в боті.")
        return

    already = db.is_group_registered(message.chat.id)
    if already and not db.is_moderator(message.chat.id, message.from_user.id):
        await _send_context_message(message, "Ця група вже прив'язана іншими модераторами. Немає доступу.")
        return

    created = db.register_group(message.chat.id, message.chat.title or str(message.chat.id), message.from_user.id)
    db.upsert_user(
        user_id=message.from_user.id,
        full_name=message.from_user.full_name,
        username=message.from_user.username,
        at_username=f"@{message.from_user.username}" if message.from_user.username else None,
        status="in_group",
    )
    db.upsert_group_user(message.chat.id, message.from_user.id)
    if created:
        await _send_context_message(message, "✅ Група зареєстрована. Ви додані як модератор цієї групи.")
    else:
        await _send_context_message(message, "✅ Група вже зареєстрована. Ви підтверджені як модератор.")


@router.message(Command("delete_group"))
async def delete_group(message: types.Message, bot: Bot) -> None:
    if not message.from_user:
        return

    raw = _command_arg(message)
    if message.chat.type in ("group", "supergroup") and not raw:
        target_group_id = message.chat.id
    else:
        try:
            target_group_id = int(raw)
        except ValueError:
            await _send_context_message(message, "Використання: /delete_group [group_id]. У групі можна без аргументу.")
            return

    ok, text = db.delete_group(target_group_id, message.from_user.id)
    if not ok:
        await _send_context_message(message, f"⛔ {text}")
        return
    await _send_context_message(message, f"✅ {text}")
    try:
        await bot.leave_chat(target_group_id)
    except Exception:
        logging.exception("Failed to leave chat %s after delete_group", target_group_id)


@router.my_chat_member()
async def bot_membership_changed(event: types.ChatMemberUpdated) -> None:
    if event.chat.type not in ("group", "supergroup"):
        return

    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    if old_status in {"left", "kicked"} and new_status in {"member", "administrator"}:
        actor = event.from_user
        if actor:
            if not db.is_group_registered(event.chat.id):
                db.register_group(event.chat.id, event.chat.title or str(event.chat.id), actor.id)
                logging.info("Auto-registered group %s by user %s", event.chat.id, actor.id)
            elif db.is_moderator(event.chat.id, actor.id):
                db.register_group(event.chat.id, event.chat.title or str(event.chat.id), actor.id)
    if new_status in {"left", "kicked"}:
        db.delete_group_force(event.chat.id)
        logging.info("Group %s deactivated because bot left/was removed", event.chat.id)


@router.message(Command("my_groups"))
async def my_groups(message: types.Message) -> None:
    if not message.from_user:
        return
    rows = db.list_user_groups(message.from_user.id)
    if not rows:
        await _send_context_message(message, "Немає прив'язаних груп. Додайте бота в потрібну групу.")
        return
    lines = ["Ваші групи:"]
    for row in rows:
        owner_mark = " (creator)" if int(row["created_by"]) == message.from_user.id else ""
        lines.append(
            "- "
            f"{row['title']} | id={row['group_id']} | "
            f"сповіщення для не підтверджених={'увімкнено' if row['notify_pending'] else 'вимкнено'} | "
            f"звук авто-блокувань={'увімкнено' if row['blocked_alert_sound'] else 'вимкнено'} | "
            f"модерація={'призупинена' if row['is_paused'] else 'активна'}{owner_mark}"
        )
    await _send_context_message(message, "\n".join(lines))


@router.message(Command("pause_group"))
async def pause_group(message: types.Message) -> None:
    if not message.from_user:
        return
    command_group_spec = _command_arg(message)
    if not command_group_spec:
        selected_group, selected_err = _resolve_selected_group_for_quick_action(message.from_user.id, message.chat.id)
        if selected_group is None:
            target_groups, err = [], selected_err
        else:
            target_groups, err = [selected_group], None
    else:
        target_groups, err = _resolve_target_groups(message, message.from_user.id, command_group_spec)
    if err:
        await _send_context_message(message, err)
        return
    statuses = []
    for gid in target_groups:
        paused_now = db.is_group_paused(gid)
        new_value = not paused_now
        db.set_group_paused(gid, new_value)
        statuses.append(f"{gid}: {'вимкнено' if new_value else 'увімкнено'}")
    await _set_dynamic_private_commands(message.bot, message.from_user.id)
    await _send_context_message(message, f"✅ Стан модерації груп: {', '.join(statuses)}")


@router.message(Command("resume_group"))
async def resume_group(message: types.Message) -> None:
    if not message.from_user:
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, _command_arg(message))
    if err:
        await _send_context_message(message, err)
        return
    for gid in target_groups:
        db.set_group_paused(gid, False)
    await _set_dynamic_private_commands(message.bot, message.from_user.id)
    await _send_context_message(message, f"✅ Модерація відновлена для group_id: {target_groups}")


@router.message(Command("set_pending_alerts"))
async def set_pending_alerts(message: types.Message) -> None:
    if not message.from_user:
        return
    raw = _command_arg(message)
    parts = raw.split(maxsplit=1)
    if not parts:
        await _send_context_message(message, "Використання: /set_pending_alerts <on|off> [group_id|all]")
        return
    parsed = _parse_bool(parts[0])
    if parsed is None:
        await _send_context_message(message, "Використання: /set_pending_alerts <on|off> [group_id|all]")
        return
    group_spec = parts[1].strip() if len(parts) > 1 else ""
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await _send_context_message(message, err)
        return
    for gid in target_groups:
        db.set_notify_pending(gid, parsed)
    await _set_dynamic_private_commands(message.bot, message.from_user.id)
    await _send_context_message(message, f"✅ Сповіщення для адекватних, не підтверджених: {'увімкнено' if parsed else 'вимкнено'} для {target_groups}")


@router.message(Command("set_blocked_sound"))
async def set_blocked_sound(message: types.Message) -> None:
    if not message.from_user:
        return
    raw = _command_arg(message)
    parts = raw.split(maxsplit=1)
    if not parts:
        await _send_context_message(message, "Використання: /set_blocked_sound <on|off> [group_id|all]")
        return
    parsed = _parse_bool(parts[0])
    if parsed is None:
        await _send_context_message(message, "Використання: /set_blocked_sound <on|off> [group_id|all]")
        return
    group_spec = parts[1].strip() if len(parts) > 1 else ""
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await _send_context_message(message, err)
        return
    for gid in target_groups:
        db.set_blocked_alert_sound(gid, parsed)
    await _set_dynamic_private_commands(message.bot, message.from_user.id)
    await _send_context_message(message, f"✅ Звук для авто-блокувань: {'увімкнено' if parsed else 'вимкнено'} для {target_groups}")


@router.message(Command("set_review_alerts"))
async def set_review_alerts_alias(message: types.Message) -> None:
    await set_pending_alerts(message)


@router.message(Command("toggle_pending"))
async def toggle_pending_command(message: types.Message) -> None:
    if not message.from_user:
        return
    group_id, err = _resolve_selected_group_for_quick_action(message.from_user.id, message.chat.id)
    if err:
        await _send_context_message(message, err)
        return
    if group_id is None:
        return
    new_value = not db.get_notify_pending(group_id)
    db.set_notify_pending(group_id, new_value)
    await _set_dynamic_private_commands(message.bot, message.from_user.id)
    await _send_context_message(
        message,
        f"Група {group_id}: {'увімкнути' if not new_value else 'вимкнути'} сповіщення від не санкціонованих реклам. Поточний стан: {'увімкнено' if new_value else 'вимкнено'}.",
    )


@router.message(Command("toggle_blocked_sound"))
async def toggle_blocked_sound_command(message: types.Message) -> None:
    if not message.from_user:
        return
    group_id, err = _resolve_selected_group_for_quick_action(message.from_user.id, message.chat.id)
    if err:
        await _send_context_message(message, err)
        return
    if group_id is None:
        return
    new_value = not db.get_blocked_alert_sound(group_id)
    db.set_blocked_alert_sound(group_id, new_value)
    await _set_dynamic_private_commands(message.bot, message.from_user.id)
    await _send_context_message(
        message,
        f"Група {group_id}: {'увімкнути' if not new_value else 'вимкнути'} сповіщення від заблокованих реклам. Поточний стан: {'увімкнено' if new_value else 'вимкнено'}.",
    )


@router.message(Command("add_moderator"))
async def add_moderator(message: types.Message) -> None:
    if not message.from_user:
        return
    target_user_id, group_spec = _parse_target_user_and_groupspec(message)
    if target_user_id is None:
        await _send_context_message(message, "Використання: /add_moderator <user_id> [group_id|all] або reply")
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await _send_context_message(message, err)
        return
    for gid in target_groups:
        db.add_moderator(gid, target_user_id, message.from_user.id)
    await _send_context_message(message, 
        f"✅ Додано модератора {target_user_id} у group_id: {target_groups}\n"
        "Нагадування: модератор має запустити /start у приваті з ботом."
    )


@router.message(Command("remove_moderator"))
async def remove_moderator(message: types.Message) -> None:
    if not message.from_user:
        return
    target_user_id, group_spec = _parse_target_user_and_groupspec(message)
    if target_user_id is None:
        await _send_context_message(message, "Використання: /remove_moderator <user_id> [group_id|all] або reply")
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await _send_context_message(message, err)
        return
    results = []
    for gid in target_groups:
        ok, text = db.remove_moderator(gid, target_user_id, message.from_user.id)
        results.append(f"група {gid}: {'ok' if ok else 'помилка'} ({text})")
    await _send_context_message(message, "\n".join(results))


@router.message(Command("list_moderators"))
async def list_moderators(message: types.Message) -> None:
    if not message.from_user:
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, _command_arg(message))
    if err:
        await _send_context_message(message, err)
        return
    await _send_context_message(message, "\n".join([f"група {gid}: {db.list_moderators(gid)}" for gid in target_groups]))


@router.message(Command("add_whitelist"))
async def add_whitelist(message: types.Message) -> None:
    if not message.from_user:
        return
    target_user_id, group_spec = _parse_target_user_and_groupspec(message)
    if target_user_id is None:
        await _send_context_message(message, "Використання: /add_whitelist <user_id> [group_id|all] або reply")
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await _send_context_message(message, err)
        return
    for gid in target_groups:
        db.add_whitelist(gid, target_user_id, message.from_user.id)
    await _send_context_message(message, f"✅ Додано у whitelist {target_user_id} для group_id: {target_groups}")


@router.message(Command("remove_whitelist"))
async def remove_whitelist(message: types.Message) -> None:
    if not message.from_user:
        return
    target_user_id, group_spec = _parse_target_user_and_groupspec(message)
    if target_user_id is None:
        await _send_context_message(message, "Використання: /remove_whitelist <user_id> [group_id|all] або reply")
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await _send_context_message(message, err)
        return
    for gid in target_groups:
        db.remove_whitelist(gid, target_user_id)
    await _send_context_message(message, f"✅ Видалено з whitelist {target_user_id} для group_id: {target_groups}")


@router.message(Command("list_whitelist"))
async def list_whitelist(message: types.Message) -> None:
    if not message.from_user:
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, _command_arg(message))
    if err:
        await _send_context_message(message, err)
        return
    await _send_context_message(message, "\n".join([f"група {gid}: {db.list_whitelist(gid)}" for gid in target_groups]))


@router.message(Command("add_hardword"))
async def add_hardword(message: types.Message) -> None:
    if not message.from_user:
        return
    raw = _command_arg(message)
    if not raw:
        await _send_context_message(message, "Використання: /add_hardword <слово/фраза> [group_id|all]")
        return

    parts = raw.split(" | ", maxsplit=1)
    word = parts[0].strip().lower()
    group_spec = parts[1].strip() if len(parts) > 1 else ""
    if not group_spec and " " in word:
        maybe_word, maybe_group = word.rsplit(" ", maxsplit=1)
        if maybe_group.lower() == "all" or re.fullmatch(r"[\d\-,\s]+", maybe_group):
            word = maybe_word.strip()
            group_spec = maybe_group.strip()

    if not word:
        await _send_context_message(message, "Порожнє слово/фраза.")
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await _send_context_message(message, err)
        return
    for gid in target_groups:
        db.add_hardword(gid, word, message.from_user.id)
    await _send_context_message(message, f"✅ hardword додано: '{word}' для group_id: {target_groups}")


@router.message(Command("remove_hardword"))
async def remove_hardword(message: types.Message) -> None:
    if not message.from_user:
        return
    raw = _command_arg(message)
    if not raw:
        await _send_context_message(message, "Використання: /remove_hardword <слово/фраза> [group_id|all]")
        return

    parts = raw.split(" | ", maxsplit=1)
    word = parts[0].strip().lower()
    group_spec = parts[1].strip() if len(parts) > 1 else ""
    if not group_spec and " " in word:
        maybe_word, maybe_group = word.rsplit(" ", maxsplit=1)
        if maybe_group.lower() == "all" or re.fullmatch(r"[\d\-,\s]+", maybe_group):
            word = maybe_word.strip()
            group_spec = maybe_group.strip()

    if not word:
        await _send_context_message(message, "Порожнє слово/фраза.")
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await _send_context_message(message, err)
        return
    for gid in target_groups:
        db.remove_hardword(gid, word)
    await _send_context_message(message, f"✅ hardword видалено: '{word}' для group_id: {target_groups}")


@router.message(Command("list_hardwords"))
async def list_hardwords(message: types.Message) -> None:
    if not message.from_user:
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, _command_arg(message))
    if err:
        await _send_context_message(message, err)
        return
    await _send_context_message(message, "\n".join([f"група {gid}: {db.list_hardwords(gid)}" for gid in target_groups]))


@router.callback_query(F.data == "menu:groups")
async def cb_menu_groups(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    await _set_dynamic_private_commands(callback.message.bot, callback.from_user.id)
    rows = db.list_user_groups(callback.from_user.id)
    if not rows:
        text = "Немає прив'язаних груп.\nДодайте бота в потрібну групу або надішліть свій id адміну."
        webapp_url = _build_webapp_url(section="ads")
        keyboard = (
            types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text="Відкрити застосунок", web_app=types.WebAppInfo(url=webapp_url))]]
            )
            if webapp_url
            else None
        )
        try:
            await callback.message.edit_text(text=text, reply_markup=keyboard)
        except TelegramAPIError:
            await _callback_answer_message(callback, text=text, reply_markup=keyboard)
        await callback.answer()
        return

    selected_group_id = db.get_selected_group(callback.from_user.id)
    if selected_group_id is None or not db.is_moderator(int(selected_group_id), callback.from_user.id):
        selected_group_id = int(rows[0]["group_id"])
        db.set_selected_group(callback.from_user.id, callback.message.chat.id, selected_group_id)

    text = _main_menu_text(int(selected_group_id))
    keyboard = _main_menu_kb(int(selected_group_id))
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await _callback_answer_message(callback, text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "menu:add_group_help_global")
async def cb_menu_add_group_help_global(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer()
        return
    text = (
        "Щоб додати нову групу:\n"
        "1) Додайте бота в потрібну групу.\n"
        "2) Група прив’яжеться автоматично до того, хто додав бота.\n"
        "3) Поверніться в застосунок і оберіть нову групу."
    )
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="Оновити список груп", callback_data="menu:groups")]]
    )
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await _callback_answer_message(callback, text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "tunnel:refresh_webapp")
async def cb_tunnel_refresh_webapp(callback: types.CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer()
        return
    if callback.from_user.id != TUNNEL_NOTIFY_USER_ID:
        await callback.answer("Немає доступу", show_alert=True)
        return

    refreshed = 0
    for row in db.list_active_private_contexts():
        try:
            ok = await _refresh_private_context_for_user(bot, int(row["user_id"]))
        except Exception:
            logging.exception("Failed to refresh private context for user %s", row["user_id"])
            ok = False
        if ok:
            refreshed += 1

    await delete_message_safe(bot, callback.message.chat.id, callback.message.message_id)
    db.untrack_private_bot_message(callback.message.chat.id, callback.message.message_id)
    await callback.answer(f"WebApp оновлено для {refreshed} чатів", show_alert=True)


@router.callback_query(F.data.startswith("menu:group:"))
async def cb_menu_group(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    try:
        group_id = int(parts[2])
    except ValueError:
        await callback.answer("Невірний group_id", show_alert=True)
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return
    db.set_selected_group(callback.from_user.id, callback.message.chat.id, group_id)
    await _set_dynamic_private_commands(callback.message.bot, callback.from_user.id)
    group = db.get_group(group_id)
    if group is None:
        await callback.answer("Групу не знайдено", show_alert=True)
        return
    text = _main_menu_text(group_id)
    keyboard = _main_menu_kb(group_id)
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await _callback_answer_message(callback, text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("nav:"))
async def cb_nav_category(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return
    try:
        group_id = int(parts[1])
        idx = int(parts[3])
    except ValueError:
        await callback.answer("Некоректні параметри", show_alert=True)
        return
    category = parts[2]
    if category not in CATEGORY_ORDER:
        await callback.answer("Невірна категорія", show_alert=True)
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return

    unresolved_ads = _alert_ads_for_category(group_id, category)
    if not unresolved_ads:
        await callback.answer("Немає нових реклам у цій категорії")
        return

    idx = max(0, min(idx, len(unresolved_ads) - 1))
    ad = unresolved_ads[idx]
    text = _ad_card_text(category, ad, idx, len(unresolved_ads), len(unresolved_ads))
    keyboard = _alert_nav_kb(group_id, category, ad, idx, len(unresolved_ads))
    try:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=keyboard)
    except TelegramAPIError:
        await callback.answer("Не вдалося оновити повідомлення", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(callback: types.CallbackQuery) -> None:
    await callback.answer()


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def moderate_message(message: types.Message, bot: Bot) -> None:
    try:
        group_id = message.chat.id
        if not db.is_group_registered(group_id):
            return
        if db.is_group_paused(group_id):
            return

        author = message.from_user
        if not author or author.is_bot:
            return

        db.upsert_user(
            user_id=author.id,
            full_name=author.full_name,
            username=author.username,
            at_username=f"@{author.username}" if author.username else None,
            status="in_group",
        )
        db.upsert_group_user(group_id, author.id)

        policy_data = db.get_policy(group_id)
        policy = GroupPolicy(
            group_id=group_id,
            whitelist_user_ids=policy_data["whitelist"],
            authorized_user_ids=policy_data["authorized"],
            hard_block_extra_keywords=policy_data["hardwords"],
        )

        text = get_text(message)
        recent_items = state.remember_recent_message(group_id, author.id, message.message_id, text)
        split_force_block, split_combined_text = _should_block_split_chain(policy, recent_items)

        result = classify_message(message, policy)
        if result.status == ModerationStatus.SAFE_TEXT:
            if not split_force_block:
                return
            result = ModerationResult(
                status=ModerationStatus.AD_BLOCKED,
                score=config.BLOCK_SCORE_THRESHOLD,
                reasons=["split_chain_block"],
                ad_intent=True,
            )

        if split_force_block and result.status != ModerationStatus.AD_BLOCKED:
            result = ModerationResult(
                status=ModerationStatus.AD_BLOCKED,
                score=max(result.score, config.BLOCK_SCORE_THRESHOLD),
                reasons=result.reasons + ["split_chain_block"],
                ad_intent=True,
            )

        ad_duplicate_count = state.ad_duplicate_count(group_id, author.id, normalize_text_for_match(text))
        if (
            result.status != ModerationStatus.AD_BLOCKED
            and ad_duplicate_count >= config.AD_DUPLICATE_BLOCK_COUNT
        ):
            result = ModerationResult(
                status=ModerationStatus.AD_BLOCKED,
                score=max(result.score, config.BLOCK_SCORE_THRESHOLD),
                reasons=result.reasons + ["ad_duplicate_block"],
                ad_intent=True,
            )

        has_media = bool(
            message.photo
            or message.video
            or message.document
            or message.animation
            or message.audio
            or message.voice
            or message.video_note
            or message.sticker
        )
        in_whitelist = author.id in policy.whitelist_user_ids
        hard_block_hit = "hard_illegal" in result.reasons
        duplicate_block_hit = "ad_duplicate_block" in result.reasons

        if result.status == ModerationStatus.AD_BLOCKED:
            if in_whitelist and not hard_block_hit and not split_force_block and not duplicate_block_hit:
                db.create_ad(
                    group_id=group_id,
                    user_id=author.id,
                    source_chat_id=message.chat.id,
                    source_message_id=message.message_id,
                    text=text,
                    has_media=has_media,
                    category="confirmed",
                    decision="pending",
                    requires_action=True,
                )
                await log_action(message.chat, author, message, result)
                return

            can_enforce = await _can_enforce_group_actions(bot, message.chat.id)

            if split_force_block and can_enforce:
                for old_message_id, _old_text in recent_items:
                    if old_message_id == message.message_id:
                        continue
                    await delete_message_safe(bot, message.chat.id, old_message_id)

            ad_text = split_combined_text if split_force_block and split_combined_text else text
            ad_id = db.create_ad(
                group_id=group_id,
                user_id=author.id,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
                text=ad_text,
                has_media=has_media,
                category="blocked",
                decision=("muted_auto" if can_enforce else "would_block"),
                requires_action=True,
            )
            state.add_strike(group_id, author.id)
            if can_enforce:
                await apply_block_action(bot, group_id, message.chat, author, message, result, ad_id=ad_id)
            else:
                await log_action(message.chat, author, message, result)
                db.update_ad_decision(
                    ad_id=ad_id,
                    decision="would_block",
                    moderator_id=None,
                    requires_action=True,
                    category="blocked",
                    note="dry-run mode (no admin rights or TEST_MODE)",
                )
            await _refresh_alerts_for_group(bot, group_id, "blocked")
            return

        if result.status == ModerationStatus.AD_SUSPECT:
            state.mark_suspect(group_id, author.id)
            if state.should_escalate_suspect(group_id, author.id):
                escalated = ModerationResult(
                    status=ModerationStatus.AD_BLOCKED,
                    score=max(result.score, config.BLOCK_SCORE_THRESHOLD),
                    reasons=result.reasons + ["suspect_escalation"],
                    ad_intent=True,
                )
                ad_id = db.create_ad(
                    group_id=group_id,
                    user_id=author.id,
                    source_chat_id=message.chat.id,
                    source_message_id=message.message_id,
                    text=text,
                    has_media=has_media,
                    category="blocked",
                    decision="muted_auto",
                    requires_action=True,
                )
                state.add_strike(group_id, author.id)
                await apply_block_action(bot, group_id, message.chat, author, message, escalated, ad_id=ad_id)
                await _refresh_alerts_for_group(bot, group_id, "blocked")
                return

            db.create_ad(
                group_id=group_id,
                user_id=author.id,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
                text=text,
                has_media=has_media,
                category="suspect",
                decision="pending",
                requires_action=True,
            )
            await log_action(message.chat, author, message, result)
            await _refresh_alerts_for_group(bot, group_id, "suspect")
            return

        if result.status == ModerationStatus.AD_PENDING_AUTH:
            db.create_ad(
                group_id=group_id,
                user_id=author.id,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
                text=text,
                has_media=has_media,
                category="pending",
                decision="pending",
                requires_action=True,
            )
            await log_action(message.chat, author, message, result)
            if db.get_notify_pending(group_id):
                await _refresh_alerts_for_group(bot, group_id, "pending")
            return

        if result.status == ModerationStatus.AD_ALLOWED:
            db.create_ad(
                group_id=group_id,
                user_id=author.id,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
                text=text,
                has_media=has_media,
                category="confirmed",
                decision="pending",
                requires_action=True,
            )
            await log_action(message.chat, author, message, result)
            return
    except Exception:
        logging.exception("Помилка в moderate_message")


@router.edited_message(F.chat.type.in_({"group", "supergroup"}))
async def moderate_edited_message(message: types.Message, bot: Bot) -> None:
    await moderate_message(message, bot)


@router.message(F.new_chat_members)
async def new_member_monitor(message: types.Message) -> None:
    if message.chat.type not in {"group", "supergroup"}:
        return
    for user in message.new_chat_members:
        db.upsert_user(
            user_id=user.id,
            full_name=user.full_name,
            username=user.username,
            at_username=f"@{user.username}" if user.username else None,
            status="in_group",
        )
        db.upsert_group_user(message.chat.id, user.id)


@router.message(F.left_chat_member)
async def left_member_monitor(message: types.Message) -> None:
    user = message.left_chat_member
    if not user:
        return
    db.upsert_user(
        user_id=user.id,
        full_name=user.full_name,
        username=user.username,
        at_username=f"@{user.username}" if user.username else None,
        status="removed",
    )
