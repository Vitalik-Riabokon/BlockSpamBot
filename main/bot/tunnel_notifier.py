"""Tunnel URL discovery, persistence and moderator notification helpers."""

import asyncio
import json
import logging
import re
import shutil
from asyncio.subprocess import PIPE, STDOUT
from pathlib import Path

from aiogram import Bot, types
from aiogram.exceptions import TelegramAPIError

from . import db
from .config import (
    CLOUDFLARED_AUTO_START,
    CLOUDFLARED_BIN,
    CLOUDFLARED_TARGET_URL,
    TUNNEL_NOTIFY_ENABLED,
    TUNNEL_LOG_PATH,
    TUNNEL_LOG_POLL_SECONDS,
    TUNNEL_NOTIFY_USER_ID,
)
from .logging_utils import emit_structured_log

STATE_PATH = Path("main/data/tunnel_notifier_state.json")
LOG_PATH = Path(TUNNEL_LOG_PATH)
TUNNEL_URL_REGEX = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com", re.IGNORECASE)


def _load_last_url() -> str:
    """Read the last discovered public tunnel URL from state file."""
    if not STATE_PATH.exists():
        return ""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return str(data.get("last_url", "")).strip()
    except Exception:
        return ""


def _save_last_url(url: str) -> None:
    """Persist the last discovered public tunnel URL to disk."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_url": url.strip()}
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def get_public_webapp_base_url() -> str:
    """Return the currently known public WebApp base URL, if any."""
    dynamic_url = _load_last_url()
    if dynamic_url:
        return dynamic_url.rstrip("/")
    return ""


def _extract_domain(url: str) -> str:
    """Strip protocol and trailing slash from a public URL for BotFather usage."""
    return re.sub(r"^https?://", "", url).strip().rstrip("/")


async def _update_menu_button(bot: Bot, url: str) -> None:
    """Update the bot menu button to point at the current WebApp URL."""
    try:
        await bot.set_chat_menu_button(
            menu_button=types.MenuButtonWebApp(
                text="Застосунок",
                web_app=types.WebAppInfo(url=f"{url.rstrip('/')}/webapp"),
            )
        )
    except TelegramAPIError:
        logging.exception("Failed to update chat menu button")


async def _send_tunnel_update(bot: Bot, url: str) -> None:
    """Send the current tunnel URL to the configured owner in private chat."""
    if not TUNNEL_NOTIFY_ENABLED or TUNNEL_NOTIFY_USER_ID <= 0:
        return
    domain = _extract_domain(url)
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="Скопіювати для BotFather",
                    copy_text=types.CopyTextButton(text=domain),
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="Скопіювати повний URL",
                    copy_text=types.CopyTextButton(text=url),
                )
            ],
            [types.InlineKeyboardButton(text="Оновити WebApp у боті", callback_data="tunnel:refresh_webapp")],
        ]
    )
    sent = await bot.send_message(
        chat_id=TUNNEL_NOTIFY_USER_ID,
        text=(
            "Новий тимчасовий WebApp URL:\n"
            f"<code>{url}</code>\n\n"
            "Для BotFather вставляй:\n"
            f"<code>{domain}</code>\n\n"
            "Онови в BotFather через /setdomain (без https://)."
        ),
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_notification=False,
    )
    db.track_private_bot_message(TUNNEL_NOTIFY_USER_ID, TUNNEL_NOTIFY_USER_ID, sent.message_id, "tunnel_update")
    emit_structured_log(
        "tunnel_url_notified",
        logger_name=__name__,
        target_user_id=TUNNEL_NOTIFY_USER_ID,
        url=url,
        domain=domain,
    )


async def _handle_discovered_url(bot: Bot, current_url: str, last_url: str) -> str:
    """Persist and announce a newly discovered tunnel URL when it changes."""
    if current_url == last_url:
        return last_url
    _save_last_url(current_url)
    await _update_menu_button(bot, current_url)
    try:
        await _send_tunnel_update(bot, current_url)
    except TelegramAPIError:
        logging.exception("Failed to send tunnel URL notification")
    return current_url


async def _watch_cloudflared_log(bot: Bot) -> None:
    """Watch an external cloudflared log file and react to discovered tunnel URLs."""
    emit_structured_log("cloudflared_log_watch_started", logger_name=__name__, log_path=str(LOG_PATH))
    last_url = _load_last_url()
    last_position = 0

    while True:
        try:
            if not LOG_PATH.exists():
                await asyncio.sleep(TUNNEL_LOG_POLL_SECONDS)
                continue

            current_size = LOG_PATH.stat().st_size
            if current_size < last_position:
                last_position = 0

            with LOG_PATH.open("r", encoding="utf-8", errors="ignore") as fh:
                fh.seek(last_position)
                for line in fh:
                    match = TUNNEL_URL_REGEX.search(line)
                    if match:
                        last_url = await _handle_discovered_url(bot, match.group(0).strip(), last_url)
                last_position = fh.tell()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("cloudflared log watcher failed")
        await asyncio.sleep(TUNNEL_LOG_POLL_SECONDS)


async def run_cloudflared_tunnel(bot: Bot, restart_delay_seconds: int = 3) -> None:
    """Run or monitor cloudflared and keep WebApp URL state in sync."""
    if not CLOUDFLARED_AUTO_START:
        await _watch_cloudflared_log(bot)
        return

    binary = shutil.which(CLOUDFLARED_BIN)
    if not binary:
        logging.warning("cloudflared binary not found: %s", CLOUDFLARED_BIN)
        return

    last_url = _load_last_url()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    while True:
        process = None
        try:
            emit_structured_log(
                "cloudflared_starting",
                logger_name=__name__,
                binary=binary,
                target_url=CLOUDFLARED_TARGET_URL,
            )
            LOG_PATH.write_text("", encoding="utf-8")
            process = await asyncio.create_subprocess_exec(
                binary,
                "tunnel",
                "--url",
                CLOUDFLARED_TARGET_URL,
                "--no-autoupdate",
                stdout=PIPE,
                stderr=STDOUT,
            )

            assert process.stdout is not None
            while True:
                raw_line = await process.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="ignore").rstrip()
                if line:
                    with LOG_PATH.open("a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                    emit_structured_log("cloudflared_output", logger_name=__name__, line=line)
                    match = TUNNEL_URL_REGEX.search(line)
                    if match:
                        last_url = await _handle_discovered_url(bot, match.group(0).strip(), last_url)

            exit_code = await process.wait()
            emit_structured_log(
                "cloudflared_exited",
                level=logging.WARNING,
                logger_name=__name__,
                exit_code=exit_code,
            )
        except asyncio.CancelledError:
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            raise
        except Exception:
            logging.exception("cloudflared supervisor failed")
        await asyncio.sleep(restart_delay_seconds)
