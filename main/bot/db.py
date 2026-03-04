import sqlite3
import time
from pathlib import Path

DB_PATH = Path("main/data/moderation.db")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                group_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                review_alerts INTEGER NOT NULL DEFAULT 1,
                is_paused INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS group_moderators (
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                added_by INTEGER NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id),
                FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS group_whitelist (
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                added_by INTEGER NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id),
                FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS group_authorized (
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                added_by INTEGER NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id),
                FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS group_hardwords (
                group_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                added_by INTEGER NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, word),
                FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
            );
            """
        )
        # Backward-compatible migration for old DBs that don't have is_paused column.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(groups)").fetchall()}
        if "is_paused" not in cols:
            conn.execute("ALTER TABLE groups ADD COLUMN is_paused INTEGER NOT NULL DEFAULT 0")


def _now() -> int:
    return int(time.time())


def register_group(group_id: int, title: str, created_by: int) -> bool:
    """Returns True if group was created now, False if already existed."""
    created = False
    with _connect() as conn:
        row = conn.execute("SELECT group_id FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO groups(group_id, title, created_by, created_at, review_alerts) VALUES(?, ?, ?, ?, 1)",
                (group_id, title or str(group_id), created_by, _now()),
            )
            created = True
        else:
            conn.execute("UPDATE groups SET title = ? WHERE group_id = ?", (title or str(group_id), group_id))

        conn.execute(
            "INSERT OR IGNORE INTO group_moderators(group_id, user_id, added_by, added_at) VALUES(?, ?, ?, ?)",
            (group_id, created_by, created_by, _now()),
        )
    return created


def is_group_registered(group_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    return row is not None


def get_group(group_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM groups WHERE group_id = ?", (group_id,)).fetchone()


def delete_group(group_id: int, requester_id: int) -> tuple[bool, str]:
    with _connect() as conn:
        row = conn.execute("SELECT created_by FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if row is None:
            return False, "Група не зареєстрована."
        if int(row["created_by"]) != requester_id:
            return False, "Тільки користувач, який зареєстрував групу, може її видалити."
        conn.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
    return True, "Групу видалено."


def delete_group_force(group_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))


def list_user_groups(user_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """
            SELECT g.group_id, g.title, g.created_by, g.review_alerts, g.is_paused
            FROM groups g
            JOIN group_moderators gm ON gm.group_id = g.group_id
            WHERE gm.user_id = ?
            ORDER BY g.group_id
            """,
            (user_id,),
        ).fetchall()


def is_moderator(group_id: int, user_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM group_moderators WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()
    return row is not None


def list_moderators(group_id: int) -> list[int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM group_moderators WHERE group_id = ? ORDER BY user_id",
            (group_id,),
        ).fetchall()
    return [int(row["user_id"]) for row in rows]


def add_moderator(group_id: int, target_user_id: int, added_by: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO group_moderators(group_id, user_id, added_by, added_at) VALUES(?, ?, ?, ?)",
            (group_id, target_user_id, added_by, _now()),
        )


def remove_moderator(group_id: int, target_user_id: int, requester_id: int) -> tuple[bool, str]:
    with _connect() as conn:
        row = conn.execute("SELECT created_by FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if row is None:
            return False, "Група не зареєстрована."

        created_by = int(row["created_by"])
        if target_user_id == created_by:
            return False, "Неможливо видалити creator групи з модераторів."

        if target_user_id == requester_id:
            return False, "Неможливо видалити себе з модераторів цієї групи."

        conn.execute(
            "DELETE FROM group_moderators WHERE group_id = ? AND user_id = ?",
            (group_id, target_user_id),
        )
    return True, "Модератора видалено."


def set_review_alerts(group_id: int, enabled: bool) -> None:
    with _connect() as conn:
        conn.execute("UPDATE groups SET review_alerts = ? WHERE group_id = ?", (1 if enabled else 0, group_id))


def set_group_paused(group_id: int, paused: bool) -> None:
    with _connect() as conn:
        conn.execute("UPDATE groups SET is_paused = ? WHERE group_id = ?", (1 if paused else 0, group_id))


def is_group_paused(group_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT is_paused FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    if row is None:
        return False
    return bool(row["is_paused"])


def get_review_alerts(group_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT review_alerts FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    if row is None:
        return True
    return bool(row["review_alerts"])


def list_whitelist(group_id: int) -> list[int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM group_whitelist WHERE group_id = ? ORDER BY user_id",
            (group_id,),
        ).fetchall()
    return [int(row["user_id"]) for row in rows]


def add_whitelist(group_id: int, user_id: int, added_by: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO group_whitelist(group_id, user_id, added_by, added_at) VALUES(?, ?, ?, ?)",
            (group_id, user_id, added_by, _now()),
        )


def remove_whitelist(group_id: int, user_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM group_whitelist WHERE group_id = ? AND user_id = ?", (group_id, user_id))


def is_whitelisted(group_id: int, user_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM group_whitelist WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()
    return row is not None


def list_authorized(group_id: int) -> list[int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM group_authorized WHERE group_id = ? ORDER BY user_id",
            (group_id,),
        ).fetchall()
    return [int(row["user_id"]) for row in rows]


def add_authorized(group_id: int, user_id: int, added_by: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO group_authorized(group_id, user_id, added_by, added_at) VALUES(?, ?, ?, ?)",
            (group_id, user_id, added_by, _now()),
        )


def remove_authorized(group_id: int, user_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM group_authorized WHERE group_id = ? AND user_id = ?", (group_id, user_id))


def is_authorized(group_id: int, user_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM group_authorized WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()
    return row is not None


def list_hardwords(group_id: int) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT word FROM group_hardwords WHERE group_id = ? ORDER BY word",
            (group_id,),
        ).fetchall()
    return [str(row["word"]) for row in rows]


def add_hardword(group_id: int, word: str, added_by: int) -> None:
    normalized = word.strip().lower()
    if not normalized:
        return
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO group_hardwords(group_id, word, added_by, added_at) VALUES(?, ?, ?, ?)",
            (group_id, normalized, added_by, _now()),
        )


def remove_hardword(group_id: int, word: str) -> None:
    normalized = word.strip().lower()
    with _connect() as conn:
        conn.execute(
            "DELETE FROM group_hardwords WHERE group_id = ? AND word = ?",
            (group_id, normalized),
        )


def get_policy(group_id: int) -> dict:
    whitelist = set(list_whitelist(group_id))
    # Backward compatibility with older configs where trusted advertisers were in group_authorized.
    trusted = whitelist | set(list_authorized(group_id))
    return {
        "review_alerts": get_review_alerts(group_id),
        "is_paused": is_group_paused(group_id),
        "whitelist": whitelist,
        "authorized": trusted,
        "hardwords": set(list_hardwords(group_id)),
        "moderators": set(list_moderators(group_id)),
    }
