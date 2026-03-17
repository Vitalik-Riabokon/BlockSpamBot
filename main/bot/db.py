"""SQLite persistence layer for groups, ads, alerts and private bot state."""

import sqlite3
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path("main/data/moderation.db")


def _connect() -> sqlite3.Connection:
    """Open a sqlite connection with foreign keys enabled."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> int:
    """Return current unix timestamp in seconds."""
    return int(time.time())


def init_db() -> None:
    """Create database schema and run lightweight backward-compatible migrations."""
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
                blocked_alert_sound INTEGER NOT NULL DEFAULT 0,
                swipe_requires_confirm INTEGER NOT NULL DEFAULT 1,
                hide_confirmed_blocked INTEGER NOT NULL DEFAULT 0
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

            CREATE TABLE IF NOT EXISTS group_triggers (
                trigger_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                trigger_type TEXT NOT NULL,
                value TEXT NOT NULL,
                added_by INTEGER NOT NULL,
                added_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(group_id, trigger_type, value),
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

            CREATE TABLE IF NOT EXISTS group_users (
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id),
                FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
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

            CREATE TABLE IF NOT EXISTS private_context_state (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                last_bot_message_id INTEGER,
                last_user_message_id INTEGER,
                selected_group_id INTEGER,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS private_bot_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
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
        if "swipe_requires_confirm" not in cols:
            conn.execute("ALTER TABLE groups ADD COLUMN swipe_requires_confirm INTEGER NOT NULL DEFAULT 1")
        if "hide_confirmed_blocked" not in cols:
            conn.execute("ALTER TABLE groups ADD COLUMN hide_confirmed_blocked INTEGER NOT NULL DEFAULT 0")

        trigger_rows = conn.execute(
            "SELECT group_id, word, added_by, added_at FROM group_hardwords"
        ).fetchall()
        for row in trigger_rows:
            value = str(row["word"] or "").strip().lower()
            if not value:
                continue
            trigger_type = "phrase" if " " in value else "word"
            now = _now()
            conn.execute(
                """
                INSERT OR IGNORE INTO group_triggers(group_id, trigger_type, value, added_by, added_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (int(row["group_id"]), trigger_type, value, int(row["added_by"]), int(row["added_at"]), now),
            )


def register_group(group_id: int, title: str, created_by: int) -> bool:
    """Register a group and ensure the creator becomes its first moderator."""
    created = False
    now = _now()
    with _connect() as conn:
        row = conn.execute("SELECT group_id FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO groups(group_id, title, created_by, created_at, review_alerts, is_paused, notify_pending, blocked_alert_sound, swipe_requires_confirm, hide_confirmed_blocked)
                VALUES(?, ?, ?, ?, 1, 0, 1, 0, 1, 0)
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
    """Return whether a moderation group already exists in the database."""
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    return row is not None


def get_group(group_id: int) -> sqlite3.Row | None:
    """Fetch a single group row by id."""
    with _connect() as conn:
        return conn.execute("SELECT * FROM groups WHERE group_id = ?", (group_id,)).fetchone()


def delete_group(group_id: int, requester_id: int) -> tuple[bool, str]:
    """Delete a group only when requested by its original creator."""
    with _connect() as conn:
        row = conn.execute("SELECT created_by FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if row is None:
            return False, "Група не зареєстрована."
        if int(row["created_by"]) != requester_id:
            return False, "Тільки користувач, який зареєстрував групу, може її видалити."
        conn.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
    return True, "Групу видалено."


def delete_group_force(group_id: int) -> None:
    """Delete a group without ownership checks."""
    with _connect() as conn:
        conn.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))


def list_user_groups(user_id: int) -> list[sqlite3.Row]:
    """Return all groups the user moderates."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT g.group_id, g.title, g.created_by, g.review_alerts, g.is_paused, g.notify_pending, g.blocked_alert_sound, g.swipe_requires_confirm, g.hide_confirmed_blocked
            FROM groups g
            JOIN group_moderators gm ON gm.group_id = g.group_id
            WHERE gm.user_id = ?
            ORDER BY g.group_id
            """,
            (user_id,),
        ).fetchall()


def is_moderator(group_id: int, user_id: int) -> bool:
    """Return whether the user is a moderator of the given group."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM group_moderators WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()
    return row is not None


def list_moderators(group_id: int) -> list[int]:
    """Return moderator ids for a group."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM group_moderators WHERE group_id = ? ORDER BY user_id",
            (group_id,),
        ).fetchall()
    return [int(row["user_id"]) for row in rows]


def list_group_moderators(group_id: int, search: str = "") -> list[sqlite3.Row]:
    """Return moderator rows for WebApp management, optionally filtered by search text."""
    query = (search or "").strip().casefold()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT gm.user_id, gm.added_by, gm.added_at, g.created_by,
                   u.full_name, u.username, u.at_username, u.phone, u.status
            FROM group_moderators gm
            JOIN groups g ON g.group_id = gm.group_id
            LEFT JOIN users u ON u.user_id = gm.user_id
            WHERE gm.group_id = ?
            ORDER BY (gm.user_id = g.created_by) DESC, COALESCE(u.full_name, ''), gm.user_id
            """,
            (group_id,),
        ).fetchall()

    if not query:
        return rows

    def _matches(row: sqlite3.Row) -> bool:
        haystack = [
            str(row["user_id"]),
            str(row["full_name"] or ""),
            str(row["username"] or ""),
            str(row["at_username"] or ""),
            str(row["phone"] or ""),
        ]
        return any(query in value.casefold() for value in haystack)

    return [row for row in rows if _matches(row)]


def add_moderator(group_id: int, target_user_id: int, added_by: int) -> None:
    """Grant moderator access to a user in a specific group."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO group_moderators(group_id, user_id, added_by, added_at) VALUES(?, ?, ?, ?)",
            (group_id, target_user_id, added_by, _now()),
        )


def remove_moderator(group_id: int, target_user_id: int, requester_id: int) -> tuple[bool, str]:
    """Remove a moderator while protecting the group owner and the requester."""
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
    """Set legacy review-alert flag for the group."""
    with _connect() as conn:
        conn.execute("UPDATE groups SET review_alerts = ? WHERE group_id = ?", (1 if enabled else 0, group_id))


def get_review_alerts(group_id: int) -> bool:
    """Return legacy review-alert setting for the group."""
    with _connect() as conn:
        row = conn.execute("SELECT review_alerts FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    if row is None:
        return True
    return bool(row["review_alerts"])


def set_group_paused(group_id: int, paused: bool) -> None:
    """Enable or disable dry-run moderation mode for a group."""
    with _connect() as conn:
        conn.execute("UPDATE groups SET is_paused = ? WHERE group_id = ?", (1 if paused else 0, group_id))


def is_group_paused(group_id: int) -> bool:
    """Return whether moderation actions are paused for the group."""
    with _connect() as conn:
        row = conn.execute("SELECT is_paused FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    if row is None:
        return False
    return bool(row["is_paused"])


def set_notify_pending(group_id: int, enabled: bool) -> None:
    """Toggle pending-ad Telegram alerts for a group."""
    with _connect() as conn:
        conn.execute("UPDATE groups SET notify_pending = ? WHERE group_id = ?", (1 if enabled else 0, group_id))


def get_notify_pending(group_id: int) -> bool:
    """Return pending-ad alert setting for the group."""
    with _connect() as conn:
        row = conn.execute("SELECT notify_pending FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    if row is None:
        return True
    return bool(row["notify_pending"])


def set_blocked_alert_sound(group_id: int, enabled: bool) -> None:
    """Toggle sound on blocked-ad alerts for a group."""
    with _connect() as conn:
        conn.execute("UPDATE groups SET blocked_alert_sound = ? WHERE group_id = ?", (1 if enabled else 0, group_id))


def get_blocked_alert_sound(group_id: int) -> bool:
    """Return blocked-ad sound setting for a group."""
    with _connect() as conn:
        row = conn.execute("SELECT blocked_alert_sound FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    if row is None:
        return False
    return bool(row["blocked_alert_sound"])


def set_swipe_requires_confirm(group_id: int, enabled: bool) -> None:
    """Toggle confirmation requirement for WebApp swipe actions."""
    with _connect() as conn:
        conn.execute("UPDATE groups SET swipe_requires_confirm = ? WHERE group_id = ?", (1 if enabled else 0, group_id))


def get_swipe_requires_confirm(group_id: int) -> bool:
    """Return whether swipe gestures require confirmation in the WebApp."""
    with _connect() as conn:
        row = conn.execute("SELECT swipe_requires_confirm FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    if row is None:
        return True
    return bool(row["swipe_requires_confirm"])


def set_hide_confirmed_blocked(group_id: int, enabled: bool) -> None:
    """Toggle hiding already confirmed blocked ads in the WebApp."""
    with _connect() as conn:
        conn.execute("UPDATE groups SET hide_confirmed_blocked = ? WHERE group_id = ?", (1 if enabled else 0, group_id))


def get_hide_confirmed_blocked(group_id: int) -> bool:
    """Return whether confirmed blocked ads are hidden in the WebApp."""
    with _connect() as conn:
        row = conn.execute("SELECT hide_confirmed_blocked FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    if row is None:
        return False
    return bool(row["hide_confirmed_blocked"])


def list_whitelist(group_id: int) -> list[int]:
    """Return legalised advertiser ids for the group."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM group_whitelist WHERE group_id = ? ORDER BY user_id",
            (group_id,),
        ).fetchall()
    return [int(row["user_id"]) for row in rows]


def add_whitelist(group_id: int, user_id: int, added_by: int) -> None:
    """Add a user to the group whitelist."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO group_whitelist(group_id, user_id, added_by, added_at) VALUES(?, ?, ?, ?)",
            (group_id, user_id, added_by, _now()),
        )


def remove_whitelist(group_id: int, user_id: int) -> None:
    """Remove a user from the group whitelist."""
    with _connect() as conn:
        conn.execute("DELETE FROM group_whitelist WHERE group_id = ? AND user_id = ?", (group_id, user_id))


def is_whitelisted(group_id: int, user_id: int) -> bool:
    """Return whether the user is legalised for the group."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM group_whitelist WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()
    return row is not None


def list_hardwords(group_id: int) -> list[str]:
    """Return all custom triggers as a flat list for classifier policy."""
    return [row["value"] for row in list_group_triggers(group_id)]


def add_hardword(group_id: int, word: str, added_by: int) -> None:
    """Backward-compatible wrapper that stores a hardword as a trigger."""
    normalized = word.strip().lower()
    if not normalized:
        return
    add_group_trigger(group_id, "phrase" if " " in normalized else "word", normalized, added_by)


def remove_hardword(group_id: int, word: str) -> None:
    """Backward-compatible wrapper that removes a hardword and its trigger entry."""
    normalized = word.strip().lower()
    with _connect() as conn:
        conn.execute(
            "DELETE FROM group_hardwords WHERE group_id = ? AND word = ?",
            (group_id, normalized),
        )
    delete_group_trigger_by_value(group_id, normalized)


def list_group_triggers(
    group_id: int,
    trigger_type: str | None = None,
    search: str = "",
    sort: str = "alpha",
) -> list[sqlite3.Row]:
    """Return custom triggers for the group with optional type, search and sort."""
    with _connect() as conn:
        where = ["group_id = ?"]
        params: list[object] = [group_id]
        if trigger_type:
            where.append("trigger_type = ?")
            params.append(trigger_type)
        if search.strip():
            where.append("value LIKE ?")
            params.append(f"%{search.strip().lower()}%")

        if sort == "newest":
            order_by = "added_at DESC, value ASC"
        elif sort == "oldest":
            order_by = "added_at ASC, value ASC"
        else:
            order_by = "value ASC"

        sql = (
            "SELECT trigger_id, trigger_type, value, added_by, added_at, updated_at "
            "FROM group_triggers "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY {f'trigger_type, {order_by}' if not trigger_type else order_by}"
        )
        return conn.execute(sql, params).fetchall()


def add_group_trigger(group_id: int, trigger_type: str, value: str, added_by: int) -> None:
    """Insert a normalized custom trigger and mirror it to the legacy table."""
    normalized = value.strip().lower()
    if not normalized:
        return
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO group_triggers(group_id, trigger_type, value, added_by, added_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (group_id, trigger_type, normalized, added_by, now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO group_hardwords(group_id, word, added_by, added_at) VALUES(?, ?, ?, ?)",
            (group_id, normalized, added_by, now),
        )


def update_group_trigger(group_id: int, trigger_id: int, trigger_type: str, value: str) -> bool:
    """Update an existing trigger value and keep legacy mirror rows in sync."""
    normalized = value.strip().lower()
    if not normalized:
        return False
    now = _now()
    with _connect() as conn:
        current = conn.execute(
            "SELECT value FROM group_triggers WHERE group_id = ? AND trigger_id = ? AND trigger_type = ?",
            (group_id, trigger_id, trigger_type),
        ).fetchone()
        if current is None:
            return False
        old_value = str(current["value"])
        conn.execute(
            """
            UPDATE group_triggers
            SET value = ?, updated_at = ?
            WHERE group_id = ? AND trigger_id = ? AND trigger_type = ?
            """,
            (normalized, now, group_id, trigger_id, trigger_type),
        )
        conn.execute("DELETE FROM group_hardwords WHERE group_id = ? AND word = ?", (group_id, old_value))
        conn.execute(
            "INSERT OR IGNORE INTO group_hardwords(group_id, word, added_by, added_at) VALUES(?, ?, ?, ?)",
            (group_id, normalized, 0, now),
        )
    return True


def delete_group_trigger(group_id: int, trigger_id: int) -> bool:
    """Delete a trigger by id and remove its legacy mirror row."""
    with _connect() as conn:
        current = conn.execute(
            "SELECT value FROM group_triggers WHERE group_id = ? AND trigger_id = ?",
            (group_id, trigger_id),
        ).fetchone()
        if current is None:
            return False
        conn.execute("DELETE FROM group_triggers WHERE group_id = ? AND trigger_id = ?", (group_id, trigger_id))
        conn.execute("DELETE FROM group_hardwords WHERE group_id = ? AND word = ?", (group_id, str(current["value"])))
    return True


def delete_group_trigger_by_value(group_id: int, value: str) -> None:
    """Delete trigger rows by normalized value."""
    normalized = value.strip().lower()
    with _connect() as conn:
        conn.execute("DELETE FROM group_triggers WHERE group_id = ? AND value = ?", (group_id, normalized))


def get_policy(group_id: int) -> dict:
    """Build the classifier policy payload for a group."""
    whitelist = set(list_whitelist(group_id))
    return {
        "review_alerts": get_review_alerts(group_id),
        "is_paused": is_group_paused(group_id),
        "notify_pending": get_notify_pending(group_id),
        "blocked_alert_sound": get_blocked_alert_sound(group_id),
        "hide_confirmed_blocked": get_hide_confirmed_blocked(group_id),
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
    """Insert or update a known Telegram user record."""
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


def upsert_group_user(group_id: int, user_id: int) -> None:
    """Track that a user has been seen in a group."""
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO group_users(group_id, user_id, first_seen_at, last_seen_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(group_id, user_id)
            DO UPDATE SET last_seen_at=excluded.last_seen_at
            """,
            (group_id, user_id, now, now),
        )


def list_group_users_for_whitelist(
    group_id: int,
    search: str = "",
    limit: int = 8,
    offset: int = 0,
) -> tuple[list[sqlite3.Row], int]:
    """Return paginated known users of a group for WebApp search."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT u.user_id, u.full_name, u.username, u.at_username, u.phone, u.status, gu.last_seen_at
            FROM group_users gu
            JOIN users u ON u.user_id = gu.user_id
            WHERE gu.group_id = ?
            ORDER BY gu.last_seen_at DESC, u.user_id DESC
            """,
            (group_id,),
        ).fetchall()

    query = (search or "").strip().casefold()
    if query:
        filtered = []
        for row in rows:
            haystack = [
                str(row["user_id"]),
                str(row["full_name"] or ""),
                str(row["username"] or ""),
                str(row["at_username"] or ""),
                str(row["phone"] or ""),
            ]
            if any(query in value.casefold() for value in haystack):
                filtered.append(row)
        rows = filtered

    total = len(rows)
    sliced = rows[offset : offset + limit]
    return sliced, total


def set_user_status(user_id: int, status: str) -> None:
    """Update current moderation status for a known user."""
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
    """Create or reuse an ad record identified by source message id."""
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
    """Return a single ad row joined with user metadata."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT a.*, u.full_name, u.username, u.at_username, u.phone, u.status AS user_status
            FROM ads a
            LEFT JOIN users u ON u.user_id = a.user_id
            WHERE a.ad_id = ?
            """,
            (ad_id,),
        ).fetchone()


def list_ads(group_id: int, category: str, unresolved_only: bool = True) -> list[sqlite3.Row]:
    """Return ads in a category, optionally limited to unresolved ones."""
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


def list_ads_for_web(
    group_id: int,
    category: str,
    search: str = "",
    sort: str = "newest",
    limit: int = 20,
    offset: int = 0,
    hide_confirmed_blocked: bool = False,
) -> tuple[list[sqlite3.Row], int]:
    """Return paginated ads for the WebApp along with total count."""
    normalized = (search or "").strip().lower()
    params: list[object] = [group_id, category]
    where = ["a.group_id = ?", "a.category = ?"]

    if category != "blocked":
        where.append("a.requires_action = 1")
    elif hide_confirmed_blocked:
        where.append("a.requires_action = 1")

    if normalized:
        like = f"%{normalized}%"
        where.append(
            """(
                CAST(a.ad_id AS TEXT) LIKE ? OR
                CAST(a.user_id AS TEXT) LIKE ? OR
                LOWER(COALESCE(a.text, '')) LIKE ? OR
                LOWER(COALESCE(u.full_name, '')) LIKE ? OR
                LOWER(COALESCE(u.username, '')) LIKE ? OR
                LOWER(COALESCE(u.at_username, '')) LIKE ? OR
                LOWER(COALESCE(u.phone, '')) LIKE ? OR
                LOWER(COALESCE(a.decision, '')) LIKE ?
            )"""
        )
        params.extend([like, like, like, like, like, like, like, like])

    order_map = {
        "newest": "a.created_at DESC, a.ad_id DESC",
        "oldest": "a.created_at ASC, a.ad_id ASC",
        "user": "LOWER(COALESCE(u.full_name, '')) ASC, a.created_at DESC",
        "decision": "LOWER(COALESCE(a.decision, '')) ASC, a.created_at DESC",
    }
    order_sql = order_map.get(sort, order_map["newest"])
    where_sql = " AND ".join(where)

    with _connect() as conn:
        total_row = conn.execute(
            f"""
            SELECT COUNT(*) AS cnt
            FROM ads a
            LEFT JOIN users u ON u.user_id = a.user_id
            WHERE {where_sql}
            """,
            tuple(params),
        ).fetchone()
        total = int(total_row["cnt"]) if total_row else 0

        rows = conn.execute(
            f"""
            SELECT a.*, u.full_name, u.username, u.at_username, u.phone, u.status AS user_status
            FROM ads a
            LEFT JOIN users u ON u.user_id = a.user_id
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
    return rows, total


def get_unresolved_counts(group_id: int) -> dict[str, int]:
    """Return unresolved ad counters per moderation category."""
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
    """Append an action entry to ad history."""
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
    """Update ad decision fields and write a corresponding history record."""
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


def confirm_all(
    group_id: int,
    category: str,
    moderator_id: int,
    decision: str = "approved",
    action: str = "approved_all",
) -> int:
    """Bulk-confirm all unresolved ads in a category and record audit history."""
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
            SET decision = ?, decided_by = ?, requires_action = 0, updated_at = ?, resolved_at = ?
            WHERE group_id = ? AND category = ? AND requires_action = 1
            """,
            (decision, moderator_id, now, now, group_id, category),
        )

    for ad_id in ids:
        record_ad_action(ad_id, group_id, moderator_id, action, "approve_all")
    return len(ids)


def get_alert_state(moderator_id: int, group_id: int, category: str) -> sqlite3.Row | None:
    """Return current Telegram alert message state for one moderator/category pair."""
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
    """Persist current Telegram alert message id for a moderator/category pair."""
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
    """Delete alert state for a specific moderator/category pair."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM moderator_alert_state WHERE moderator_id = ? AND group_id = ? AND category = ?",
            (moderator_id, group_id, category),
        )


def clear_all_alert_states() -> None:
    """Delete all alert state rows."""
    with _connect() as conn:
        conn.execute("DELETE FROM moderator_alert_state")


def latest_ads_history(limit: int = 200) -> list[sqlite3.Row]:
    """Return recent ad history rows for manual inspection or debugging."""
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


def get_private_context_state(user_id: int) -> sqlite3.Row | None:
    """Return stored private-chat UI state for a user."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT user_id, chat_id, last_bot_message_id, last_user_message_id, selected_group_id, updated_at
            FROM private_context_state
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()


def upsert_private_context_state(
    user_id: int,
    chat_id: int,
    last_bot_message_id: int | None = None,
    last_user_message_id: int | None = None,
    selected_group_id: int | None = None,
) -> None:
    """Insert or update private-chat UI state for a user."""
    now = _now()
    with _connect() as conn:
        current = conn.execute(
            "SELECT user_id, last_bot_message_id, last_user_message_id, selected_group_id FROM private_context_state WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if current is None:
            conn.execute(
                """
                INSERT INTO private_context_state(user_id, chat_id, last_bot_message_id, last_user_message_id, selected_group_id, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (user_id, chat_id, last_bot_message_id, last_user_message_id, selected_group_id, now),
            )
            return

        conn.execute(
            """
            UPDATE private_context_state
            SET chat_id = ?,
                last_bot_message_id = COALESCE(?, last_bot_message_id),
                last_user_message_id = COALESCE(?, last_user_message_id),
                selected_group_id = COALESCE(?, selected_group_id),
                updated_at = ?
            WHERE user_id = ?
            """,
            (chat_id, last_bot_message_id, last_user_message_id, selected_group_id, now, user_id),
        )


def clear_private_context_bot_message(user_id: int) -> None:
    """Clear the last bot message id from a user's private context state."""
    with _connect() as conn:
        conn.execute(
            "UPDATE private_context_state SET last_bot_message_id = NULL, updated_at = ? WHERE user_id = ?",
            (_now(), user_id),
        )


def clear_all_private_context_bot_messages() -> None:
    """Clear stored last bot message ids for all private contexts."""
    with _connect() as conn:
        conn.execute(
            "UPDATE private_context_state SET last_bot_message_id = NULL, updated_at = ? WHERE last_bot_message_id IS NOT NULL",
            (_now(),),
        )


def list_private_contexts_older_than(age_seconds: int) -> list[sqlite3.Row]:
    """Return private contexts whose tracked bot message is older than a given age."""
    cutoff = _now() - age_seconds
    with _connect() as conn:
        return conn.execute(
            """
            SELECT user_id, chat_id, last_bot_message_id, last_user_message_id, selected_group_id, updated_at
            FROM private_context_state
            WHERE last_bot_message_id IS NOT NULL AND updated_at <= ?
            ORDER BY updated_at ASC
            """,
            (cutoff,),
        ).fetchall()


def list_active_private_contexts() -> list[sqlite3.Row]:
    """Return all private contexts that currently have an associated chat."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT user_id, chat_id, last_bot_message_id, last_user_message_id, selected_group_id, updated_at
            FROM private_context_state
            WHERE chat_id IS NOT NULL
            ORDER BY updated_at DESC, user_id DESC
            """
        ).fetchall()


def track_private_bot_message(chat_id: int, user_id: int, message_id: int, kind: str) -> None:
    """Track a bot-generated private message for later cleanup."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO private_bot_messages(chat_id, message_id, user_id, kind, created_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id)
            DO UPDATE SET user_id=excluded.user_id, kind=excluded.kind, created_at=excluded.created_at
            """,
            (chat_id, message_id, user_id, kind, _now()),
        )


def untrack_private_bot_message(chat_id: int, message_id: int) -> None:
    """Remove a tracked private message record."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM private_bot_messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )


def list_tracked_private_bot_messages() -> list[sqlite3.Row]:
    """Return all tracked private bot messages scheduled for cleanup."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT chat_id, message_id, user_id, kind, created_at
            FROM private_bot_messages
            ORDER BY created_at ASC, chat_id ASC, message_id ASC
            """
        ).fetchall()


def clear_tracked_private_bot_messages() -> None:
    """Delete all tracked private bot message rows."""
    with _connect() as conn:
        conn.execute("DELETE FROM private_bot_messages")


def list_private_users_with_activity() -> list[int]:
    """Return user ids that currently have tracked private-chat activity."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT user_id
            FROM (
                SELECT user_id FROM private_context_state
                UNION
                SELECT moderator_id AS user_id FROM moderator_alert_state
                UNION
                SELECT user_id FROM private_bot_messages
            )
            WHERE user_id IS NOT NULL
            ORDER BY user_id
            """
        ).fetchall()
    return [int(row["user_id"]) for row in rows]


def set_selected_group(user_id: int, chat_id: int, group_id: int) -> None:
    """Persist currently selected group in private UI state."""
    upsert_private_context_state(
        user_id=user_id,
        chat_id=chat_id,
        selected_group_id=group_id,
    )


def get_selected_group(user_id: int) -> int | None:
    """Return currently selected group id for a user, if any."""
    row = get_private_context_state(user_id)
    if row is None:
        return None
    value = row["selected_group_id"]
    if value is None:
        return None
    return int(value)
