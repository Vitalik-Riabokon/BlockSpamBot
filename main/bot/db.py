import sqlite3
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path("main/data/moderation.db")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> int:
    return int(time.time())


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
                is_paused INTEGER NOT NULL DEFAULT 0,
                notify_pending INTEGER NOT NULL DEFAULT 1,
                blocked_alert_sound INTEGER NOT NULL DEFAULT 0
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

            CREATE TABLE IF NOT EXISTS group_hardwords (
                group_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                added_by INTEGER NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, word),
                FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                username TEXT,
                at_username TEXT,
                phone TEXT,
                status TEXT NOT NULL DEFAULT 'in_group',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ads (
                ad_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                has_media INTEGER NOT NULL DEFAULT 0,
                category TEXT NOT NULL,
                decision TEXT NOT NULL DEFAULT 'pending',
                requires_action INTEGER NOT NULL DEFAULT 1,
                decided_by INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                resolved_at INTEGER,
                UNIQUE(group_id, source_message_id),
                FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ad_actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                moderator_id INTEGER,
                action TEXT NOT NULL,
                note TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (ad_id) REFERENCES ads(ad_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS moderator_alert_state (
                moderator_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (moderator_id, group_id, category)
            );
            """
        )

        # Backward-compatible migration for old DBs.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(groups)").fetchall()}
        if "is_paused" not in cols:
            conn.execute("ALTER TABLE groups ADD COLUMN is_paused INTEGER NOT NULL DEFAULT 0")
        if "notify_pending" not in cols:
            conn.execute("ALTER TABLE groups ADD COLUMN notify_pending INTEGER NOT NULL DEFAULT 1")
        if "blocked_alert_sound" not in cols:
            conn.execute("ALTER TABLE groups ADD COLUMN blocked_alert_sound INTEGER NOT NULL DEFAULT 0")


def register_group(group_id: int, title: str, created_by: int) -> bool:
    created = False
    now = _now()
    with _connect() as conn:
        row = conn.execute("SELECT group_id FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO groups(group_id, title, created_by, created_at, review_alerts, is_paused, notify_pending, blocked_alert_sound)
                VALUES(?, ?, ?, ?, 1, 0, 1, 0)
                """,
                (group_id, title or str(group_id), created_by, now),
            )
            created = True
        else:
            conn.execute("UPDATE groups SET title = ? WHERE group_id = ?", (title or str(group_id), group_id))

        conn.execute(
            "INSERT OR IGNORE INTO group_moderators(group_id, user_id, added_by, added_at) VALUES(?, ?, ?, ?)",
            (group_id, created_by, created_by, now),
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
            SELECT g.group_id, g.title, g.created_by, g.review_alerts, g.is_paused, g.notify_pending, g.blocked_alert_sound
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


def get_review_alerts(group_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT review_alerts FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    if row is None:
        return True
    return bool(row["review_alerts"])


def set_group_paused(group_id: int, paused: bool) -> None:
    with _connect() as conn:
        conn.execute("UPDATE groups SET is_paused = ? WHERE group_id = ?", (1 if paused else 0, group_id))


def is_group_paused(group_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT is_paused FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    if row is None:
        return False
    return bool(row["is_paused"])


def set_notify_pending(group_id: int, enabled: bool) -> None:
    with _connect() as conn:
        conn.execute("UPDATE groups SET notify_pending = ? WHERE group_id = ?", (1 if enabled else 0, group_id))


def get_notify_pending(group_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT notify_pending FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    if row is None:
        return True
    return bool(row["notify_pending"])


def set_blocked_alert_sound(group_id: int, enabled: bool) -> None:
    with _connect() as conn:
        conn.execute("UPDATE groups SET blocked_alert_sound = ? WHERE group_id = ?", (1 if enabled else 0, group_id))


def get_blocked_alert_sound(group_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT blocked_alert_sound FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    if row is None:
        return False
    return bool(row["blocked_alert_sound"])


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
    return {
        "review_alerts": get_review_alerts(group_id),
        "is_paused": is_group_paused(group_id),
        "notify_pending": get_notify_pending(group_id),
        "blocked_alert_sound": get_blocked_alert_sound(group_id),
        "whitelist": whitelist,
        "authorized": whitelist,
        "hardwords": set(list_hardwords(group_id)),
        "moderators": set(list_moderators(group_id)),
    }


def upsert_user(
    user_id: int,
    full_name: Optional[str],
    username: Optional[str],
    at_username: Optional[str] = None,
    phone: Optional[str] = None,
    status: Optional[str] = None,
) -> None:
    now = _now()
    with _connect() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO users(user_id, full_name, username, at_username, phone, status, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    full_name,
                    username,
                    at_username,
                    phone,
                    status or "in_group",
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE users
                SET full_name = COALESCE(?, full_name),
                    username = COALESCE(?, username),
                    at_username = COALESCE(?, at_username),
                    phone = COALESCE(?, phone),
                    status = COALESCE(?, status),
                    updated_at = ?
                WHERE user_id = ?
                """,
                (full_name, username, at_username, phone, status, now, user_id),
            )


def set_user_status(user_id: int, status: str) -> None:
    now = _now()
    with _connect() as conn:
        conn.execute("UPDATE users SET status = ?, updated_at = ? WHERE user_id = ?", (status, now, user_id))


def create_ad(
    group_id: int,
    user_id: int,
    source_chat_id: int,
    source_message_id: int,
    text: str,
    has_media: bool,
    category: str,
    decision: str = "pending",
    requires_action: bool = True,
) -> int:
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO ads(
                group_id, user_id, source_chat_id, source_message_id, text, has_media,
                category, decision, requires_action, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                user_id,
                source_chat_id,
                source_message_id,
                text,
                1 if has_media else 0,
                category,
                decision,
                1 if requires_action else 0,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT ad_id FROM ads WHERE group_id = ? AND source_message_id = ?",
            (group_id, source_message_id),
        ).fetchone()
    if row is None:
        return 0
    return int(row["ad_id"])


def get_ad(ad_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM ads WHERE ad_id = ?", (ad_id,)).fetchone()


def list_ads(group_id: int, category: str, unresolved_only: bool = True) -> list[sqlite3.Row]:
    with _connect() as conn:
        if unresolved_only:
            rows = conn.execute(
                """
                SELECT a.*, u.full_name, u.username, u.at_username, u.phone, u.status AS user_status
                FROM ads a
                LEFT JOIN users u ON u.user_id = a.user_id
                WHERE a.group_id = ? AND a.category = ? AND a.requires_action = 1
                ORDER BY a.created_at DESC, a.ad_id DESC
                """,
                (group_id, category),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT a.*, u.full_name, u.username, u.at_username, u.phone, u.status AS user_status
                FROM ads a
                LEFT JOIN users u ON u.user_id = a.user_id
                WHERE a.group_id = ? AND a.category = ?
                ORDER BY a.created_at DESC, a.ad_id DESC
                """,
                (group_id, category),
            ).fetchall()
    return rows


def get_unresolved_counts(group_id: int) -> dict[str, int]:
    base = {"blocked": 0, "suspect": 0, "pending": 0, "confirmed": 0}
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT category, COUNT(*) AS cnt
            FROM ads
            WHERE group_id = ? AND requires_action = 1
            GROUP BY category
            """,
            (group_id,),
        ).fetchall()
    for row in rows:
        category = str(row["category"])
        if category in base:
            base[category] = int(row["cnt"])
    return base


def record_ad_action(ad_id: int, group_id: int, moderator_id: Optional[int], action: str, note: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO ad_actions(ad_id, group_id, moderator_id, action, note, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (ad_id, group_id, moderator_id, action, note, _now()),
        )


def update_ad_decision(
    ad_id: int,
    decision: str,
    moderator_id: Optional[int],
    requires_action: bool,
    category: Optional[str] = None,
    note: Optional[str] = None,
) -> None:
    now = _now()
    with _connect() as conn:
        if category is None:
            conn.execute(
                """
                UPDATE ads
                SET decision = ?, decided_by = ?, requires_action = ?, updated_at = ?,
                    resolved_at = CASE WHEN ? = 1 THEN NULL ELSE ? END
                WHERE ad_id = ?
                """,
                (decision, moderator_id, 1 if requires_action else 0, now, 1 if requires_action else 0, now, ad_id),
            )
        else:
            conn.execute(
                """
                UPDATE ads
                SET category = ?, decision = ?, decided_by = ?, requires_action = ?, updated_at = ?,
                    resolved_at = CASE WHEN ? = 1 THEN NULL ELSE ? END
                WHERE ad_id = ?
                """,
                (
                    category,
                    decision,
                    moderator_id,
                    1 if requires_action else 0,
                    now,
                    1 if requires_action else 0,
                    now,
                    ad_id,
                ),
            )
    ad = get_ad(ad_id)
    gid = int(ad["group_id"]) if ad else 0
    record_ad_action(ad_id, gid, moderator_id, decision, note)


def confirm_all(group_id: int, category: str, moderator_id: int) -> int:
    now = _now()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ad_id FROM ads WHERE group_id = ? AND category = ? AND requires_action = 1",
            (group_id, category),
        ).fetchall()
        ids = [int(row["ad_id"]) for row in rows]
        if not ids:
            return 0

        conn.execute(
            """
            UPDATE ads
            SET decision = 'approved', decided_by = ?, requires_action = 0, updated_at = ?, resolved_at = ?
            WHERE group_id = ? AND category = ? AND requires_action = 1
            """,
            (moderator_id, now, now, group_id, category),
        )

    for ad_id in ids:
        record_ad_action(ad_id, group_id, moderator_id, "approved_all", "approve_all")
    return len(ids)


def get_alert_state(moderator_id: int, group_id: int, category: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            """
            SELECT moderator_id, group_id, category, message_id, chat_id, updated_at
            FROM moderator_alert_state
            WHERE moderator_id = ? AND group_id = ? AND category = ?
            """,
            (moderator_id, group_id, category),
        ).fetchone()


def set_alert_state(moderator_id: int, group_id: int, category: str, message_id: int, chat_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO moderator_alert_state(moderator_id, group_id, category, message_id, chat_id, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(moderator_id, group_id, category)
            DO UPDATE SET message_id=excluded.message_id, chat_id=excluded.chat_id, updated_at=excluded.updated_at
            """,
            (moderator_id, group_id, category, message_id, chat_id, _now()),
        )


def clear_alert_state(moderator_id: int, group_id: int, category: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM moderator_alert_state WHERE moderator_id = ? AND group_id = ? AND category = ?",
            (moderator_id, group_id, category),
        )


def latest_ads_history(limit: int = 200) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """
            SELECT a.*, u.full_name, u.username, u.at_username, u.phone, u.status AS user_status
            FROM ads a
            LEFT JOIN users u ON u.user_id = a.user_id
            ORDER BY a.created_at DESC, a.ad_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
