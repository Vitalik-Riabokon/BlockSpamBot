import logging
import re

from aiogram import Bot, F, Router, types
from aiogram.filters import Command
from aiogram.filters.command import CommandStart

from . import config, db, state
from .actions import apply_block_action, log_action, notify_review_needed
from .classifier import classify_message
from .models import GroupPolicy, ModerationResult, ModerationStatus

router = Router()


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


@router.message(CommandStart())
async def start_handler(message: types.Message) -> None:
    await message.reply(
        "\n".join(
            [
                "Бот активний.",
                "1) Додайте бота адміном у вашу групу.",
                "2) Група прив'язується автоматично при додаванні бота.",
                "   (або вручну: /register_group у групі)",
                "3) Кожен модератор має натиснути /start в приваті.",
                "Команди: /mod_help, /my_id, /chat_id, /my_groups",
            ]
        )
    )


@router.message(Command("help"))
async def help_handler(message: types.Message) -> None:
    await message.reply("Доступні команди: /start, /my_id, /chat_id, /my_groups, /mod_help")


@router.message(Command("my_id"))
async def my_id(message: types.Message) -> None:
    if not message.from_user:
        return
    await message.reply(f"Ваш user_id: {message.from_user.id}")


@router.message(Command("chat_id"))
async def chat_id(message: types.Message) -> None:
    await message.reply(f"Поточний chat_id: {message.chat.id}")


@router.message(Command("mod_help"))
async def mod_help(message: types.Message) -> None:
    await message.reply(
        "\n".join(
            [
                "Групи:",
                "/register_group - зареєструвати поточну групу",
                "/delete_group [group_id] - видалити групу (лише той, хто її реєстрував)",
                "/my_groups - список ваших груп",
                "",
                "Модератори:",
                "/add_moderator <user_id> [group_id|all]",
                "/remove_moderator <user_id> [group_id|all]",
                "/list_moderators [group_id]",
                "",
                "Whitelist (довірені рекламодавці):",
                "/add_whitelist <user_id> [group_id|all]",
                "/remove_whitelist <user_id> [group_id|all]",
                "/list_whitelist [group_id]",
                "",
                "Hard-block слова:",
                "/add_hardword <слово/фраза> [group_id|all]",
                "  Якщо фраза з пробілами і кілька груп: /add_hardword фраза | all",
                "/remove_hardword <слово/фраза> [group_id|all]",
                "/list_hardwords [group_id]",
                "",
                "Інше:",
                "/set_review_alerts <on|off> [group_id|all]",
                "/pause_group [group_id|all] - призупинити модерацію",
                "/resume_group [group_id|all] - відновити модерацію",
                "",
                "Як додати модератора:",
                "1) Новий модер пише боту /start в приваті",
                "2) Новий модер надсилає вам свій id через /my_id",
                "3) Ви виконуєте /add_moderator <id> [group_id|all]",
            ]
        )
    )


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
async def bot_membership_changed(event: types.ChatMemberUpdated, bot: Bot) -> None:
    if event.chat.type not in ("group", "supergroup"):
        return

    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status

    if old_status in {"left", "kicked"} and new_status in {"member", "administrator"}:
        actor = event.from_user
        if actor:
            if not db.is_group_registered(event.chat.id):
                # Автоматична прив'язка групи до користувача, який додав бота.
                db.register_group(event.chat.id, event.chat.title or str(event.chat.id), actor.id)
                logging.info("Auto-registered group %s by user %s", event.chat.id, actor.id)
            elif db.is_group_registered(event.chat.id):
                # Existing group: if actor already moderator, refresh title.
                if db.is_moderator(event.chat.id, actor.id):
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
            f"- {row['title']} | id={row['group_id']} | review_alerts={'on' if row['review_alerts'] else 'off'} | paused={'yes' if row['is_paused'] else 'no'}{owner_mark}"
        )
    await message.reply("\n".join(lines))


@router.message(Command("set_review_alerts"))
async def set_review_alerts(message: types.Message) -> None:
    if not message.from_user:
        return

    raw = _command_arg(message)
    parts = raw.split(maxsplit=1)
    if not parts:
        await message.reply("Використання: /set_review_alerts <on|off> [group_id|all]")
        return

    parsed = _parse_bool(parts[0])
    if parsed is None:
        await message.reply("Використання: /set_review_alerts <on|off> [group_id|all]")
        return

    group_spec = parts[1].strip() if len(parts) > 1 else ""
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await message.reply(err)
        return

    for gid in target_groups:
        db.set_review_alerts(gid, parsed)

    await message.reply(f"✅ review alerts: {'on' if parsed else 'off'} для group_id: {target_groups}")


@router.message(Command("pause_group"))
async def pause_group(message: types.Message) -> None:
    if not message.from_user:
        return

    group_spec = _command_arg(message)
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
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

    group_spec = _command_arg(message)
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await message.reply(err)
        return

    for gid in target_groups:
        db.set_group_paused(gid, False)
    await message.reply(f"✅ Модерація відновлена для group_id: {target_groups}")


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

    await message.reply(f"✅ Додано модератора {target_user_id} у group_id: {target_groups}")


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

    group_spec = _command_arg(message)
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await message.reply(err)
        return

    lines = []
    for gid in target_groups:
        lines.append(f"group {gid}: {db.list_moderators(gid)}")
    await message.reply("\n".join(lines))


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

    group_spec = _command_arg(message)
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await message.reply(err)
        return

    lines = []
    for gid in target_groups:
        lines.append(f"group {gid}: {db.list_whitelist(gid)}")
    await message.reply("\n".join(lines))


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

    group_spec = _command_arg(message)
    target_groups, err = _resolve_target_groups(message, message.from_user.id, group_spec)
    if err:
        await message.reply(err)
        return

    lines = []
    for gid in target_groups:
        lines.append(f"group {gid}: {db.list_hardwords(gid)}")
    await message.reply("\n".join(lines))


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def moderate_message(message: types.Message, bot: Bot) -> None:
    try:
        group_id = message.chat.id
        if not db.is_group_registered(group_id):
            return
        if db.is_group_paused(group_id):
            return

        author = message.from_user
        if not author:
            return

        policy_data = db.get_policy(group_id)
        policy = GroupPolicy(
            group_id=group_id,
            whitelist_user_ids=policy_data["whitelist"],
            authorized_user_ids=policy_data["authorized"],
            hard_block_extra_keywords=policy_data["hardwords"],
        )

        result = classify_message(message, policy)

        in_whitelist = author.id in policy.whitelist_user_ids
        hard_block_hit = "hard_illegal" in result.reasons

        if result.status == ModerationStatus.SAFE_TEXT:
            return

        if in_whitelist:
            # Whitelist user: do not send review/pending alerts, unless hard-block trigger hit.
            if result.status == ModerationStatus.AD_BLOCKED and hard_block_hit:
                await apply_block_action(
                    bot,
                    group_id,
                    message.chat,
                    author,
                    message,
                    result,
                    extra_note="Користувач у whitelist. Перевірте, чи потрібно прибрати його з whitelist.",
                )
                return
            await log_action(message.chat, author, message, result)
            return

        if result.status == ModerationStatus.AD_BLOCKED:
            await apply_block_action(bot, group_id, message.chat, author, message, result)
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
                await apply_block_action(bot, group_id, message.chat, author, message, escalated)
                return
            await notify_review_needed(bot, group_id, message.chat, author, message, result)
            await log_action(message.chat, author, message, result)
            return

        if result.status == ModerationStatus.AD_PENDING_AUTH:
            await notify_review_needed(bot, group_id, message.chat, author, message, result)
            await log_action(message.chat, author, message, result)
            return

        if result.status == ModerationStatus.AD_ALLOWED:
            await log_action(message.chat, author, message, result)
    except Exception:
        logging.exception("Помилка в moderate_message")


@router.edited_message(F.chat.type.in_({"group", "supergroup"}))
async def moderate_edited_message(message: types.Message, bot: Bot) -> None:
    await moderate_message(message, bot)


@router.message(F.new_chat_members)
async def new_member_monitor(message: types.Message) -> None:
    for user in message.new_chat_members:
        logging.info("New member %s joined chat %s", user.id, message.chat.id)
