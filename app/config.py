import os

# Your PseudoGram API key. Required. Same key you got from /v1/keygen.
# It is used both as the Bearer/X-API-Key for outbound calls AND as the
# HMAC secret for verifying inbound webhook signatures (per the spec).
API_KEY = os.environ.get("PSEUDOGRAM_API_KEY", "")

BASE_URL = os.environ.get("PSEUDOGRAM_BASE_URL", "https://pseudogram-api.onrender.com")

DB_PATH = os.environ.get("DB_PATH", "linkplease.db")

# Rate limit imposed by the mock API on POST /v1/dm/send
SEND_RATE_LIMIT = 10
SEND_RATE_WINDOW_SECONDS = 60

# Retry policy for send failures (429 handled separately via Retry-After)
MAX_SEND_ATTEMPTS = 6
BACKOFF_BASE_SECONDS = 2  # attempt 1 -> 2s, attempt 2 -> 4s, attempt 3 -> 8s ...
BACKOFF_MAX_SECONDS = 60

# How often the background loops tick
QUEUE_POLL_INTERVAL = 0.5
SEND_POLL_INTERVAL = 0.5
RECONCILE_POLL_INTERVAL = 2.0

# How long to wait after a DM is accepted (202) before checking its status
RECONCILE_MIN_AGE_SECONDS = 2.0

# Fail hard on startup if this isn't set, rather than silently accepting
# every forged webhook (a missing key would make signature checks meaningless).
if not API_KEY:
    # Don't raise at import time in test contexts; main.py checks this
    # explicitly on startup and refuses to serve traffic without it.
    pass
