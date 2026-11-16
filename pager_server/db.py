"""SQLite storage for users, devices, messages and delivery acknowledgements."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config
from .security import hash_password, hash_token, new_device_token

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS acks (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    device_id  INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    acked_at   TEXT NOT NULL,
    PRIMARY KEY (message_id, device_id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


# ---------------------------------------------------------------- users

def add_user(username: str, password: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), now()),
        )


def set_password(username: str, password: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (hash_password(password), username),
        )
        return cur.rowcount > 0


def delete_user(username: str) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM users WHERE username = ?", (username,)).rowcount > 0


def get_user(username: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def list_users():
    with connect() as conn:
        return conn.execute("SELECT username, created_at FROM users ORDER BY username").fetchall()


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
        cur = conn.execute(
            "INSERT INTO messages (sender, body, created_at) VALUES (?, ?, ?)",
            (sender, body, created),
        )
        return {"id": cur.lastrowid, "sender": sender, "body": body, "created_at": created}


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
