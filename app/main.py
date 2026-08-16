import hashlib
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

from app import db, worker, mock_client
from app.config import API_KEY

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("linkplease")

_background_tasks = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not API_KEY:
        log.warning(
            "PSEUDOGRAM_API_KEY is not set -- outbound sends will fail and "
            "webhook signature verification will reject everything."
        )
    db.init_db()
    _background_tasks.extend(worker.start_background_tasks())
    log.info("background loops started")
    yield
    for t in _background_tasks:
        t.cancel()
    await mock_client.close_client()


app = FastAPI(title="LinkPlease webhook automation", lifespan=lifespan)


# ---------- POST /webhook ----------

def _verify_signature(raw_body: bytes, header_value: str | None) -> bool:
    if not header_value or not header_value.startswith("sha256="):
        return False
    given = header_value.split("=", 1)[1]
    expected = hmac.new(API_KEY.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(given, expected)


@app.post("/webhook")
async def webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-PseudoGram-Signature")

    if not _verify_signature(raw_body, signature):
        # Reject forged/unsigned requests. Deliberately not a 200 --
        # these never entered the queue and should not be treated as
        # "received".
        raise HTTPException(status_code=401, detail="invalid or missing signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    event_id = payload.get("event_id")
    event_type = payload.get("event_type")
    data = payload.get("data") or {}
    if not event_id or not event_type:
        raise HTTPException(status_code=400, detail="missing event_id or event_type")

    # This is the only synchronous work: a single indexed INSERT OR IGNORE.
    # Everything else -- rule matching, sending, retries, reconciliation --
    # happens in the background loops so we always return well under 5s,
    # even under a 500-events/10s burst.
    async with worker.db_lock:
        db.enqueue_event(event_id, event_type, data)

    return {"status": "ok"}


# ---------- POST /rules ----------

class RuleIn(BaseModel):
    keyword: str
    dm_message: str


@app.post("/rules", status_code=201)
async def create_rule(rule: RuleIn):
    if not rule.keyword.strip():
        raise HTTPException(status_code=400, detail="keyword must not be empty")
    async with worker.db_lock:
        created = db.create_rule(rule.keyword, rule.dm_message)
    return created


@app.get("/rules")
async def list_rules():
    return db.get_rules()


# ---------- GET /stats ----------

@app.get("/stats")
async def stats():
    return db.get_stats()


@app.get("/health")
async def health():
    return {"status": "ok"}
