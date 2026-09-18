"""Stateless HMAC-signed unsubscribe tokens.

Carries {userId, category, channel}. No expiry — an old email's unsubscribe
link must still work. Verified with hmac.compare_digest; no DB lookup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class UnsubscribeClaims:
    user_id: UUID
    category: str
    channel: str


class UnsubscribeTokenService:
    def __init__(self, secret: str) -> None:
        self._secret = secret.encode("utf-8")

    def sign(self, user_id: UUID, category: str, channel: str) -> str:
        payload = {
            "userId": str(user_id),
            "category": category,
            "channel": channel,
        }
        body = urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        sig = self._sign(body)
        return f"{body}.{sig}"

    def verify(self, token: str) -> UnsubscribeClaims | None:
        try:
            body, sig = token.rsplit(".", 1)
        except ValueError:
            return None
        expected = self._sign(body)
        if not hmac.compare_digest(expected, sig):
            return None
        pad = "=" * (-len(body) % 4)
        try:
            data = json.loads(urlsafe_b64decode(body + pad))
            return UnsubscribeClaims(
                user_id=UUID(data["userId"]),
                category=data["category"],
                channel=data["channel"],
            )
        except (KeyError, ValueError, json.JSONDecodeError):
            return None

    def _sign(self, body: str) -> str:
        digest = hmac.new(self._secret, body.encode(), hashlib.sha256).digest()
        return urlsafe_b64encode(digest).decode().rstrip("=")
