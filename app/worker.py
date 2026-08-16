import asyncio
import logging
import random
import time

from app import db
from app import mock_client
from app.config import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_MAX_SECONDS,
    MAX_SEND_ATTEMPTS,
    QUEUE_POLL_INTERVAL,
    RECONCILE_MIN_AGE_SECONDS,
    RECONCILE_POLL_INTERVAL,
    SEND_POLL_INTERVAL,
)
from app.rate_limiter import limiter

log = logging.getLogger("linkplease.worker")

# A single lock serializes DB writers across the three loops + the request
# handlers. sqlite already serializes writes internally; this just avoids
# "database is locked" retries under the 500-events-in-10s burst case by
# never having two writers contend for the file at once.
db_lock = asyncio.Lock()


def backoff_seconds(attempt: int) -> float:
    base = min(BACKOFF_BASE_SECONDS * (2 ** attempt), BACKOFF_MAX_SECONDS)
    return base + random.uniform(0, base * 0.2)  # jitter


async def process_event_queue():
    """Matches incoming comments against rules and reserves DM slots.
    comment.deleted events cancel any not-yet-sent reservation."""
    while True:
        try:
            async with db_lock:
                events = db.fetch_pending_events(limit=50)

            for ev in events:
                try:
                    if ev["event_type"] == "comment.created":
                        async with db_lock:
                            db.upsert_comment_created(
                                ev["comment_id"], ev["post_id"], ev["text"],
                                ev["user_id"], ev["username"], None,
                            )
                            if not db.is_comment_deleted(ev["comment_id"]):
                                text_lower = (ev["text"] or "").lower()
                                for rule in db.get_rules():
                                    if rule["keyword_lower"] in text_lower:
                                        db.reserve_dm_slot(rule["rule_id"], ev["user_id"], ev["comment_id"])
                            db.mark_event_done(ev["event_id"])

                    elif ev["event_type"] == "comment.deleted":
                        async with db_lock:
                            db.mark_comment_deleted(ev["comment_id"])
                            db.mark_event_done(ev["event_id"])

                    else:
                        async with db_lock:
                            db.mark_event_done(ev["event_id"], error=f"unknown event_type {ev['event_type']}")

                except Exception as e:
                    log.exception("failed processing event %s", ev.get("event_id"))
                    async with db_lock:
                        db.mark_event_done(ev["event_id"], error=str(e))

        except Exception:
            log.exception("event queue loop crashed a tick")

        await asyncio.sleep(QUEUE_POLL_INTERVAL)


async def send_pending_dms():
    """Sends reserved-but-not-yet-sent DMs, respecting the API's rate
    limit and retrying transient failures with backoff. 400s are terminal
    (retrying a malformed request never helps); 500s and network errors
    retry up to MAX_SEND_ATTEMPTS; 429s just reschedule past Retry-After
    without burning an attempt."""
    while True:
        try:
            async with db_lock:
                candidates = db.fetch_sendable(limit=10)

            for row in candidates:
                if not limiter.try_acquire():
                    break  # out of budget this window; try the rest next tick

                result = await mock_client.send_dm(
                    recipient_user_id=row["user_id"],
                    message=row["dm_message"],
                    comment_id=row["comment_id"],
                    idempotency_key=row["idempotency_key"],
                )

                async with db_lock:
                    if result.kind == "accepted":
                        db.mark_dm_accepted(row["rule_id"], row["user_id"], result.dm_id)
                    elif result.kind == "rate_limited":
                        db.mark_dm_retry(
                            row["rule_id"], row["user_id"],
                            time.time() + result.retry_after,
                            "rate_limited by API",
                        )
                    elif result.kind == "invalid":
                        db.mark_dm_failed(row["rule_id"], row["user_id"], f"invalid_request: {result.detail}")
                    else:  # server_error / network_error
                        attempts = row["attempts"] + 1
                        if attempts >= MAX_SEND_ATTEMPTS:
                            db.mark_dm_failed(row["rule_id"], row["user_id"],
                                               f"gave up after {attempts} attempts: {result.detail}")
                        else:
                            db.mark_dm_retry(
                                row["rule_id"], row["user_id"],
                                time.time() + backoff_seconds(attempts),
                                f"{result.kind}: {result.detail}",
                            )

        except Exception:
            log.exception("send loop crashed a tick")

        await asyncio.sleep(SEND_POLL_INTERVAL)


async def reconcile_deliveries():
    """Polls the mock API for DMs we've accepted (202) but haven't seen a
    terminal status for yet, so /stats reflects confirmed delivery rather
    than just "the API said 202". Reads are free (don't count against the
    rate limit) so this can run independently of the send limiter.

    Scope note: this loop *records* late failures (a DM the API accepted
    that later resolves to `failed`) but does not automatically retry
    them -- see FAILURES.md."""
    while True:
        try:
            async with db_lock:
                candidates = db.fetch_reconcilable(RECONCILE_MIN_AGE_SECONDS, limit=20)

            for row in candidates:
                status = await mock_client.get_dm_status(row["dm_id"])
                if status in ("delivered", "failed"):
                    async with db_lock:
                        db.mark_dm_terminal(row["rule_id"], row["user_id"], status,
                                             error=None if status == "delivered" else "reported failed by API")
                # status == "queued" or None (transient error): leave it, we'll check again next tick

        except Exception:
            log.exception("reconcile loop crashed a tick")

        await asyncio.sleep(RECONCILE_POLL_INTERVAL)


def start_background_tasks() -> list[asyncio.Task]:
    return [
        asyncio.create_task(process_event_queue()),
        asyncio.create_task(send_pending_dms()),
        asyncio.create_task(reconcile_deliveries()),
    ]
