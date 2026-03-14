import asyncio
import contextlib
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, types

from bot.config import BOT_TOKEN, TEST_MODE, WEBAPP_HOST, WEBAPP_PORT
from bot.db import init_db
from bot.handlers import periodic_private_context_cleanup, router
from bot.webapp_server import start_webapp_server

LOCK_PATH = Path("main/data/bot.lock")


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock() -> int:
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
    try:
        os.close(fd)
    finally:
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except Exception:
            pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(token=BOT_TOKEN)
    web_runner, _web_site = await start_webapp_server(WEBAPP_HOST, WEBAPP_PORT)
    logging.info("WebApp server started on http://%s:%s/webapp", WEBAPP_HOST, WEBAPP_PORT)
    await bot.set_my_commands(
        [
            types.BotCommand(command="start", description="Початок роботи з ботом"),
            types.BotCommand(command="menu", description="Головне меню модератора"),
            types.BotCommand(command="my_groups", description="Мої підключені групи"),
            types.BotCommand(command="my_id", description="Показати мій user id"),
            types.BotCommand(command="mod_help", description="Довідка по командах"),
            types.BotCommand(command="toggle_pending", description="Увімк/вимк сповіщення від не санкціонованих реклам"),
            types.BotCommand(command="toggle_blocked_sound", description="Увімк/вимк звук від заблокованих реклам"),
            types.BotCommand(command="pause_group", description="Увімк/вимк модерацію обраної групи"),
        ]
    )
    dp = Dispatcher()
    dp.include_router(router)
    cleanup_task = asyncio.create_task(periodic_private_context_cleanup(bot))

    logging.info("Bot started. TEST_MODE = %s", TEST_MODE)
    try:
        await dp.start_polling(bot)
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task
        with contextlib.suppress(Exception):
            await web_runner.cleanup()


if __name__ == "__main__":
    lock_fd = acquire_lock()
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
