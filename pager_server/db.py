"""SQLite storage: users, sessions, devices, messages, acknowledgements, audit log."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config
from .security import hash_password, hash_token, new_device_token, new_session_token

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY,
    username             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash        TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    is_admin             INTEGER NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    totp_secret          TEXT,
    totp_last_step       INTEGER
);
CREATE TABLE IF NOT EXISTS devices (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_seen  TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    sender     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    auth_at      TEXT,
    totp_pending TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS acks (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    device_id  INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    acked_at   TEXT NOT NULL,
    PRIMARY KEY (message_id, device_id)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT,
    details TEXT,
    ip      TEXT
);
-- Monotonic counters that must survive deletions (e.g. page ids: pagers ignore
-- ids they have already seen, so ids must never be reused after a wipe/purge).
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

# Columns added after the first release; added to existing databases on startup.
MIGRATIONS = {
    "users": [
        ("is_admin", "INTEGER NOT NULL DEFAULT 0"),
        ("must_change_password", "INTEGER NOT NULL DEFAULT 0"),
        ("totp_secret", "TEXT"),
        ("totp_last_step", "INTEGER"),
    ],
    "sessions": [("auth_at", "TEXT"), ("totp_pending", "TEXT")],
}

AUDIT_RETENTION_DAYS = 365


class LastAdminError(Exception):
    """Raised when an action would leave the system without any admin."""


def now(offset_seconds: float = 0) -> str:
    """UTC timestamp as ISO text; same format everywhere so strings compare correctly."""
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return t.isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# ---------------------------------------------------------------- users

def add_user(username: str, password: str, is_admin: bool = False, must_change: bool = False) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at, is_admin, must_change_password) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, hash_password(password), now(), int(is_admin), int(must_change)),
        )
        return cur.lastrowid


def set_password(username: str, password: str, must_change: bool = False,
                 keep_session: str | None = None) -> bool:
    """Change a password and end the user's sessions (except `keep_session`, if given)."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = ? WHERE username = ?",
            (hash_password(password), int(must_change), username),
        )
        conn.execute(
            "DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ?) "
            "AND token_hash != ?",
            (username, hash_token(keep_session) if keep_session else ""),
        )
        return cur.rowcount > 0


def _ensure_other_admin(conn, username: str) -> None:
    target = conn.execute("SELECT is_admin FROM users WHERE username = ?", (username,)).fetchone()
    if not (target and target["is_admin"]):
        return
    others = conn.execute(
        "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND username != ?", (username,)
    ).fetchone()[0]
    if others == 0:
        raise LastAdminError("This is the last admin account")


def delete_user(username: str) -> bool:
    with connect() as conn:
        _ensure_other_admin(conn, username)
        return conn.execute("DELETE FROM users WHERE username = ?", (username,)).rowcount > 0


def set_admin(username: str, is_admin: bool) -> bool:
    """Grant or revoke admin. Ends the user's sessions so the change applies at next login."""
    with connect() as conn:
        if not is_admin:
            _ensure_other_admin(conn, username)
        cur = conn.execute(
            "UPDATE users SET is_admin = ? WHERE username = ?", (int(is_admin), username)
        )
        conn.execute(
            "DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ?)",
            (username,),
        )
        return cur.rowcount > 0


def set_totp(user_id: int, secret: str | None, used_step: int | None = None) -> None:
    """Enable (secret) or disable (None) two-factor authentication for a user."""
    with connect() as conn:
        conn.execute(
            "UPDATE users SET totp_secret = ?, totp_last_step = ? WHERE id = ?",
            (secret, used_step, user_id),
        )


def reset_totp(username: str) -> bool:
    """Admin action: remove 2FA and end sessions; the user sets it up again if required."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE users SET totp_secret = NULL, totp_last_step = NULL WHERE username = ?",
            (username,),
        )
        conn.execute(
            "DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ?)",
            (username,),
        )
        return cur.rowcount > 0


def use_totp_step(user_id: int, step: int) -> bool:
    """Atomically mark a TOTP time step as used; False if it (or a later one) was used already."""
    with connect() as conn:
        return conn.execute(
            "UPDATE users SET totp_last_step = ? WHERE id = ? "
            "AND (totp_last_step IS NULL OR totp_last_step < ?)",
            (step, user_id, step),
        ).rowcount > 0


def get_user(username: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def get_user_by_id(user_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users():
    with connect() as conn:
        return conn.execute(
            """
            SELECT u.username, u.created_at, u.is_admin, u.must_change_password,
                   u.totp_secret IS NOT NULL AS has_2fa,
                   (SELECT COUNT(*) FROM sessions s
                     WHERE s.user_id = u.id AND s.expires_at > ?) AS sessions
            FROM users u ORDER BY u.username
            """,
            (now(),),
        ).fetchall()


def rename_user(old: str, new: str) -> bool:
    """Change a username. Sessions stay valid (they refer to the user id).

    Raises sqlite3.IntegrityError if the new name is taken (case-insensitive).
    Past pages and audit entries keep the name used at the time.
    """
    with connect() as conn:
        return conn.execute(
            "UPDATE users SET username = ? WHERE username = ?", (new, old)
        ).rowcount > 0


def admin_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]


# ------------------------------------------------------------- sessions

def create_session(user_id: int, max_age: int) -> str:
    """Start a login session; returns the token to put in the (signed) cookie."""
    token = new_session_token()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, auth_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (hash_token(token), user_id, now(), now(max_age), now()),
        )
    return token


def get_session(token: str):
    """Session joined with its user, if valid and unexpired."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT s.auth_at, s.totp_pending, u.id AS user_id, u.username, u.is_admin,
                   u.must_change_password, u.totp_secret IS NOT NULL AS has_2fa
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (hash_token(token), now()),
        ).fetchone()


def touch_auth(token: str) -> None:
    """Record that the user just proved who they are (password, and 2FA if enabled)."""
    with connect() as conn:
        conn.execute("UPDATE sessions SET auth_at = ? WHERE token_hash = ?", (now(), hash_token(token)))


def set_totp_pending(token: str, secret: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE sessions SET totp_pending = ? WHERE token_hash = ?", (secret, hash_token(token))
        )


def delete_session(token: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))


def delete_user_sessions(username: str) -> int:
    with connect() as conn:
        return conn.execute(
            "DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ?)",
            (username,),
        ).rowcount


# ------------------------------------------------------------ audit log

def audit(actor: str, action: str, target: str | None = None,
          details: str | None = None, ip: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, target, details, ip) VALUES (?, ?, ?, ?, ?, ?)",
            (now(), actor, action, target, details, ip),
        )


def recent_audit(limit: int = 200):
    with connect() as conn:
        return conn.execute(
            "SELECT ts, actor, action, target, details, ip FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


# ----------------------------------------------------------- housekeeping

def purge_expired(retention_days: int) -> tuple[int, int]:
    """Delete expired sessions, old audit entries and (if retention_days > 0) old pages."""
    with connect() as conn:
        sessions = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),)).rowcount
        conn.execute(
            "DELETE FROM audit_log WHERE ts < ?", (now(-AUDIT_RETENTION_DAYS * 86400),)
        )
        pages = 0
        if retention_days > 0:
            _remember_highest_message_id(conn)
            pages = conn.execute(
                "DELETE FROM messages WHERE created_at < ?", (now(-retention_days * 86400),)
            ).rowcount
    return sessions, pages


# -------------------------------------------------------------- devices

def add_device(name: str) -> str:
    """Register a device and return its (plaintext) token. Only shown once."""
    token = new_device_token()
    with connect() as conn:
        conn.execute(
            "INSERT INTO devices (name, token_hash, created_at) VALUES (?, ?, ?)",
            (name, hash_token(token), now()),
        )
    return token


def rotate_device_token(name: str) -> str | None:
    token = new_device_token()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE devices SET token_hash = ? WHERE name = ?", (hash_token(token), name)
        )
        return token if cur.rowcount else None


def delete_device(name: str) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM devices WHERE name = ?", (name,)).rowcount > 0


def device_by_token(token: str):
    with connect() as conn:
        return conn.execute(
            "SELECT id, name FROM devices WHERE token_hash = ?", (hash_token(token),)
        ).fetchone()


def touch_device(device_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now(), device_id))


def list_devices():
    with connect() as conn:
        return conn.execute(
            "SELECT id, name, created_at, last_seen FROM devices ORDER BY name"
        ).fetchall()


# ------------------------------------------------------------- messages

def add_message(sender: str, body: str) -> dict:
    created = now()
    with connect() as conn:
        # Next id = 1 + highest id ever issued, even if those pages were deleted since.
        last = conn.execute("SELECT value FROM counters WHERE name = 'message_id'").fetchone()
        highest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        new_id = max(last["value"] if last else 0, highest) + 1
        conn.execute(
            "INSERT INTO messages (id, sender, body, created_at) VALUES (?, ?, ?, ?)",
            (new_id, sender, body, created),
        )
        conn.execute(
            "INSERT INTO counters (name, value) VALUES ('message_id', ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (new_id,),
        )
        return {"id": new_id, "sender": sender, "body": body, "created_at": created}


def wipe_messages() -> int:
    """Delete the whole page history (acks cascade). Page ids keep counting up."""
    with connect() as conn:
        _remember_highest_message_id(conn)
        return conn.execute("DELETE FROM messages").rowcount


def _remember_highest_message_id(conn) -> None:
    conn.execute(
        "INSERT INTO counters (name, value) SELECT 'message_id', COALESCE(MAX(id), 0) FROM messages "
        "WHERE true ON CONFLICT(name) DO UPDATE SET value = MAX(value, excluded.value)"
    )


def recent_messages(limit: int = 25):
    with connect() as conn:
        return conn.execute(
            """
            SELECT m.id, m.sender, m.body, m.created_at,
                   (SELECT COUNT(*) FROM acks a WHERE a.message_id = m.id) AS acks
            FROM messages m ORDER BY m.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()


def messages_since(message_id: int, limit: int):
    """The newest `limit` messages with id > message_id, oldest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, sender, body, created_at FROM messages WHERE id > ? "
            "ORDER BY id DESC LIMIT ?",
            (message_id, limit),
        ).fetchall()
    return list(reversed(rows))


def record_ack(message_id: int, device_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO acks (message_id, device_id, acked_at) "
            "SELECT id, ?, ? FROM messages WHERE id = ?",
            (device_id, now(), message_id),
        )
