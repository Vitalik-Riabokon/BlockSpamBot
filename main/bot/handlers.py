import asyncio
import html
import logging
import re
from datetime import datetime, timezone

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
_PRIVATE_INPUT_FLOW: dict[int, tuple[str, int]] = {}
_WHITELIST_SEARCH: dict[tuple[int, int], str] = {}
_WHITELIST_OFFSET: dict[tuple[int, int], int] = {}


async def _send_context_message(
    message: types.Message,
    text: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> types.Message:
    """
    Private chat UX:
    - keep only one contextual bot message
    - delete previous command message(s)
    """
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
    _PRIVATE_LAST_BOT_MESSAGE[user_id] = sent.message_id
    _PRIVATE_LAST_USER_COMMAND[user_id] = message.message_id
    db.upsert_private_context_state(
        user_id=user_id,
        chat_id=message.chat.id,
        last_bot_message_id=sent.message_id,
        last_user_message_id=message.message_id,
    )
    return sent


def _command_arg(message: types.Message) -> str:
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def _parse_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"1", "on", "yes", "true", "enable", "enabled"}:
        return True
    if normalized in {"0", "off", "no", "false", "disable", "disabled"}:
        return False
    return None


def _reply_user_id(message: types.Message) -> int | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    return None


def _parse_target_user_and_groupspec(message: types.Message) -> tuple[int | None, str]:
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
    selected = db.get_selected_group(user_id)
    if selected is not None and db.is_moderator(selected, user_id):
        return selected, None

    groups = db.list_user_groups(user_id)
    if not groups:
        return None, "У вас немає груп. Додайте бота в групу і виконайте /register_group."
    if len(groups) > 1:
        return None, "У вас кілька груп. Спочатку оберіть групу через /menu."

    group_id = int(groups[0]["group_id"])
    db.set_selected_group(user_id, chat_id, group_id)
    return group_id, None


async def _set_dynamic_private_commands(bot: Bot, user_id: int) -> None:
    selected_group_id = db.get_selected_group(user_id)
    if selected_group_id is None:
        user_groups = db.list_user_groups(user_id)
        if len(user_groups) == 1:
            selected_group_id = int(user_groups[0]["group_id"])
            db.set_selected_group(user_id, user_id, selected_group_id)
    pause_desc = "Увімк/вимк модерацію обраної групи"
    pending_desc = "Увімк/вимк сповіщення від не санкціонованих реклам"
    blocked_desc = "Увімк/вимк звук від заблокованих реклам"

    if selected_group_id is not None and db.is_moderator(selected_group_id, user_id):
        paused = db.is_group_paused(selected_group_id)
        pending_on = db.get_notify_pending(selected_group_id)
        blocked_sound_on = db.get_blocked_alert_sound(selected_group_id)

        pause_desc = (
            "Увімкнути модерацію обраної групи"
            if paused
            else "Вимкнути модерацію обраної групи"
        )
        pending_desc = (
            "Увімкнути сповіщення від не санкціонованих реклам"
            if not pending_on
            else "Вимкнути сповіщення від не санкціонованих реклам"
        )
        blocked_desc = (
            "Увімкнути звук від заблокованих реклам"
            if not blocked_sound_on
            else "Вимкнути звук від заблокованих реклам"
        )

    commands = [
        types.BotCommand(command="start", description="Початок роботи з ботом"),
        types.BotCommand(command="menu", description="Головне меню модератора"),
        types.BotCommand(command="my_groups", description="Мої підключені групи"),
        types.BotCommand(command="my_id", description="Показати мій user id"),
        types.BotCommand(command="mod_help", description="Довідка по командах"),
        types.BotCommand(command="toggle_pending", description=pending_desc),
        types.BotCommand(command="toggle_blocked_sound", description=blocked_desc),
        types.BotCommand(command="pause_group", description=pause_desc),
    ]
    try:
        await bot.set_my_commands(commands, scope=types.BotCommandScopeChat(chat_id=user_id))
    except TelegramAPIError:
        logging.exception("Failed to set dynamic commands for user %s", user_id)


async def _is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {"creator", "administrator"}
    except Exception:
        logging.exception("Failed to check chat admin rights")
        return False


def _resolve_target_groups(message: types.Message, requester_id: int, group_spec: str) -> tuple[list[int], str | None]:
    if message.chat.type in ("group", "supergroup") and not group_spec:
        group_id = message.chat.id
        if not db.is_moderator(group_id, requester_id):
            return [], "Ви не модератор цієї групи."
        return [group_id], None

    user_groups = db.list_user_groups(requester_id)
    available_ids = [int(row["group_id"]) for row in user_groups]
    if not available_ids:
        return [], "У вас немає прив'язаних груп. Спочатку /register_group в групі."

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
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _safe_username(username: str | None) -> str:
    if not username:
        return "-"
    if username.startswith("@"):
        return username
    return f"@{username}"


def _short_text(text: str, limit: int = 1400) -> str:
    value = text.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _profile_url(user_id: int) -> str:
    return f"tg://user?id={user_id}"


def _extract_first_url(text: str) -> str | None:
    match = re.search(r"https?://\S+", text)
    if not match:
        return None
    return match.group(0)


def _message_link(chat_id: int, message_id: int) -> str | None:
    chat_str = str(chat_id)
    if chat_str.startswith("-100"):
        return f"https://t.me/c/{chat_str[4:]}/{message_id}"
    return None


def _group_row_to_label(row) -> str:
    title = str(row["title"])
    gid = int(row["group_id"])
    return f"{title} ({gid})"


def _menu_groups_kb(rows: list) -> types.InlineKeyboardMarkup:
    keyboard: list[list[types.InlineKeyboardButton]] = []
    for row in rows:
        gid = int(row["group_id"])
        keyboard.append([types.InlineKeyboardButton(text=_group_row_to_label(row), callback_data=f"menu:group:{gid}")])
    keyboard.append([types.InlineKeyboardButton(text="Додати нову групу", callback_data="menu:add_group_help_global")])
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _group_dashboard_kb(group_id: int, has_many_groups: bool) -> types.InlineKeyboardMarkup:
    keyboard = [
        [types.InlineKeyboardButton(text="Реклами", callback_data=f"menu:ads:{group_id}")],
        [types.InlineKeyboardButton(text="Налаштування", callback_data=f"menu:settings:{group_id}")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _ads_menu_kb(group_id: int, counts: dict[str, int]) -> types.InlineKeyboardMarkup:
    keyboard = [
        [types.InlineKeyboardButton(text=f"{CATEGORY_LABELS['blocked']} ({counts.get('blocked', 0)})", callback_data=f"category:{group_id}:blocked")],
        [types.InlineKeyboardButton(text=f"{CATEGORY_LABELS['suspect']} ({counts.get('suspect', 0)})", callback_data=f"category:{group_id}:suspect")],
        [types.InlineKeyboardButton(text=f"{CATEGORY_LABELS['pending']} ({counts.get('pending', 0)})", callback_data=f"category:{group_id}:pending")],
        [types.InlineKeyboardButton(text=f"{CATEGORY_LABELS['confirmed']} ({counts.get('confirmed', 0)})", callback_data=f"category:{group_id}:confirmed")],
        [types.InlineKeyboardButton(text="Назад", callback_data=f"menu:group:{group_id}")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _settings_menu_kb(group_id: int, notify_pending: bool, blocked_sound: bool, paused: bool) -> types.InlineKeyboardMarkup:
    webapp_url = ""
    if config.WEBAPP_BASE_URL:
        base = config.WEBAPP_BASE_URL.rstrip("/")
        webapp_url = f"{base}/webapp?group_id={group_id}"

    keyboard = [
        [types.InlineKeyboardButton(text=f"Сповіщення від не санкціонованих реклам: {'УВІМКНЕНО' if notify_pending else 'ВИМКНЕНО'}", callback_data=f"toggle:pending:{group_id}")],
        [types.InlineKeyboardButton(text=f"Звук від заблокованих реклам: {'УВІМКНЕНО' if blocked_sound else 'ВИМКНЕНО'}", callback_data=f"toggle:blocked_sound:{group_id}")],
        [types.InlineKeyboardButton(text=("Відновити модерацію" if paused else "Призупинити модерацію"), callback_data=(f"toggle:resume:{group_id}" if paused else f"toggle:pause:{group_id}"))],
        [types.InlineKeyboardButton(text="Додати модератора", callback_data=f"settings:add_moderator:{group_id}")],
        [types.InlineKeyboardButton(text="Надати постійну легалізацію", callback_data=f"settings:add_whitelist:{group_id}")],
        [types.InlineKeyboardButton(text="Назад", callback_data=f"menu:group:{group_id}")],
    ]
    if webapp_url:
        keyboard.insert(
            5,
            [
                types.InlineKeyboardButton(
                    text="WebApp: Легалізація",
                    web_app=types.WebAppInfo(url=webapp_url),
                )
            ],
        )
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _whitelist_state_key(moderator_id: int, group_id: int) -> tuple[int, int]:
    return moderator_id, group_id


def _format_user_for_whitelist(row) -> str:
    full_name = str(row["full_name"] or "").strip()
    if not full_name:
        full_name = "Без імені"
    username = _safe_username(row["at_username"] or row["username"])
    return f"{full_name} ({int(row['user_id'])}) {username}"


def _whitelist_picker_kb(
    group_id: int,
    rows: list,
    offset: int,
    total: int,
) -> types.InlineKeyboardMarkup:
    keyboard: list[list[types.InlineKeyboardButton]] = []
    for row in rows:
        user_id = int(row["user_id"])
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=_format_user_for_whitelist(row),
                    callback_data=f"whitelist:ask:{group_id}:{user_id}",
                )
            ]
        )

    nav_row: list[types.InlineKeyboardButton] = []
    prev_offset = max(0, offset - 8)
    next_offset = offset + 8
    if offset > 0:
        nav_row.append(
            types.InlineKeyboardButton(text="< Назад", callback_data=f"whitelist:page:{group_id}:{prev_offset}")
        )
    if next_offset < total:
        nav_row.append(
            types.InlineKeyboardButton(text="Далі >", callback_data=f"whitelist:page:{group_id}:{next_offset}")
        )
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([types.InlineKeyboardButton(text="Пошук", callback_data=f"whitelist:search:{group_id}")])
    keyboard.append([types.InlineKeyboardButton(text="Скинути пошук", callback_data=f"whitelist:reset:{group_id}")])
    keyboard.append([types.InlineKeyboardButton(text="Назад", callback_data=f"menu:settings:{group_id}")])
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _render_whitelist_picker(
    callback: types.CallbackQuery,
    group_id: int,
    moderator_id: int,
    offset: int = 0,
) -> None:
    key = _whitelist_state_key(moderator_id, group_id)
    query = _WHITELIST_SEARCH.get(key, "")
    rows, total = db.list_group_users_for_whitelist(group_id, query, limit=8, offset=offset)
    _WHITELIST_OFFSET[key] = max(0, offset)
    query_line = f"Пошук: {query}" if query else "Пошук: (не задано)"
    text = (
        "Оберіть користувача для постійної легалізації.\n"
        f"{query_line}\n"
        f"Знайдено: {total}"
    )
    keyboard = _whitelist_picker_kb(group_id, rows, offset, total)
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


def _category_view_ads(group_id: int, category: str) -> list:
    if category == "blocked":
        return db.list_ads(group_id, category, unresolved_only=False)
    return db.list_ads(group_id, category, unresolved_only=True)


def _settings_menu_text() -> str:
    lines = [
        "Налаштування для обраної групи:",
        "• Сповіщення для не санкціонованих: вмикає/вимикає push модератору.",
        "• Звук від заблокованих реклам: звук при авто-блокуванні.",
        "• Пауза модерації: тимчасово зупиняє обробку повідомлень.",
        "• Додати модератора: додавання за user_id.",
        "• Надати постійну легалізацію: список користувачів + пошук.",
    ]
    if config.WEBAPP_BASE_URL:
        lines.append("• WebApp: live-пошук і легалізація без зайвих повідомлень.")
    return "\n".join(lines)


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


def _ad_actions_kb(group_id: int, category: str, ad, idx: int, total: int) -> types.InlineKeyboardMarkup:
    ad_id = int(ad["ad_id"])
    user_id = int(ad["user_id"])
    text = str(ad["text"] or "")
    first_url = _extract_first_url(text) if category != "blocked" else None
    message_url = _message_link(int(ad["source_chat_id"]), int(ad["source_message_id"])) if category != "blocked" else None
    keyboard: list[list[types.InlineKeyboardButton]] = []

    if category == "blocked":
        keyboard.append(
            [
                types.InlineKeyboardButton(text="Розблокувати", callback_data=f"action:{group_id}:{category}:{ad_id}:unmute:{idx}"),
                types.InlineKeyboardButton(text="Підтвердити", callback_data=f"action:{group_id}:{category}:{ad_id}:ack:{idx}"),
            ]
        )
        keyboard.append([types.InlineKeyboardButton(text="Підтвердити всі", callback_data=f"allask:{group_id}:{category}")])
    elif category == "suspect":
        keyboard.append(
            [
                types.InlineKeyboardButton(text="Підтвердити", callback_data=f"action:{group_id}:{category}:{ad_id}:approve:{idx}"),
                types.InlineKeyboardButton(text="Заблокувати", callback_data=f"action:{group_id}:{category}:{ad_id}:block:{idx}"),
            ]
        )
    else:
        keyboard.append(
            [
                types.InlineKeyboardButton(text="Підтвердити", callback_data=f"action:{group_id}:{category}:{ad_id}:approve:{idx}"),
                types.InlineKeyboardButton(text="Заблокувати", callback_data=f"action:{group_id}:{category}:{ad_id}:block:{idx}"),
            ]
        )
        keyboard.append([types.InlineKeyboardButton(text="Підтвердити всі", callback_data=f"allask:{group_id}:{category}")])

    if total > 1:
        prev_idx = max(0, idx - 1)
        next_idx = min(total - 1, idx + 1)
        keyboard.append(
            [
                types.InlineKeyboardButton(text="< Назад", callback_data=f"nav:{group_id}:{category}:{prev_idx}"),
                types.InlineKeyboardButton(text="Далі >", callback_data=f"nav:{group_id}:{category}:{next_idx}"),
            ]
        )

    profile_buttons = [types.InlineKeyboardButton(text="Профіль", url=_profile_url(user_id))]
    if message_url:
        profile_buttons.append(types.InlineKeyboardButton(text="Відкрити в чаті", url=message_url))
    if first_url:
        profile_buttons.append(types.InlineKeyboardButton(text="Посилання", url=first_url))
    keyboard.append(profile_buttons)
    keyboard.append([types.InlineKeyboardButton(text="До категорій", callback_data=f"menu:ads:{group_id}")])
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _alert_empty_kb(group_id: int, category: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="OK", callback_data=f"ackalert:{group_id}:{category}")],
            [types.InlineKeyboardButton(text="До категорій", callback_data=f"menu:ads:{group_id}")],
        ]
    )


async def _render_category(callback: types.CallbackQuery, group_id: int, category: str, idx: int = 0) -> None:
    if not callback.from_user:
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return
    db.set_selected_group(callback.from_user.id, callback.message.chat.id, group_id)

    ads = _category_view_ads(group_id, category)
    unresolved = db.get_unresolved_counts(group_id).get(category, 0)
    if not ads:
        text = f"<b>{html.escape(CATEGORY_LABELS.get(category, category))}</b>\nСписок порожній."
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text="До категорій", callback_data=f"menu:ads:{group_id}")]]
        )
    else:
        clamped = max(0, min(idx, len(ads) - 1))
        ad = ads[clamped]
        text = _ad_card_text(category, ad, clamped, len(ads), unresolved)
        keyboard = _ad_actions_kb(group_id, category, ad, clamped, len(ads))

    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard, parse_mode="HTML")
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


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
    except TelegramAPIError:
        logging.info(
            "Cannot deliver alert to moderator=%s for group=%s. User likely did not /start in private.",
            moderator_id,
            group_id,
        )


async def _refresh_alert_for_moderator(bot: Bot, moderator_id: int, group_id: int, category: str) -> None:
    if category in {"blocked", "confirmed"}:
        row = db.get_alert_state(moderator_id, group_id, category)
        if row is not None:
            try:
                await bot.delete_message(chat_id=int(row["chat_id"]), message_id=int(row["message_id"]))
            except TelegramAPIError:
                pass
            db.clear_alert_state(moderator_id, group_id, category)
        return

    if category == "pending" and not db.get_notify_pending(group_id):
        row = db.get_alert_state(moderator_id, group_id, category)
        if row is not None:
            try:
                await bot.delete_message(chat_id=int(row["chat_id"]), message_id=int(row["message_id"]))
            except TelegramAPIError:
                pass
            db.clear_alert_state(moderator_id, group_id, category)
        return

    unresolved_ads = db.list_ads(group_id, category, unresolved_only=True)
    unresolved_count = len(unresolved_ads)
    if unresolved_count == 0:
        row = db.get_alert_state(moderator_id, group_id, category)
        if row is None:
            return
        try:
            await bot.delete_message(chat_id=int(row["chat_id"]), message_id=int(row["message_id"]))
        except TelegramAPIError:
            pass
        db.clear_alert_state(moderator_id, group_id, category)
        return

    ad = unresolved_ads[0]
    text = _ad_card_text(category, ad, 0, unresolved_count, unresolved_count)
    keyboard = _ad_actions_kb(group_id, category, ad, 0, unresolved_count)
    disable_notification = False

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
        db.clear_alert_state(moderator_id, group_id, category)


async def periodic_private_context_cleanup(
    bot: Bot,
    ttl_seconds: int = 15 * 60,
    check_every_seconds: int = 60,
) -> None:
    """
    Periodically delete stale private context messages.
    """
    while True:
        try:
            stale_rows = db.list_private_contexts_older_than(ttl_seconds)
            for row in stale_rows:
                message_id = row["last_bot_message_id"]
                if message_id is None:
                    continue
                try:
                    await bot.delete_message(chat_id=int(row["chat_id"]), message_id=int(message_id))
                except TelegramAPIError:
                    pass
                user_id = int(row["user_id"])
                db.clear_private_context_bot_message(user_id)
                groups = db.list_user_groups(user_id)
                if groups:
                    sent = await bot.send_message(
                        chat_id=int(row["chat_id"]),
                        text="Оберіть групу для керування:",
                        reply_markup=_menu_groups_kb(groups),
                        disable_notification=True,
                    )
                    db.upsert_private_context_state(
                        user_id=user_id,
                        chat_id=int(row["chat_id"]),
                        last_bot_message_id=sent.message_id,
                        last_user_message_id=None,
                    )
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
        prefix = "Бот активний.\n\n" if intro else ""
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text="Додати нову групу", callback_data="menu:add_group_help_global")]]
        )
        await _send_context_message(
            message,
            f"{prefix}Немає груп.\n1) Додайте бота в групу\n2) У групі виконайте /register_group",
            reply_markup=keyboard,
        )
        return
    prefix = ""
    if intro:
        prefix = (
            "Бот активний.\n"
            "Кожен модератор має запустити /start у приваті.\n\n"
        )
    await _send_context_message(message, f"{prefix}Оберіть групу для керування:", reply_markup=_menu_groups_kb(rows))


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
                "/register_group - реєстрація групи (виконати в групі)",
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
        await _send_context_message(message, "Немає прив'язаних груп. Додайте бота в групу і виконайте /register_group.")
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
    _PRIVATE_INPUT_FLOW.pop(callback.from_user.id, None)
    await _set_dynamic_private_commands(callback.message.bot, callback.from_user.id)
    rows = db.list_user_groups(callback.from_user.id)
    if not rows:
        text = (
            "Немає прив'язаних груп.\n"
            "1) Додайте бота в потрібну групу.\n"
            "2) У групі виконайте /register_group."
        )
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text="Додати нову групу", callback_data="menu:add_group_help_global")]]
        )
        try:
            await callback.message.edit_text(text=text, reply_markup=keyboard)
        except TelegramAPIError:
            await callback.message.answer(text=text, reply_markup=keyboard)
        await callback.answer()
        return
    text = "Оберіть групу для керування:"
    keyboard = _menu_groups_kb(rows)
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "menu:add_group_help_global")
async def cb_menu_add_group_help_global(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer()
        return
    text = (
        "Щоб додати нову групу:\n"
        "1) Додайте бота в потрібну групу.\n"
        "2) У групі виконайте /register_group (адміном групи).\n"
        "3) Поверніться в /menu і оберіть групу."
    )
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="Оновити список груп", callback_data="menu:groups")]]
    )
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "menu:help_global")
async def cb_help_global(callback: types.CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return
    text = "Скористайтеся /mod_help для повного списку команд."
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="До груп", callback_data="menu:groups")]]
    )
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


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
    _PRIVATE_INPUT_FLOW.pop(callback.from_user.id, None)
    await _set_dynamic_private_commands(callback.message.bot, callback.from_user.id)
    rows = db.list_user_groups(callback.from_user.id)
    group = db.get_group(group_id)
    if group is None:
        await callback.answer("Групу не знайдено", show_alert=True)
        return
    text = "\n".join(
        [
            f"Група: {group['title']} ({group_id})",
            f"Модерація: {'ПРИЗУПИНЕНА' if group['is_paused'] else 'АКТИВНА'}",
            f"Сповіщення для не підтверджених: {'УВІМКНЕНО' if group['notify_pending'] else 'ВИМКНЕНО'}",
            f"Звук авто-блокувань: {'УВІМКНЕНО' if group['blocked_alert_sound'] else 'ВИМКНЕНО'}",
        ]
    )
    keyboard = _group_dashboard_kb(group_id, len(rows) > 1)
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("menu:ads:"))
async def cb_menu_ads(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
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

    counts = db.get_unresolved_counts(group_id)
    text = "\n".join(
        [
            "Розділ реклам:",
            "• Заблоковані: уже авто-заблоковані, можна розблокувати або підтвердити.",
            "• Проблемна: підозрілі, потрібне рішення модератора.",
            "• Не санкціонована: адекватна реклама без легалізації.",
            "• Легалізована: реклама від користувачів з постійною легалізацією.",
        ]
    )
    keyboard = _ads_menu_kb(group_id, counts)
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("menu:settings:"))
async def cb_menu_settings(callback: types.CallbackQuery) -> None:
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
    _PRIVATE_INPUT_FLOW.pop(callback.from_user.id, None)
    await _set_dynamic_private_commands(callback.message.bot, callback.from_user.id)
    group = db.get_group(group_id)
    if group is None:
        await callback.answer("Групу не знайдено", show_alert=True)
        return
    text = _settings_menu_text()
    keyboard = _settings_menu_kb(
        group_id=group_id,
        notify_pending=bool(group["notify_pending"]),
        blocked_sound=bool(group["blocked_alert_sound"]),
        paused=bool(group["is_paused"]),
    )
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


async def _show_settings_after_toggle(callback: types.CallbackQuery, group_id: int) -> None:
    group = db.get_group(group_id)
    if group is None:
        return
    text = _settings_menu_text()
    keyboard = _settings_menu_kb(
        group_id=group_id,
        notify_pending=bool(group["notify_pending"]),
        blocked_sound=bool(group["blocked_alert_sound"]),
        paused=bool(group["is_paused"]),
    )
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("menu:help:"))
async def cb_menu_help(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
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
    text = "\n".join(
        [
            "Коротко по розділах:",
            "- Заблоковані: авто-блоки, можна розблокувати або OK.",
            "- Підозрілі: підтвердити або заблокувати.",
            "- Адекватні не підтверджені: підтвердити/блокувати/підтвердити всі.",
            "- Від підтверджених: без push, але доступні в меню.",
        ]
    )
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="Назад", callback_data=f"menu:group:{group_id}")]]
    )
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("menu:stats:"))
async def cb_menu_stats(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
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
    text = "Статистика буде додана окремим етапом. Дані вже зберігаються в БД."
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="Назад", callback_data=f"menu:group:{group_id}")]]
    )
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("menu:add_group_help:"))
async def cb_menu_add_group_help(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return
    try:
        group_id = int(parts[3])
    except ValueError:
        await callback.answer("Невірний ідентифікатор групи", show_alert=True)
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return
    text = (
        "Щоб додати нову групу:\n"
        "1) Додайте бота в потрібну групу.\n"
        "2) У групі виконайте /register_group (адміном групи).\n"
        "3) Поверніться в /menu і оберіть групу."
    )
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="Назад у головне меню", callback_data=f"menu:group:{group_id}")]]
    )
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("toggle:pending:"))
async def cb_toggle_pending(callback: types.CallbackQuery, bot: Bot) -> None:
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
    current = db.get_notify_pending(group_id)
    new_value = not current
    db.set_notify_pending(group_id, new_value)
    if new_value:
        await _refresh_alerts_for_group(bot, group_id, "pending")
    else:
        await _clear_alerts_for_group(bot, group_id, "pending")
    await _set_dynamic_private_commands(callback.message.bot, callback.from_user.id)
    await callback.answer(f"Сповіщення: {'УВІМКНЕНО' if new_value else 'ВИМКНЕНО'}")
    await _show_settings_after_toggle(callback, group_id)


@router.callback_query(F.data.startswith("toggle:blocked_sound:"))
async def cb_toggle_blocked_sound(callback: types.CallbackQuery) -> None:
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
    current = db.get_blocked_alert_sound(group_id)
    db.set_blocked_alert_sound(group_id, not current)
    await _set_dynamic_private_commands(callback.message.bot, callback.from_user.id)
    await callback.answer(f"Звук авто-блокувань: {'УВІМКНЕНО' if not current else 'ВИМКНЕНО'}")
    await _show_settings_after_toggle(callback, group_id)


@router.callback_query(F.data.startswith("toggle:pause:"))
async def cb_toggle_pause(callback: types.CallbackQuery) -> None:
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
    db.set_group_paused(group_id, True)
    await _set_dynamic_private_commands(callback.message.bot, callback.from_user.id)
    await callback.answer("Модерацію призупинено")
    await _show_settings_after_toggle(callback, group_id)


@router.callback_query(F.data.startswith("toggle:resume:"))
async def cb_toggle_resume(callback: types.CallbackQuery) -> None:
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
    db.set_group_paused(group_id, False)
    await _set_dynamic_private_commands(callback.message.bot, callback.from_user.id)
    await callback.answer("Модерацію відновлено")
    await _show_settings_after_toggle(callback, group_id)


@router.callback_query(F.data.startswith("settings:add_group_help:"))
async def cb_settings_add_group_help(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return
    try:
        group_id = int(parts[3])
    except ValueError:
        await callback.answer("Невірний ідентифікатор групи", show_alert=True)
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return
    text = (
        "Щоб додати нову групу:\n"
        "1) Додайте бота в потрібну групу.\n"
        "2) У групі виконайте команду /register_group (адміном групи).\n"
        "3) Поверніться в /menu і оберіть групу."
    )
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="Назад у налаштування", callback_data=f"menu:settings:{group_id}")]]
    )
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("settings:add_moderator:"))
async def cb_settings_add_moderator(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    group_id = int(parts[2])
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return
    _PRIVATE_INPUT_FLOW[callback.from_user.id] = ("add_moderator", group_id)
    text = (
        "Надішліть user_id нового модератора одним повідомленням.\n"
        "Перед цим новий модератор має натиснути /start у боті."
    )
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="Скасувати", callback_data=f"menu:settings:{group_id}")]]
    )
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("settings:add_whitelist:"))
async def cb_settings_add_whitelist(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    group_id = int(parts[2])
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return
    key = _whitelist_state_key(callback.from_user.id, group_id)
    _WHITELIST_SEARCH.pop(key, None)
    _WHITELIST_OFFSET[key] = 0
    await _render_whitelist_picker(callback, group_id, callback.from_user.id, offset=0)


@router.callback_query(F.data.startswith("whitelist:page:"))
async def cb_whitelist_page(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return
    try:
        group_id = int(parts[2])
        offset = max(0, int(parts[3]))
    except ValueError:
        await callback.answer("Некоректні параметри", show_alert=True)
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return
    await _render_whitelist_picker(callback, group_id, callback.from_user.id, offset=offset)


@router.callback_query(F.data.startswith("whitelist:search:"))
async def cb_whitelist_search(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    try:
        group_id = int(parts[2])
    except ValueError:
        await callback.answer("Некоректна група", show_alert=True)
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return
    _PRIVATE_INPUT_FLOW[callback.from_user.id] = ("whitelist_search", group_id)
    text = (
        "Введіть текст пошуку (ім'я, @username, телефон або user_id).\n"
        "Кожне нове повідомлення оновлює список.\n"
        "Приклад: 410477852 або @nick."
    )
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="Скасувати", callback_data=f"whitelist:page:{group_id}:0")]]
    )
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("whitelist:reset:"))
async def cb_whitelist_reset(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    try:
        group_id = int(parts[2])
    except ValueError:
        await callback.answer("Некоректна група", show_alert=True)
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return
    key = _whitelist_state_key(callback.from_user.id, group_id)
    _WHITELIST_SEARCH.pop(key, None)
    _WHITELIST_OFFSET[key] = 0
    await _render_whitelist_picker(callback, group_id, callback.from_user.id, offset=0)


@router.callback_query(F.data.startswith("whitelist:ask:"))
async def cb_whitelist_ask(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return
    try:
        group_id = int(parts[2])
        target_user_id = int(parts[3])
    except ValueError:
        await callback.answer("Некоректні дані", show_alert=True)
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return
    _PRIVATE_INPUT_FLOW.pop(callback.from_user.id, None)
    text = (
        f"Надати постійну легалізацію користувачу {target_user_id}?\n"
        "Після цього його адекватні реклами підуть у категорію 'Легалізовані'."
    )
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text="Так", callback_data=f"whitelist:confirm:{group_id}:{target_user_id}"),
                types.InlineKeyboardButton(text="Ні", callback_data=f"whitelist:page:{group_id}:0"),
            ]
        ]
    )
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("whitelist:confirm:"))
async def cb_whitelist_confirm(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return
    try:
        group_id = int(parts[2])
        target_user_id = int(parts[3])
    except ValueError:
        await callback.answer("Некоректні дані", show_alert=True)
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return
    _PRIVATE_INPUT_FLOW.pop(callback.from_user.id, None)
    db.add_whitelist(group_id, target_user_id, callback.from_user.id)
    await callback.answer("Легалізацію надано")
    await _render_whitelist_picker(callback, group_id, callback.from_user.id, offset=0)


@router.callback_query(F.data.startswith("category:"))
async def cb_open_category(callback: types.CallbackQuery) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    try:
        group_id = int(parts[1])
    except ValueError:
        await callback.answer("Невірний group_id", show_alert=True)
        return
    category = parts[2]
    if category not in CATEGORY_ORDER:
        await callback.answer("Невірна категорія", show_alert=True)
        return
    await _render_category(callback, group_id, category, idx=0)


@router.callback_query(F.data.startswith("nav:"))
async def cb_nav_category(callback: types.CallbackQuery) -> None:
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
    await _render_category(callback, group_id, category, idx=idx)


@router.callback_query(F.data.startswith("allask:"))
async def cb_confirm_all_ask(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    try:
        group_id = int(parts[1])
    except ValueError:
        await callback.answer("Невірний group_id", show_alert=True)
        return
    category = parts[2]
    if category not in {"blocked", "pending", "confirmed"}:
        await callback.answer("Недоступно для цієї категорії", show_alert=True)
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return

    text = f"Підтвердити всі реклами в категорії '{CATEGORY_LABELS[category]}'?\nЦе зніме індикатор очікування."
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text="Так", callback_data=f"allconfirm:{group_id}:{category}"),
                types.InlineKeyboardButton(text="Ні", callback_data=f"category:{group_id}:{category}"),
            ]
        ]
    )
    try:
        await callback.message.edit_text(text=text, reply_markup=keyboard)
    except TelegramAPIError:
        await callback.message.answer(text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("allconfirm:"))
async def cb_confirm_all(callback: types.CallbackQuery, bot: Bot) -> None:
    if not callback.from_user:
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    try:
        group_id = int(parts[1])
    except ValueError:
        await callback.answer("Невірний group_id", show_alert=True)
        return
    category = parts[2]
    if category not in {"blocked", "pending", "confirmed"}:
        await callback.answer("Недоступно для цієї категорії", show_alert=True)
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return

    if category == "blocked":
        updated = db.confirm_all(
            group_id,
            category,
            callback.from_user.id,
            decision="acknowledged",
            action="acknowledged_all",
        )
    else:
        updated = db.confirm_all(group_id, category, callback.from_user.id)
    await _refresh_alerts_for_group(bot, group_id, category)
    await callback.answer(f"Підтверджено: {updated}")
    await _render_category(callback, group_id, category, idx=0)


@router.callback_query(F.data.startswith("ackalert:"))
async def cb_ack_alert(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    try:
        group_id = int(parts[1])
    except ValueError:
        await callback.answer("Невірний group_id", show_alert=True)
        return
    category = parts[2]
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return

    db.clear_alert_state(callback.from_user.id, group_id, category)
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass
    await callback.answer("Прибрано")


@router.callback_query(F.data.startswith("action:"))
async def cb_action(callback: types.CallbackQuery, bot: Bot) -> None:
    if not callback.from_user:
        return
    parts = callback.data.split(":")
    if len(parts) != 6:
        await callback.answer()
        return
    try:
        group_id = int(parts[1])
        ad_id = int(parts[3])
        idx = int(parts[5])
    except ValueError:
        await callback.answer("Некоректні параметри", show_alert=True)
        return

    category = parts[2]
    action = parts[4]
    if category not in CATEGORY_ORDER:
        await callback.answer("Невірна категорія", show_alert=True)
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return

    ad = db.get_ad(ad_id)
    if ad is None:
        await callback.answer("Запис не знайдено", show_alert=True)
        return
    if int(ad["group_id"]) != group_id:
        await callback.answer("Невірна група", show_alert=True)
        return

    if action == "approve":
        db.update_ad_decision(ad_id=ad_id, decision="approved", moderator_id=callback.from_user.id, requires_action=False)
        await callback.answer("Підтверджено")
    elif action == "ack":
        db.update_ad_decision(ad_id=ad_id, decision="acknowledged", moderator_id=callback.from_user.id, requires_action=False)
        await callback.answer("Позначено")
    elif action == "block":
        if not config.TEST_MODE:
            await delete_message_safe(bot, int(ad["source_chat_id"]), int(ad["source_message_id"]))
            await apply_permanent_mute(bot, int(ad["source_chat_id"]), int(ad["user_id"]))
        state.add_strike(group_id, int(ad["user_id"]))
        db.update_ad_decision(
            ad_id=ad_id,
            decision="muted_manual",
            moderator_id=callback.from_user.id,
            requires_action=False,
            category="blocked",
            note="manual block",
        )
        await callback.answer("Заблоковано")
    elif action == "unmute":
        if not config.TEST_MODE:
            await apply_unmute(bot, int(ad["source_chat_id"]), int(ad["user_id"]))
        db.update_ad_decision(
            ad_id=ad_id,
            decision="unmuted",
            moderator_id=callback.from_user.id,
            requires_action=False,
            category="blocked",
            note="manual unmute",
        )
        await callback.answer("Розблоковано")
    else:
        await callback.answer("Невідома дія", show_alert=True)
        return

    await _refresh_alerts_for_group(bot, group_id, "blocked")
    await _refresh_alerts_for_group(bot, group_id, "suspect")
    await _refresh_alerts_for_group(bot, group_id, "pending")
    await _refresh_alerts_for_group(bot, group_id, "confirmed")

    target_category = "blocked" if action == "block" else category
    await _render_category(callback, group_id, target_category, idx=idx)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: types.CallbackQuery) -> None:
    await callback.answer()


@router.message(F.chat.type == "private")
async def private_input_flow_handler(message: types.Message) -> None:
    if not message.from_user:
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    flow = _PRIVATE_INPUT_FLOW.get(message.from_user.id)
    if flow is None:
        return

    action, group_id = flow
    if not db.is_moderator(group_id, message.from_user.id):
        _PRIVATE_INPUT_FLOW.pop(message.from_user.id, None)
        await _send_context_message(message, "Немає доступу до цієї групи.")
        return

    if action == "whitelist_search":
        key = _whitelist_state_key(message.from_user.id, group_id)
        _WHITELIST_SEARCH[key] = text
        _WHITELIST_OFFSET[key] = 0

        rows, total = db.list_group_users_for_whitelist(group_id, text, limit=8, offset=0)
        query_line = f"Пошук: {text}" if text else "Пошук: (не задано)"
        keyboard = _whitelist_picker_kb(group_id, rows, 0, total)
        await _send_context_message(
            message,
            "Оберіть користувача для постійної легалізації.\n"
            f"{query_line}\n"
            f"Знайдено: {total}\n"
            "Надішліть новий запит, щоб одразу оновити результати.",
            reply_markup=keyboard,
        )
        return

    try:
        target_user_id = int(text)
    except ValueError:
        await _send_context_message(message, "Невірний user_id. Надішліть тільки число.")
        return

    if action == "add_moderator":
        db.add_moderator(group_id, target_user_id, message.from_user.id)
        _PRIVATE_INPUT_FLOW.pop(message.from_user.id, None)
        await _send_context_message(
            message,
            f"✅ Модератора {target_user_id} додано до групи {group_id}.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text="До налаштувань групи", callback_data=f"menu:settings:{group_id}")]]
            ),
        )
        return

    if action == "add_whitelist":
        db.add_whitelist(group_id, target_user_id, message.from_user.id)
        _PRIVATE_INPUT_FLOW.pop(message.from_user.id, None)
        await _send_context_message(
            message,
            f"✅ Користувачу {target_user_id} надано постійну легалізацію у групі {group_id}.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text="До налаштувань групи", callback_data=f"menu:settings:{group_id}")]]
            ),
        )


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
