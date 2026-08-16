# LinkPlease webhook automation

Part A + Part B of the assignment: rule-based auto-DM on `PRICE`-style
comment keywords, on top of the PseudoGram mock Instagram API, with
webhook signature verification and live `/stats`.

## How it's built

- **FastAPI** app with exactly the three contract endpoints
  (`POST /webhook`, `POST /rules`, `GET /stats`), plus `GET /rules` and
  `GET /health` for convenience.
- **SQLite (WAL mode)** is the only source of truth. There is no
  in-memory queue or in-memory retry timer anywhere — every event,
  reservation, and retry schedule is a row on disk, so a process restart
  loses nothing that was already durably queued.
- **Three background loops** (`app/worker.py`), all reading/writing that
  same DB:
  1. `process_event_queue` — matches `comment.created` text against
     rules (case-insensitive substring) and reserves a `(rule_id,
     user_id)` slot; handles `comment.deleted` by cancelling any
     not-yet-sent reservation.
  2. `send_pending_dms` — sends reserved DMs via `POST /v1/dm/send`,
     respecting the 10-req/60s limit with a sliding-window limiter,
     retrying `500`s/network errors with exponential backoff + jitter,
     honoring `Retry-After` on `429`, and giving up (marking `failed`)
     on `400` or after 6 attempts.
  3. `reconcile_deliveries` — polls `GET /v1/dm/{id}` for anything still
     `queued` so `/stats.sent` reflects *confirmed* delivery, not just
     "the API said 202."
- **Dedup**: `event_queue` is keyed by `event_id` (absorbs the ~8%
  redelivery case for free). `dm_dedup` is keyed by `(rule_id,
  user_id)` — that primary key *is* the "never DM the same user twice
  for the same rule" guarantee, enforced atomically by SQLite, not by
  application-level locking.
- **Webhook signature**: HMAC-SHA256 of the raw request body using the
  PseudoGram API key as the secret, compared with `hmac.compare_digest`.
  A bad/missing signature gets `401` before anything touches the DB.

See `FAILURES.md` for what's *not* handled.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Get a PseudoGram API key

```bash
curl -X POST https://pseudogram-api.onrender.com/v1/apply \
  -H "Content-Type: application/json" \
  -d '{"name": "...", "email": "you@example.com", "phone": "+91...", "linkedin_url": "https://linkedin.com/in/you"}'

curl -X POST https://pseudogram-api.onrender.com/v1/keygen \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'
```

Copy the returned `api_key`.

### Run locally

```bash
export PSEUDOGRAM_API_KEY=<your key>
uvicorn app.main:app --reload --port 8000
```

Then point the mock API's simulator at your local tunnel (e.g. via
`ngrok http 8000`) or your deployed URL:

```bash
curl -X POST https://pseudogram-api.onrender.com/v1/simulate/start \
  -H "X-API-Key: $PSEUDOGRAM_API_KEY" -H "Content-Type: application/json" \
  -d '{"webhook_url": "https://<your-url>/webhook", "count": 500, "duration_seconds": 10}'
```

Compare `GET /stats` against `GET /v1/simulate/{run_id}/truth` afterward.

## Deploy (Render)

`render.yaml` is included. Two things to know before you rely on it:

- **The free Render plan doesn't support persistent disks** — the
  `disk:` block in `render.yaml` needs a paid instance type to actually
  mount. On the free plan, `linkplease.db` lives on ephemeral storage
  and a redeploy/restart wipes it (rules and dedup history reset, but
  nothing *mid-flight* is lost any worse than described in
  `FAILURES.md`, since durability-across-a-single-restart was the goal,
  not durability-across-storage-wipes).
- Set `PSEUDOGRAM_API_KEY` in the Render dashboard's environment
  variables (marked `sync: false` in `render.yaml` so it's not
  committed).

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `PSEUDOGRAM_API_KEY` | *(required)* | Outbound auth + webhook signature secret |
| `PSEUDOGRAM_BASE_URL` | `https://pseudogram-api.onrender.com` | Mock API base |
| `DB_PATH` | `linkplease.db` | SQLite file location |
