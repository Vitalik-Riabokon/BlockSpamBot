"""Telegram-side moderation actions such as delete, mute and unmute."""

import logging
from typing import Optional

from aiogram import Bot, types
from aiogram.exceptions import TelegramAPIError

from . import config, db
from .models import ModerationResult


async def log_action(chat: types.Chat, author: types.User, message: types.Message, result: ModerationResult) -> None:
    """Write a moderation decision to application logs."""
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


async def delete_message_safe(bot: Bot, chat_id: int, message_id: int) -> bool:
    """Delete a Telegram message and suppress API errors into a boolean result."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except TelegramAPIError:
        logging.exception("Failed to delete message chat_id=%s msg_id=%s", chat_id, message_id)
        return False


async def apply_permanent_mute(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Apply permanent chat restrictions while keeping the user in the group."""
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=types.ChatPermissions(can_send_messages=False),
        )
        db.set_user_status(user_id, "mute_permanent")
        return True
    except TelegramAPIError:
        logging.exception("Failed to restrict user %s in chat %s", user_id, chat_id)
        return False


async def apply_unmute(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Restore normal sending permissions for a previously muted user."""
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=types.ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=False,
            ),
        )
        db.set_user_status(user_id, "in_group")
        return True
    except TelegramAPIError:
        logging.exception("Failed to unmute user %s in chat %s", user_id, chat_id)
        return False


async def apply_block_action(
    bot: Bot,
    group_id: int,
    chat: types.Chat,
    author: types.User,
    message: types.Message,
    result: ModerationResult,
    ad_id: Optional[int] = None,
) -> None:
    """Execute the bot-side block flow for an automatically blocked message."""
    await log_action(chat, author, message, result)

    if not config.TEST_MODE:
        await delete_message_safe(bot, chat.id, message.message_id)
        await apply_permanent_mute(bot, chat.id, author.id)

    if ad_id:
        db.update_ad_decision(
            ad_id=ad_id,
            decision="muted_auto",
            moderator_id=None,
            requires_action=True,
            category="blocked",
            note="automatic block action",
        )
