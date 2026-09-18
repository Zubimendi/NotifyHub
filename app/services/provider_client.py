from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

import httpx

Outcome = Literal[
    "SUCCESS",
    "AMBIGUOUS_FAILURE",
    "DEFINITIVE_PROVIDER_FAILURE",
    "DEFINITIVE_RECIPIENT_INVALID",
]


@dataclass(frozen=True)
class ProviderResult:
    outcome: Outcome
    provider_message_id: str | None = None
    error_detail: str | None = None


class NotificationProvider(ABC):
    name: str

    @abstractmethod
    async def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        idempotency_key: str,
    ) -> ProviderResult:
        ...


class HttpProviderClient(NotificationProvider):
    """POSTs to a mock/real provider base URL and maps HTTP outcomes.

    Ambiguous vs definitive classification is the centerpiece of NotifyHub —
    when uncertain, treat as AMBIGUOUS_FAILURE (safer: same-provider retry
    with a stable idempotency key rather than premature failover).
    """

    def __init__(self, name: str, base_url: str, timeout_ms: int) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_ms / 1000.0

    async def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        idempotency_key: str,
    ) -> ProviderResult:
        url = f"{self.base_url}/send"
        payload = {
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "idempotencyKey": idempotency_key,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            return ProviderResult(
                outcome="AMBIGUOUS_FAILURE",
                error_detail=f"{type(exc).__name__}: {exc}",
            )
        except httpx.HTTPError as exc:
            return ProviderResult(
                outcome="AMBIGUOUS_FAILURE",
                error_detail=f"{type(exc).__name__}: {exc}",
            )

        return self._map_response(resp)

    def _map_response(self, resp: httpx.Response) -> ProviderResult:
        if 200 <= resp.status_code < 300:
            try:
                data = resp.json()
            except ValueError:
                return ProviderResult(
                    outcome="AMBIGUOUS_FAILURE",
                    error_detail="2xx with unparseable body",
                )
            message_id = None
            if isinstance(data, dict):
                message_id = data.get("messageId") or data.get("message_id")
            return ProviderResult(outcome="SUCCESS", provider_message_id=message_id)

        body: Any = None
        reason: str | None = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                reason = body.get("reason")
        except ValueError:
            body = None

        # Unclear 5xx or unparseable error → ambiguous
        if resp.status_code >= 500:
            if reason is None and body is None:
                return ProviderResult(
                    outcome="AMBIGUOUS_FAILURE",
                    error_detail=f"HTTP {resp.status_code} with no parseable body",
                )
            # Explicit provider rejection in a 5xx body is still definitive-provider
            if reason == "provider_rejected":
                return ProviderResult(
                    outcome="DEFINITIVE_PROVIDER_FAILURE",
                    error_detail=f"HTTP {resp.status_code}: {reason}",
                )
            return ProviderResult(
                outcome="AMBIGUOUS_FAILURE",
                error_detail=f"HTTP {resp.status_code}: unclear error body",
            )

        if resp.status_code in (400, 422) and reason == "invalid_recipient":
            return ProviderResult(
                outcome="DEFINITIVE_RECIPIENT_INVALID",
                error_detail=f"HTTP {resp.status_code}: {reason}",
            )

        if reason == "provider_rejected" or resp.status_code in (401, 403, 429):
            return ProviderResult(
                outcome="DEFINITIVE_PROVIDER_FAILURE",
                error_detail=f"HTTP {resp.status_code}: {reason or 'provider_rejected'}",
            )

        # Malformed / unclear rejection → ambiguous (safer)
        return ProviderResult(
            outcome="AMBIGUOUS_FAILURE",
            error_detail=f"HTTP {resp.status_code}: unclear or malformed error body",
        )
