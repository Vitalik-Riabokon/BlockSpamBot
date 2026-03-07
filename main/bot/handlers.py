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
from .classifier import classify_message, get_text
from .models import GroupPolicy, ModerationResult, ModerationStatus

router = Router()

CATEGORY_LABELS = {
    "blocked": "Заблоковані",
    "suspect": "Підозрілі",
    "pending": "Адекватні, не підтверджені",
    "confirmed": "Від підтверджених",
}

CATEGORY_ORDER = ["blocked", "suspect", "pending", "confirmed"]


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


def _group_row_to_label(row) -> str:
    title = str(row["title"])
    gid = int(row["group_id"])
    return f"{title} ({gid})"


def _menu_groups_kb(rows: list) -> types.InlineKeyboardMarkup:
    keyboard: list[list[types.InlineKeyboardButton]] = []
    for row in rows:
        gid = int(row["group_id"])
        keyboard.append([types.InlineKeyboardButton(text=_group_row_to_label(row), callback_data=f"menu:group:{gid}")])
    keyboard.append([types.InlineKeyboardButton(text="Help", callback_data="menu:help_global")])
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _group_dashboard_kb(group_id: int, has_many_groups: bool) -> types.InlineKeyboardMarkup:
    keyboard = [
        [types.InlineKeyboardButton(text="Реклами", callback_data=f"menu:ads:{group_id}")],
        [types.InlineKeyboardButton(text="Налаштування", callback_data=f"menu:settings:{group_id}")],
        [types.InlineKeyboardButton(text="Статистика (скоро)", callback_data=f"menu:stats:{group_id}")],
        [types.InlineKeyboardButton(text="Help", callback_data=f"menu:help:{group_id}")],
    ]
    if has_many_groups:
        keyboard.append([types.InlineKeyboardButton(text="До вибору груп", callback_data="menu:groups")])
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
    keyboard = [
        [types.InlineKeyboardButton(text=f"Push для адекватних не підтверджених: {'ON' if notify_pending else 'OFF'}", callback_data=f"toggle:pending:{group_id}")],
        [types.InlineKeyboardButton(text=f"Звук для автоблоків: {'ON' if blocked_sound else 'OFF'}", callback_data=f"toggle:blocked_sound:{group_id}")],
        [types.InlineKeyboardButton(text=("Відновити модерацію" if paused else "Призупинити модерацію"), callback_data=(f"toggle:resume:{group_id}" if paused else f"toggle:pause:{group_id}"))],
        [types.InlineKeyboardButton(text="Назад", callback_data=f"menu:group:{group_id}")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _category_view_ads(group_id: int, category: str) -> list:
    if category == "blocked":
        return db.list_ads(group_id, category, unresolved_only=False)
    return db.list_ads(group_id, category, unresolved_only=True)


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
    first_url = _extract_first_url(text)
    keyboard: list[list[types.InlineKeyboardButton]] = []

    if category == "blocked":
        keyboard.append(
            [
                types.InlineKeyboardButton(text="Розблокувати", callback_data=f"action:{group_id}:{category}:{ad_id}:unmute:{idx}"),
                types.InlineKeyboardButton(text="OK", callback_data=f"action:{group_id}:{category}:{ad_id}:ack:{idx}"),
            ]
        )
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
    unresolved_ads = db.list_ads(group_id, category, unresolved_only=True)
    unresolved_count = len(unresolved_ads)
    if unresolved_count == 0:
        if category == "blocked":
            await _send_or_edit_private(
                bot=bot,
                moderator_id=moderator_id,
                group_id=group_id,
                category=category,
                text=f"<b>{html.escape(CATEGORY_LABELS[category])}</b>\nУсі нові автоблоки переглянуто.",
                keyboard=_alert_empty_kb(group_id, category),
                disable_notification=True,
            )
            return

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
        db.clear_alert_state(moderator_id, group_id, category)


async def _show_groups_menu(message: types.Message) -> None:
    if not message.from_user:
        return

    rows = db.list_user_groups(message.from_user.id)
    if not rows:
        await message.reply("Немає груп.\n1) Додайте бота в групу\n2) У групі виконайте /register_group")
        return
    await message.reply("Оберіть групу для керування:", reply_markup=_menu_groups_kb(rows))


@router.message(CommandStart())
async def start_handler(message: types.Message) -> None:
    await message.reply(
        "Бот активний. Натисніть /menu для керування групами та рекламами.\n"
        "Кожен модератор має запустити /start в приваті, інакше push-повідомлення не прийдуть."
    )
    await _show_groups_menu(message)


@router.message(Command("menu"))
async def menu_handler(message: types.Message) -> None:
    await _show_groups_menu(message)


@router.message(Command("help"))
@router.message(Command("mod_help"))
async def help_handler(message: types.Message) -> None:
    await message.reply(
        "\n".join(
            [
                "Основні:",
                "/start, /menu, /my_id, /chat_id, /my_groups",
                "",
                "Групи:",
                "/register_group - реєстрація групи (виконати в групі)",
                "/delete_group [group_id] - видалити групу і вивести бота",
                "/pause_group [group_id|all], /resume_group [group_id|all]",
                "",
                "Модератори:",
                "/add_moderator <user_id> [group_id|all]",
                "/remove_moderator <user_id> [group_id|all]",
                "/list_moderators [group_id]",
                "",
                "Whitelist:",
                "/add_whitelist <user_id> [group_id|all]",
                "/remove_whitelist <user_id> [group_id|all]",
                "/list_whitelist [group_id]",
                "",
                "Hardwords:",
                "/add_hardword <слово/фраза> [group_id|all]",
                "/remove_hardword <слово/фраза> [group_id|all]",
                "/list_hardwords [group_id]",
                "",
                "Сповіщення:",
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
        await message.reply(f"Ваш user_id: {message.from_user.id}")


@router.message(Command("chat_id"))
async def chat_id(message: types.Message) -> None:
    await message.reply(f"Поточний chat_id: {message.chat.id}")


@router.message(Command("register_group"))
async def register_group(message: types.Message, bot: Bot) -> None:
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Команда працює тільки в групі.")
        return
    if not message.from_user:
        return

    is_admin = await _is_group_admin(bot, message.chat.id, message.from_user.id)
    if not is_admin:
        await message.reply("Тільки адмін групи може зареєструвати групу в боті.")
        return

    already = db.is_group_registered(message.chat.id)
    if already and not db.is_moderator(message.chat.id, message.from_user.id):
        await message.reply("Ця група вже прив'язана іншими модераторами. Немає доступу.")
        return

    created = db.register_group(message.chat.id, message.chat.title or str(message.chat.id), message.from_user.id)
    if created:
        await message.reply("✅ Група зареєстрована. Ви додані як модератор цієї групи.")
    else:
        await message.reply("✅ Група вже зареєстрована. Ви підтверджені як модератор.")


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
            await message.reply("Використання: /delete_group [group_id]. У групі можна без аргументу.")
            return

    ok, text = db.delete_group(target_group_id, message.from_user.id)
    if not ok:
        await message.reply(f"⛔ {text}")
        return
    await message.reply(f"✅ {text}")
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
        await message.reply("Немає прив'язаних груп. Додайте бота в групу і виконайте /register_group.")
        return
    lines = ["Ваші групи:"]
    for row in rows:
        owner_mark = " (creator)" if int(row["created_by"]) == message.from_user.id else ""
        lines.append(
            "- "
            f"{row['title']} | id={row['group_id']} | "
            f"pending_push={'on' if row['notify_pending'] else 'off'} | "
            f"blocked_sound={'on' if row['blocked_alert_sound'] else 'off'} | "
            f"paused={'yes' if row['is_paused'] else 'no'}{owner_mark}"
        )
    await message.reply("\n".join(lines))


@router.message(Command("pause_group"))
async def pause_group(message: types.Message) -> None:
    if not message.from_user:
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, _command_arg(message))
    if err:
        await message.reply(err)
        return
    for gid in target_groups:
        db.set_group_paused(gid, True)
    await message.reply(f"✅ Модерація призупинена для group_id: {target_groups}")


@router.message(Command("resume_group"))
async def resume_group(message: types.Message) -> None:
    if not message.from_user:
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, _command_arg(message))
    if err:
        await message.reply(err)
        return
    for gid in target_groups:
        db.set_group_paused(gid, False)
    await message.reply(f"✅ Модерація відновлена для group_id: {target_groups}")


@router.message(Command("set_pending_alerts"))
async def set_pending_alerts(message: types.Message) -> None:
    if not message.from_user:
        return
    raw = _command_arg(message)
    parts = raw.split(maxsplit=1)
    if not parts:
        await message.reply("Використання: /set_pending_alerts <on|off> [group_id|all]")
        return
    parsed = _parse_bool(parts[0])
    if parsed is None:
        await message.reply("Використання: /set_pending_alerts <on|off> [group_id|all]")
        return
    group_spec = parts[1].strip() if len(parts) > 1 else ""
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await message.reply(err)
        return
    for gid in target_groups:
        db.set_notify_pending(gid, parsed)
    await message.reply(f"✅ Push для адекватних не підтверджених: {'on' if parsed else 'off'} для {target_groups}")


@router.message(Command("set_blocked_sound"))
async def set_blocked_sound(message: types.Message) -> None:
    if not message.from_user:
        return
    raw = _command_arg(message)
    parts = raw.split(maxsplit=1)
    if not parts:
        await message.reply("Використання: /set_blocked_sound <on|off> [group_id|all]")
        return
    parsed = _parse_bool(parts[0])
    if parsed is None:
        await message.reply("Використання: /set_blocked_sound <on|off> [group_id|all]")
        return
    group_spec = parts[1].strip() if len(parts) > 1 else ""
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await message.reply(err)
        return
    for gid in target_groups:
        db.set_blocked_alert_sound(gid, parsed)
    await message.reply(f"✅ Звук push для автоблоків: {'on' if parsed else 'off'} для {target_groups}")


@router.message(Command("set_review_alerts"))
async def set_review_alerts_alias(message: types.Message) -> None:
    await set_pending_alerts(message)


@router.message(Command("add_moderator"))
async def add_moderator(message: types.Message) -> None:
    if not message.from_user:
        return
    target_user_id, group_spec = _parse_target_user_and_groupspec(message)
    if target_user_id is None:
        await message.reply("Використання: /add_moderator <user_id> [group_id|all] або reply")
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await message.reply(err)
        return
    for gid in target_groups:
        db.add_moderator(gid, target_user_id, message.from_user.id)
    await message.reply(
        f"✅ Додано модератора {target_user_id} у group_id: {target_groups}\n"
        "Нагадування: модератор має запустити /start у приваті з ботом."
    )


@router.message(Command("remove_moderator"))
async def remove_moderator(message: types.Message) -> None:
    if not message.from_user:
        return
    target_user_id, group_spec = _parse_target_user_and_groupspec(message)
    if target_user_id is None:
        await message.reply("Використання: /remove_moderator <user_id> [group_id|all] або reply")
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await message.reply(err)
        return
    results = []
    for gid in target_groups:
        ok, text = db.remove_moderator(gid, target_user_id, message.from_user.id)
        results.append(f"group {gid}: {'ok' if ok else 'fail'} ({text})")
    await message.reply("\n".join(results))


@router.message(Command("list_moderators"))
async def list_moderators(message: types.Message) -> None:
    if not message.from_user:
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, _command_arg(message))
    if err:
        await message.reply(err)
        return
    await message.reply("\n".join([f"group {gid}: {db.list_moderators(gid)}" for gid in target_groups]))


@router.message(Command("add_whitelist"))
async def add_whitelist(message: types.Message) -> None:
    if not message.from_user:
        return
    target_user_id, group_spec = _parse_target_user_and_groupspec(message)
    if target_user_id is None:
        await message.reply("Використання: /add_whitelist <user_id> [group_id|all] або reply")
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await message.reply(err)
        return
    for gid in target_groups:
        db.add_whitelist(gid, target_user_id, message.from_user.id)
    await message.reply(f"✅ Додано у whitelist {target_user_id} для group_id: {target_groups}")


@router.message(Command("remove_whitelist"))
async def remove_whitelist(message: types.Message) -> None:
    if not message.from_user:
        return
    target_user_id, group_spec = _parse_target_user_and_groupspec(message)
    if target_user_id is None:
        await message.reply("Використання: /remove_whitelist <user_id> [group_id|all] або reply")
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await message.reply(err)
        return
    for gid in target_groups:
        db.remove_whitelist(gid, target_user_id)
    await message.reply(f"✅ Видалено з whitelist {target_user_id} для group_id: {target_groups}")


@router.message(Command("list_whitelist"))
async def list_whitelist(message: types.Message) -> None:
    if not message.from_user:
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, _command_arg(message))
    if err:
        await message.reply(err)
        return
    await message.reply("\n".join([f"group {gid}: {db.list_whitelist(gid)}" for gid in target_groups]))


@router.message(Command("add_hardword"))
async def add_hardword(message: types.Message) -> None:
    if not message.from_user:
        return
    raw = _command_arg(message)
    if not raw:
        await message.reply("Використання: /add_hardword <слово/фраза> [group_id|all]")
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
        await message.reply("Порожнє слово/фраза.")
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await message.reply(err)
        return
    for gid in target_groups:
        db.add_hardword(gid, word, message.from_user.id)
    await message.reply(f"✅ hardword додано: '{word}' для group_id: {target_groups}")


@router.message(Command("remove_hardword"))
async def remove_hardword(message: types.Message) -> None:
    if not message.from_user:
        return
    raw = _command_arg(message)
    if not raw:
        await message.reply("Використання: /remove_hardword <слово/фраза> [group_id|all]")
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
        await message.reply("Порожнє слово/фраза.")
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await message.reply(err)
        return
    for gid in target_groups:
        db.remove_hardword(gid, word)
    await message.reply(f"✅ hardword видалено: '{word}' для group_id: {target_groups}")


@router.message(Command("list_hardwords"))
async def list_hardwords(message: types.Message) -> None:
    if not message.from_user:
        return
    target_groups, err = _resolve_target_groups(message, message.from_user.id, _command_arg(message))
    if err:
        await message.reply(err)
        return
    await message.reply("\n".join([f"group {gid}: {db.list_hardwords(gid)}" for gid in target_groups]))


@router.callback_query(F.data == "menu:groups")
async def cb_menu_groups(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    rows = db.list_user_groups(callback.from_user.id)
    if not rows:
        await callback.answer("Немає груп", show_alert=True)
        return
    text = "Оберіть групу для керування:"
    keyboard = _menu_groups_kb(rows)
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
    rows = db.list_user_groups(callback.from_user.id)
    group = db.get_group(group_id)
    if group is None:
        await callback.answer("Групу не знайдено", show_alert=True)
        return
    text = "\n".join(
        [
            f"Група: {group['title']} ({group_id})",
            f"Модерація: {'PAUSED' if group['is_paused'] else 'ACTIVE'}",
            f"Push pending: {'ON' if group['notify_pending'] else 'OFF'}",
            f"Звук автоблоків: {'ON' if group['blocked_alert_sound'] else 'OFF'}",
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
            "Меню реклам:",
            f"Автоблоки (нові): {counts.get('blocked', 0)}",
            f"Підозрілі: {counts.get('suspect', 0)}",
            f"Адекватні не підтверджені: {counts.get('pending', 0)}",
            f"Від підтверджених: {counts.get('confirmed', 0)}",
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
    group = db.get_group(group_id)
    if group is None:
        await callback.answer("Групу не знайдено", show_alert=True)
        return
    text = (
        "Налаштування сповіщень та модерації:\n"
        "- Push для адекватних не підтверджених\n"
        "- Звук для автоблоків\n"
        "- Пауза/відновлення модерації"
    )
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
    text = (
        "Налаштування сповіщень та модерації:\n"
        "- Push для адекватних не підтверджених\n"
        "- Звук для автоблоків\n"
        "- Пауза/відновлення модерації"
    )
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


@router.callback_query(F.data.startswith("toggle:pending:"))
async def cb_toggle_pending(callback: types.CallbackQuery, bot: Bot) -> None:
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
    current = db.get_notify_pending(group_id)
    new_value = not current
    db.set_notify_pending(group_id, new_value)
    if new_value:
        await _refresh_alerts_for_group(bot, group_id, "pending")
    else:
        await _clear_alerts_for_group(bot, group_id, "pending")
    await callback.answer(f"Push pending: {'ON' if new_value else 'OFF'}")
    await _show_settings_after_toggle(callback, group_id)


@router.callback_query(F.data.startswith("toggle:blocked_sound:"))
async def cb_toggle_blocked_sound(callback: types.CallbackQuery) -> None:
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
    current = db.get_blocked_alert_sound(group_id)
    db.set_blocked_alert_sound(group_id, not current)
    await callback.answer(f"Звук автоблоків: {'ON' if not current else 'OFF'}")
    await _show_settings_after_toggle(callback, group_id)


@router.callback_query(F.data.startswith("toggle:pause:"))
async def cb_toggle_pause(callback: types.CallbackQuery) -> None:
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
    db.set_group_paused(group_id, True)
    await callback.answer("Модерацію призупинено")
    await _show_settings_after_toggle(callback, group_id)


@router.callback_query(F.data.startswith("toggle:resume:"))
async def cb_toggle_resume(callback: types.CallbackQuery) -> None:
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
    db.set_group_paused(group_id, False)
    await callback.answer("Модерацію відновлено")
    await _show_settings_after_toggle(callback, group_id)


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
    if category not in {"pending", "confirmed"}:
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
    if category not in {"pending", "confirmed"}:
        await callback.answer("Недоступно для цієї категорії", show_alert=True)
        return
    if not db.is_moderator(group_id, callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return

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

        policy_data = db.get_policy(group_id)
        policy = GroupPolicy(
            group_id=group_id,
            whitelist_user_ids=policy_data["whitelist"],
            authorized_user_ids=policy_data["authorized"],
            hard_block_extra_keywords=policy_data["hardwords"],
        )
        result = classify_message(message, policy)
        if result.status == ModerationStatus.SAFE_TEXT:
            return

        text = get_text(message)
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

        if result.status == ModerationStatus.AD_BLOCKED:
            if in_whitelist and not hard_block_hit:
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
            await apply_block_action(bot, group_id, message.chat, author, message, result, ad_id=ad_id)
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
    for user in message.new_chat_members:
        db.upsert_user(
            user_id=user.id,
            full_name=user.full_name,
            username=user.username,
            at_username=f"@{user.username}" if user.username else None,
            status="in_group",
        )


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
