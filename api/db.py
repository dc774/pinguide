"""SQLite storage: accounts, saved chats, sign-in tokens and daily web-search usage.

One file, stdlib sqlite3, no separate database service. On Railway it lives on
the Volume (see PINGUIDE_DB_PATH in CLAUDE.md), in a subfolder the ingest leaves
alone. A new connection is opened per call: gunicorn serves requests from
several threads, and sqlite3 connections must not be shared between threads.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv(
    "PINGUIDE_DB_PATH",
    str(Path(__file__).parent.parent / "vector_store" / "app" / "pinguide.db"),
))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT 'New chat',
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_user ON conversations(user_id, updated);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    thinking TEXT NOT NULL DEFAULT '',
    searches TEXT NOT NULL DEFAULT '[]',
    sources TEXT NOT NULL DEFAULT '[]',
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS login_tokens (
    token_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    expires TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,
    who TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, who)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as conn, conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)


# --------------------------------------------------------------------------
# Daily counters (web searches, sign-in emails)
# --------------------------------------------------------------------------

def usage_get(who: str, day: str | None = None) -> int:
    with closing(connect()) as conn:
        row = conn.execute("SELECT count FROM usage WHERE day = ? AND who = ?",
                           (day or today(), who)).fetchone()
    return row["count"] if row else 0


def usage_add(who: str, n: int, day: str | None = None) -> None:
    if n <= 0:
        return
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO usage (day, who, count) VALUES (?, ?, ?) "
            "ON CONFLICT (day, who) DO UPDATE SET count = count + excluded.count",
            (day or today(), who, n),
        )


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

def upsert_user(email: str, name: str = "") -> int:
    """Return the user id for an email, creating the account on first sign-in.

    Google and email-link sign-ins with the same address share one account.
    """
    email = email.strip().lower()
    with closing(connect()) as conn, conn:
        conn.execute("INSERT INTO users (email, name, created) VALUES (?, ?, ?) "
                     "ON CONFLICT (email) DO NOTHING", (email, name, now()))
        if name:
            conn.execute("UPDATE users SET name = ? WHERE email = ? AND name = ''", (name, email))
        return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


def get_user(user_id: int) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT id, email, name FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def delete_user(user_id: int) -> None:
    """Delete the account; its conversations and messages go with it (ON DELETE CASCADE)."""
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# --------------------------------------------------------------------------
# Email sign-in tokens (only a hash is stored)
# --------------------------------------------------------------------------

def save_login_token(token_hash: str, email: str, expires: str) -> None:
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM login_tokens WHERE expires < ?", (now(),))
        conn.execute("INSERT INTO login_tokens (token_hash, email, expires) VALUES (?, ?, ?)",
                     (token_hash, email, expires))


def use_login_token(token_hash: str) -> str | None:
    """Consume a token: return its email if it exists and hasn't expired. Single use."""
    with closing(connect()) as conn, conn:
        row = conn.execute("DELETE FROM login_tokens WHERE token_hash = ? RETURNING email, expires",
                           (token_hash,)).fetchone()
    if row is None or row["expires"] < now():
        return None
    return row["email"]


# --------------------------------------------------------------------------
# Conversations (every query is scoped to its owner)
# --------------------------------------------------------------------------

def list_conversations(user_id: int, query: str = "", limit: int = 200) -> list[dict]:
    sql = "SELECT id, title, updated FROM conversations c WHERE user_id = ?"
    args: list = [user_id]
    if query:
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += (" AND (title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM messages m "
                "WHERE m.conversation_id = c.id AND m.content LIKE ? ESCAPE '\\'))")
        args += [like, like]
    sql += " ORDER BY updated DESC LIMIT ?"
    args.append(limit)
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def get_conversation(user_id: int, conversation_id: str) -> dict | None:
    with closing(connect()) as conn:
        conv = conn.execute("SELECT id, title, updated FROM conversations WHERE id = ? AND user_id = ?",
                            (conversation_id, user_id)).fetchone()
        if conv is None:
            return None
        rows = conn.execute(
            "SELECT role, content, thinking, searches, sources FROM messages "
            "WHERE conversation_id = ? ORDER BY id", (conversation_id,)).fetchall()
    messages = [{"role": r["role"], "content": r["content"], "thinking": r["thinking"],
                 "searches": json.loads(r["searches"]), "sources": json.loads(r["sources"])}
                for r in rows]
    return {**dict(conv), "messages": messages}


def start_turn(user_id: int, conversation_id: str, keep: int, question: str) -> list[dict]:
    """Prepare a conversation for a new question and return the prior history.

    Creates the conversation if needed, keeps only its first `keep` messages
    (Edit and Retry regenerate from an earlier point), then appends the question.
    Raises PermissionError if the id belongs to another user's conversation.
    """
    stamp = now()
    with closing(connect()) as conn, conn:
        conn.execute("INSERT INTO conversations (id, user_id, created, updated) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (id) DO NOTHING", (conversation_id, user_id, stamp, stamp))
        owner = conn.execute("SELECT user_id FROM conversations WHERE id = ?",
                             (conversation_id,)).fetchone()["user_id"]
        if owner != user_id:
            raise PermissionError("conversation belongs to another user")  # rolls back
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,))]
        drop = ids[max(keep, 0):]
        if drop:
            conn.executemany("DELETE FROM messages WHERE id = ?", [(i,) for i in drop])
        history = [dict(r) for r in conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,))]
        conn.execute("INSERT INTO messages (conversation_id, role, content, created) VALUES (?, 'user', ?, ?)",
                     (conversation_id, question, stamp))
        conn.execute("UPDATE conversations SET updated = ? WHERE id = ?", (stamp, conversation_id))
    return history


def save_answer(conversation_id: str, content: str, thinking: str, searches: list, sources: list) -> None:
    stamp = now()
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, thinking, searches, sources, created) "
            "VALUES (?, 'assistant', ?, ?, ?, ?, ?)",
            (conversation_id, content, thinking, json.dumps(searches), json.dumps(sources), stamp))
        conn.execute("UPDATE conversations SET updated = ? WHERE id = ?", (stamp, conversation_id))


def set_title(user_id: int, conversation_id: str, title: str) -> bool:
    with closing(connect()) as conn, conn:
        cur = conn.execute("UPDATE conversations SET title = ? WHERE id = ? AND user_id = ?",
                           (title, conversation_id, user_id))
    return cur.rowcount > 0


def delete_conversation(user_id: int, conversation_id: str) -> bool:
    with closing(connect()) as conn, conn:
        cur = conn.execute("DELETE FROM conversations WHERE id = ? AND user_id = ?",
                           (conversation_id, user_id))
    return cur.rowcount > 0
