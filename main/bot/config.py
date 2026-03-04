import os
from pathlib import Path


def load_local_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_local_env()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in environment or .env file.")

TEST_MODE = os.getenv("TEST_MODE", "0").lower() in {"1", "true", "yes", "on"}

# Thresholds
SUSPECT_SCORE_THRESHOLD = int(os.getenv("SUSPECT_SCORE_THRESHOLD", "45"))
BLOCK_SCORE_THRESHOLD = int(os.getenv("BLOCK_SCORE_THRESHOLD", "75"))
SUSPECT_ESCALATION_COUNT = int(os.getenv("SUSPECT_ESCALATION_COUNT", "3"))
SUSPECT_WINDOW_SECONDS = int(os.getenv("SUSPECT_WINDOW_SECONDS", str(7 * 24 * 3600)))

# Spam-pattern controls
WINDOW_SECONDS = int(os.getenv("WINDOW_SECONDS", "120"))
FLOOD_COUNT = int(os.getenv("FLOOD_COUNT", "4"))
DUPLICATE_WINDOW_SECONDS = int(os.getenv("DUPLICATE_WINDOW_SECONDS", "600"))
