"""Application entrypoint for the Telegram bot and WebApp backend."""

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, types

from bot.config import BOT_LOCK_PATH, BOT_TOKEN, TEST_MODE, WEBAPP_HOST, WEBAPP_PORT
from bot.db import init_db
from bot.handlers import periodic_private_context_cleanup, router
from bot.logging_utils import configure_logging, emit_structured_log
from bot.tunnel_notifier import get_public_webapp_base_url, run_cloudflared_tunnel
from bot.webapp_server import start_webapp_server

LOCK_PATH = Path(BOT_LOCK_PATH)


def _pid_running(pid: int) -> bool:
    """Return whether a process with the given PID is currently alive."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock() -> int:
    """Create an exclusive process lock and recover stale lock files when possible."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Recover from stale lock file left by crashed process.
    if LOCK_PATH.exists():
        try:
            raw_pid = LOCK_PATH.read_text(encoding="utf-8").strip()
            stale_pid = int(raw_pid)
            if not _pid_running(stale_pid):
                LOCK_PATH.unlink(missing_ok=True)
        except Exception:
            LOCK_PATH.unlink(missing_ok=True)

    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError as exc:
        raise RuntimeError(
            f"Another instance is already running (lock file exists: {LOCK_PATH})."
        ) from exc

    os.write(fd, str(os.getpid()).encode("utf-8"))
    return fd


def release_lock(fd: int) -> None:
    """Release the process lock and remove the lock file."""
    try:
        os.close(fd)
    finally:
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except Exception:
            pass


async def main() -> None:
    """Start database, WebApp server, Telegram polling and background tasks."""
    configure_logging(logging.INFO)
    init_db()
    bot = Bot(token=BOT_TOKEN)
    web_runner, _web_site = await start_webapp_server(bot, WEBAPP_HOST, WEBAPP_PORT)
    emit_structured_log(
        "webapp_server_started",
        logger_name=__name__,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
        url=f"http://{WEBAPP_HOST}:{WEBAPP_PORT}/webapp",
    )
    base = get_public_webapp_base_url()
    if base:
        await bot.set_chat_menu_button(
            menu_button=types.MenuButtonWebApp(
                text="Застосунок",
                web_app=types.WebAppInfo(url=f"{base.rstrip('/')}/webapp"),
            )
        )
    await bot.set_my_commands(
        [
            types.BotCommand(command="start", description="Початок роботи з ботом"),
        ]
    )
    dp = Dispatcher()
    dp.include_router(router)
    cleanup_task = asyncio.create_task(periodic_private_context_cleanup(bot))
    cloudflared_task = asyncio.create_task(run_cloudflared_tunnel(bot))

    emit_structured_log("bot_started", logger_name=__name__, test_mode=TEST_MODE)
    try:
        await dp.start_polling(bot)
    finally:
        cleanup_task.cancel()
        cloudflared_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task
        with contextlib.suppress(asyncio.CancelledError):
            await cloudflared_task
        with contextlib.suppress(Exception):
            await web_runner.cleanup()


if __name__ == "__main__":
    lock_fd = acquire_lock()
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
