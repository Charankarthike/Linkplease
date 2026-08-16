"""
All persistence lives here, as plain SQLite. Nothing about retries, dedup
state, or the send queue is ever held only in memory -- if the process
restarts mid-flight, everything needed to pick back up is in this file.

Concurrency note: FastAPI request handlers and the background loops all
run in the same asyncio event loop, but sqlite3 calls are blocking, so
every DB function here is synchronous and short. WAL mode lets readers
and a single writer coexist without "database is locked" errors at the
event volumes this assignment describes (500 events / 10s). A single
process-wide lock serializes writers on top of that as a belt-and-braces
measure -- see db_lock in worker.py.
"""
import sqlite3
import time
import uuid
from contextlib import contextmanager

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    rule_id TEXT PRIMARY KEY,
    keyword TEXT NOT NULL,
    keyword_lower TEXT NOT NULL,
    dm_message TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    comment_id TEXT PRIMARY KEY,
    post_id TEXT,
    text TEXT,
    user_id TEXT,
    username TEXT,
    created_at TEXT,
    deleted INTEGER NOT NULL DEFAULT 0
);

-- Every webhook delivery lands here first, keyed by event_id. Redelivered
-- events (same event_id, ~8% of traffic per the spec) are silently
-- absorbed by the PRIMARY KEY / INSERT OR IGNORE below -- the webhook
-- handler never has to think about it, and neither does anything
-- downstream.
CREATE TABLE IF NOT EXISTS event_queue (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    comment_id TEXT,
    post_id TEXT,
    text TEXT,
    user_id TEXT,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | error
    created_at REAL NOT NULL,
    error TEXT
);

-- The dedup + send-state table. PRIMARY KEY(rule_id, user_id) is what
-- guarantees "the same user never gets DMed twice for the same rule":
-- reserving a slot is a single INSERT OR IGNORE, and a rowcount of 0
-- means someone already claimed it.
CREATE TABLE IF NOT EXISTS dm_dedup (
    rule_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    dm_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | queued | delivered | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (rule_id, user_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_event_queue_status ON event_queue(status);
CREATE INDEX IF NOT EXISTS idx_dm_dedup_status ON dm_dedup(status, next_attempt_at);
"""


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO counters(name, value) VALUES ('duplicates_blocked', 0)")
    conn.commit()
    conn.close()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- rules ----------

def create_rule(keyword: str, dm_message: str) -> dict:
    rule_id = uuid.uuid4().hex
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO rules(rule_id, keyword, keyword_lower, dm_message, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (rule_id, keyword, keyword.lower(), dm_message, time.time()),
        )
    return {"rule_id": rule_id, "keyword": keyword, "dm_message": dm_message}


def get_rules() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM rules").fetchall()
        return [dict(r) for r in rows]


def get_rule(rule_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM rules WHERE rule_id = ?", (rule_id,)).fetchone()
        return dict(row) if row else None


# ---------- event queue ----------

def enqueue_event(event_id: str, event_type: str, data: dict) -> bool:
    """Returns True if this is a newly-seen event_id, False if it was a duplicate delivery."""
    comment = data or {}
    from_user = comment.get("from") or {}
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO event_queue "
            "(event_id, event_type, comment_id, post_id, text, user_id, username, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                event_id,
                event_type,
                comment.get("comment_id"),
                comment.get("post_id"),
                comment.get("text"),
                from_user.get("user_id"),
                from_user.get("username"),
                time.time(),
            ),
        )
        return cur.rowcount == 1


def fetch_pending_events(limit: int = 50) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM event_queue WHERE status = 'pending' ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_event_done(event_id: str, error: str = None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE event_queue SET status = ?, error = ? WHERE event_id = ?",
            ("error" if error else "done", error, event_id),
        )


# ---------- comments ----------

def upsert_comment_created(comment_id: str, post_id: str, text: str, user_id: str,
                            username: str, created_at: str):
    """Insert a comment if it's not already known. Deliberately does NOT
    touch `deleted` on conflict -- if a comment.deleted event for this
    comment_id arrived first (out-of-order delivery is explicitly called
    out in the spec), we must not resurrect it."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO comments (comment_id, post_id, text, user_id, username, created_at, deleted) "
            "VALUES (?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(comment_id) DO UPDATE SET "
            "post_id=excluded.post_id, text=excluded.text, user_id=excluded.user_id, "
            "username=excluded.username, created_at=excluded.created_at",
            (comment_id, post_id, text, user_id, username, created_at),
        )


def mark_comment_deleted(comment_id: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO comments (comment_id, deleted) VALUES (?, 1) "
            "ON CONFLICT(comment_id) DO UPDATE SET deleted = 1",
            (comment_id,),
        )
        # Cancel any DM that was reserved but not yet sent for this comment.
        conn.execute(
            "UPDATE dm_dedup SET status = 'failed', last_error = 'comment deleted before send', "
            "updated_at = ? WHERE comment_id = ? AND status = 'pending'",
            (time.time(), comment_id),
        )


def is_comment_deleted(comment_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT deleted FROM comments WHERE comment_id = ?", (comment_id,)).fetchone()
        return bool(row and row["deleted"])


# ---------- dm dedup / send state ----------

def reserve_dm_slot(rule_id: str, user_id: str, comment_id: str) -> bool:
    """Atomically claim the (rule_id, user_id) slot. Returns True if this
    call won the race and should proceed to send; False means a DM for
    this rule+user was already reserved/sent (duplicates_blocked++)."""
    now = time.time()
    idempotency_key = f"{rule_id}:{user_id}"
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO dm_dedup "
            "(rule_id, user_id, comment_id, status, attempts, idempotency_key, created_at, updated_at, next_attempt_at) "
            "VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?)",
            (rule_id, user_id, comment_id, idempotency_key, now, now, now),
        )
        won = cur.rowcount == 1
        if not won:
            conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'duplicates_blocked'")
        return won


def fetch_sendable(limit: int = 10) -> list:
    now = time.time()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT d.*, r.dm_message FROM dm_dedup d JOIN rules r ON r.rule_id = d.rule_id "
            "WHERE d.status = 'pending' AND d.next_attempt_at <= ? "
            "ORDER BY d.created_at LIMIT ?",
            (now, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_dm_accepted(rule_id: str, user_id: str, dm_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE dm_dedup SET status = 'queued', dm_id = ?, updated_at = ? "
            "WHERE rule_id = ? AND user_id = ?",
            (dm_id, time.time(), rule_id, user_id),
        )


def mark_dm_retry(rule_id: str, user_id: str, next_attempt_at: float, error: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE dm_dedup SET attempts = attempts + 1, next_attempt_at = ?, "
            "last_error = ?, updated_at = ? WHERE rule_id = ? AND user_id = ?",
            (next_attempt_at, error, time.time(), rule_id, user_id),
        )


def mark_dm_failed(rule_id: str, user_id: str, error: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE dm_dedup SET status = 'failed', last_error = ?, updated_at = ? "
            "WHERE rule_id = ? AND user_id = ?",
            (error, time.time(), rule_id, user_id),
        )


def fetch_reconcilable(min_age_seconds: float, limit: int = 20) -> list:
    cutoff = time.time() - min_age_seconds
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM dm_dedup WHERE status = 'queued' AND updated_at <= ? "
            "ORDER BY updated_at LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_dm_terminal(rule_id: str, user_id: str, status: str, error: str = None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE dm_dedup SET status = ?, last_error = ?, updated_at = ? "
            "WHERE rule_id = ? AND user_id = ?",
            (status, error, time.time(), rule_id, user_id),
        )


# ---------- stats ----------

def get_stats() -> dict:
    with get_conn() as conn:
        sent = conn.execute("SELECT COUNT(*) c FROM dm_dedup WHERE status = 'delivered'").fetchone()["c"]
        failed = conn.execute("SELECT COUNT(*) c FROM dm_dedup WHERE status = 'failed'").fetchone()["c"]
        queued = conn.execute(
            "SELECT COUNT(*) c FROM dm_dedup WHERE status IN ('pending', 'queued')"
        ).fetchone()["c"]
        dup = conn.execute("SELECT value FROM counters WHERE name = 'duplicates_blocked'").fetchone()["value"]
    return {"sent": sent, "failed": failed, "queued": queued, "duplicates_blocked": dup}
