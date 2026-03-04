import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher

from bot.config import BOT_TOKEN, TEST_MODE
from bot.db import init_db
from bot.handlers import router

LOCK_PATH = Path("main/data/bot.lock")


def acquire_lock() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
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
    dp = Dispatcher()
    dp.include_router(router)

    logging.info("Bot started. TEST_MODE = %s", TEST_MODE)
    await dp.start_polling(bot)


if __name__ == "__main__":
    lock_fd = acquire_lock()
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
