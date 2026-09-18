"""Unit tests for ambiguous vs definitive provider classification."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.services.dispatch_service import idempotency_key_for
from app.services.provider_client import HttpProviderClient, ProviderResult
from app.services.unsubscribe_token import UnsubscribeTokenService


@pytest.fixture
def client() -> HttpProviderClient:
    return HttpProviderClient(name="test", base_url="http://provider.test", timeout_ms=500)


def test_success_2xx(client: HttpProviderClient):
    result = client._map_response(httpx.Response(200, json={"messageId": "msg-1"}))
    assert result.outcome == "SUCCESS"
    assert result.provider_message_id == "msg-1"


def test_definitive_invalid_recipient(client: HttpProviderClient):
    result = client._map_response(httpx.Response(422, json={"reason": "invalid_recipient"}))
    assert result.outcome == "DEFINITIVE_RECIPIENT_INVALID"


def test_definitive_provider_reject_429(client: HttpProviderClient):
    result = client._map_response(httpx.Response(429, json={"reason": "provider_rejected"}))
    assert result.outcome == "DEFINITIVE_PROVIDER_FAILURE"


def test_definitive_401(client: HttpProviderClient):
    result = client._map_response(httpx.Response(401, json={"reason": "unauthorized"}))
    assert result.outcome == "DEFINITIVE_PROVIDER_FAILURE"


def test_5xx_no_body_is_ambiguous(client: HttpProviderClient):
    result = client._map_response(httpx.Response(503, content=b""))
    assert result.outcome == "AMBIGUOUS_FAILURE"


def test_malformed_error_body_is_ambiguous(client: HttpProviderClient):
    result = client._map_response(httpx.Response(400, content=b"not-json"))
    assert result.outcome == "AMBIGUOUS_FAILURE"


def test_2xx_unparseable_is_ambiguous(client: HttpProviderClient):
    result = client._map_response(httpx.Response(200, content=b"oops"))
    assert result.outcome == "AMBIGUOUS_FAILURE"


@pytest.mark.asyncio
async def test_timeout_is_ambiguous(client: HttpProviderClient):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    transport = httpx.MockTransport(handler)

    async def send_with_transport() -> ProviderResult:
        url = f"{client.base_url}/send"
        payload = {
            "recipient": "a@b.com",
            "subject": "s",
            "body": "b",
            "idempotencyKey": "key-1",
        }
        try:
            async with httpx.AsyncClient(transport=transport, timeout=client.timeout) as c:
                resp = await c.post(url, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            return ProviderResult(outcome="AMBIGUOUS_FAILURE", error_detail=str(exc))
        return client._map_response(resp)

    result = await send_with_transport()
    assert result.outcome == "AMBIGUOUS_FAILURE"


def test_idempotency_key_excludes_attempt_number():
    nid = uuid4()
    a = idempotency_key_for(nid, "localhost:9101")
    b = idempotency_key_for(nid, "localhost:9101")
    assert a == b
    assert a != idempotency_key_for(nid, "localhost:9102")


def test_unsubscribe_token_roundtrip():
    svc = UnsubscribeTokenService("test-secret")
    uid = uuid4()
    token = svc.sign(uid, "comments", "EMAIL")
    claims = svc.verify(token)
    assert claims is not None
    assert claims.user_id == uid
    assert claims.category == "comments"
    assert claims.channel == "EMAIL"
    assert svc.verify(token + "x") is None


def test_provider_name_from_url():
    from app.services.provider_registry import provider_name_from_url

    assert provider_name_from_url("http://localhost:9101") == "localhost:9101"
    assert provider_name_from_url("http://localhost:9102/") == "localhost:9102"
