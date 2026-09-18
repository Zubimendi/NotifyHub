"""Generic mock notification provider — one process, many Docker services.

SIMULATE_MODE controls failure classification scenarios used by NotifyHub tests.
"""

from __future__ import annotations

import os
import time
import uuid
from threading import Lock

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

app = FastAPI(title="NotifyHub Mock Provider")

PROVIDER_NAME = os.getenv("PROVIDER_NAME", "mock-provider")
SIMULATE_MODE = os.getenv("SIMULATE_MODE", "NORMAL").upper()
# Sleep long enough that NotifyHub's PROVIDER_TIMEOUT_MS expires first.
AMBIGUOUS_SLEEP_SECONDS = float(os.getenv("AMBIGUOUS_SLEEP_SECONDS", "10"))

_lock = Lock()
# idempotencyKey -> messageId (first successful "send")
_idempotency: dict[str, str] = {}
# idempotencyKey -> how many distinct sends were recorded (0 or 1)
_distinct_sends: dict[str, int] = {}
# Total HTTP hits per key (including retries)
_request_counts: dict[str, int] = {}


class SendRequest(BaseModel):
    recipient: str
    subject: str = ""
    body: str
    idempotencyKey: str = Field(alias="idempotencyKey")

    model_config = {"populate_by_name": True}


def _record_send(idempotency_key: str) -> str:
    """Record a logical send once per key; return stable messageId."""
    with _lock:
        _request_counts[idempotency_key] = _request_counts.get(idempotency_key, 0) + 1
        if idempotency_key in _idempotency:
            return _idempotency[idempotency_key]
        message_id = str(uuid.uuid4())
        _idempotency[idempotency_key] = message_id
        _distinct_sends[idempotency_key] = 1
        return message_id


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "provider": PROVIDER_NAME, "mode": SIMULATE_MODE}


@app.post("/send")
async def send(req: SendRequest):
    if SIMULATE_MODE == "DEFINITIVE_REJECT":
        with _lock:
            _request_counts[req.idempotencyKey] = _request_counts.get(req.idempotencyKey, 0) + 1
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=429, content={"reason": "provider_rejected"})

    if SIMULATE_MODE == "DEFINITIVE_INVALID_RECIPIENT":
        with _lock:
            _request_counts[req.idempotencyKey] = _request_counts.get(req.idempotencyKey, 0) + 1
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"reason": "invalid_recipient"})

    if SIMULATE_MODE == "AMBIGUOUS_TIMEOUT":
        # Complete the send on the server side, then sleep so the caller
        # times out and never learns the outcome from this response.
        _record_send(req.idempotencyKey)
        time.sleep(AMBIGUOUS_SLEEP_SECONDS)
        return {"messageId": _idempotency[req.idempotencyKey]}

    # NORMAL (default)
    message_id = _record_send(req.idempotencyKey)
    return {"messageId": message_id}


@app.get("/debug/send-count")
async def send_count(idempotencyKey: str = Query(...)) -> dict:
    with _lock:
        return {
            "idempotencyKey": idempotencyKey,
            "distinctSends": _distinct_sends.get(idempotencyKey, 0),
            "requestCount": _request_counts.get(idempotencyKey, 0),
            "messageId": _idempotency.get(idempotencyKey),
            "provider": PROVIDER_NAME,
        }


@app.post("/debug/reset")
async def reset() -> dict[str, str]:
    with _lock:
        _idempotency.clear()
        _distinct_sends.clear()
        _request_counts.clear()
    return {"status": "reset"}
