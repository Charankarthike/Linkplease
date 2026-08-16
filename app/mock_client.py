"""Thin wrapper around the PseudoGram mock API's DM endpoints."""
import httpx

from app.config import API_KEY, BASE_URL

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"X-API-Key": API_KEY},
            timeout=10.0,
        )
    return _client


async def close_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class SendResult:
    def __init__(self, kind: str, dm_id: str = None, retry_after: float = None, detail: str = None):
        self.kind = kind  # "accepted" | "rate_limited" | "server_error" | "invalid" | "network_error"
        self.dm_id = dm_id
        self.retry_after = retry_after
        self.detail = detail


async def send_dm(recipient_user_id: str, message: str, comment_id: str, idempotency_key: str) -> SendResult:
    client = get_client()
    try:
        resp = await client.post(
            "/v1/dm/send",
            json={
                "recipient_user_id": recipient_user_id,
                "message": message,
                "comment_id": comment_id,
            },
            headers={"Idempotency-Key": idempotency_key},
        )
    except httpx.RequestError as e:
        return SendResult("network_error", detail=str(e))

    if resp.status_code == 202:
        body = resp.json()
        return SendResult("accepted", dm_id=body["dm_id"])
    if resp.status_code == 429:
        retry_after = float(resp.headers.get("Retry-After", "5"))
        return SendResult("rate_limited", retry_after=retry_after)
    if resp.status_code == 500:
        return SendResult("server_error", detail=resp.text)
    if resp.status_code == 400:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = resp.text
        return SendResult("invalid", detail=detail)
    return SendResult("server_error", detail=f"unexpected status {resp.status_code}: {resp.text}")


async def get_dm_status(dm_id: str) -> str | None:
    """Returns 'queued' | 'delivered' | 'failed', or None on transient error
    (caller should just try again next tick -- reads don't cost rate limit)."""
    client = get_client()
    try:
        resp = await client.get(f"/v1/dm/{dm_id}")
    except httpx.RequestError:
        return None
    if resp.status_code != 200:
        return None
    return resp.json().get("status")
