import logging

from aiogram import Bot, types
from aiogram.exceptions import TelegramAPIError

from . import config, db, state
from .models import ModerationResult, ModerationStatus


async def log_action(chat: types.Chat, author: types.User, message: types.Message, result: ModerationResult) -> None:
    reason = ", ".join(result.reasons)
    logging.info(
        "Moderation: status=%s score=%s user=%s chat=%s msg_id=%s reasons=%s",
        result.status.value,
        result.score,
        author.id,
        chat.id,
        message.message_id,
        reason,
    )


async def _notify_group_moderators(
    bot: Bot,
    group_id: int,
    chat: types.Chat,
    author: types.User,
    message: types.Message,
    result: ModerationResult,
    extra_note: str | None = None,
) -> None:
    moderators = db.list_moderators(group_id)
    if not moderators:
        return

    label = result.status.value
    reason = ", ".join(result.reasons)
    text = (
        f"⚠️ {label}\n"
        f"Score: {result.score}\n"
        f"Група: {chat.title or chat.id} ({chat.id})\n"
        f"Користувач: {author.full_name} (id={author.id})\n"
        f"Причини: {reason}"
    )
    if extra_note:
        text += f"\nПримітка: {extra_note}"

    for moderator_id in moderators:
        try:
            await bot.send_message(moderator_id, text)
            await bot.copy_message(
                chat_id=moderator_id,
                from_chat_id=chat.id,
                message_id=message.message_id,
            )
        except Exception:
            # Модератор міг не натиснути /start в боті або заблокував бота.
            logging.exception("Cannot notify moderator_id=%s for group_id=%s", moderator_id, group_id)


async def notify_review_needed(
    bot: Bot,
    group_id: int,
    chat: types.Chat,
    author: types.User,
    message: types.Message,
    result: ModerationResult,
) -> None:
    if result.status not in (ModerationStatus.AD_SUSPECT, ModerationStatus.AD_PENDING_AUTH):
        return

    if not db.get_review_alerts(group_id):
        return

    await _notify_group_moderators(bot, group_id, chat, author, message, result)


async def apply_block_action(
    bot: Bot,
    group_id: int,
    chat: types.Chat,
    author: types.User,
    message: types.Message,
    result: ModerationResult,
    extra_note: str | None = None,
) -> None:
    strikes = state.add_strike(group_id, author.id)
    await log_action(chat, author, message, result)

    if not config.TEST_MODE:
        try:
            await bot.delete_message(chat_id=chat.id, message_id=message.message_id)
        except TelegramAPIError:
            logging.exception("Failed to delete message")

        if strikes >= 4:
            try:
                await bot.ban_chat_member(chat_id=chat.id, user_id=author.id)
            except TelegramAPIError:
                logging.exception("Failed to ban user")

    # Hard-block алерти також відправляємо модераторам у приват.
    await _notify_group_moderators(bot, group_id, chat, author, message, result, extra_note=extra_note)
