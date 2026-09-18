from __future__ import annotations

from urllib.parse import urlparse

from app.config import Settings, get_settings
from app.services.provider_client import HttpProviderClient, NotificationProvider


def provider_name_from_url(base_url: str) -> str:
    """Stable name derived from the configured base URL (host:port)."""
    parsed = urlparse(base_url if "://" in base_url else f"http://{base_url}")
    host = parsed.hostname or "provider"
    if parsed.port:
        return f"{host}:{parsed.port}"
    return host


class ProviderChain:
    def __init__(self, providers: list[NotificationProvider]) -> None:
        self._providers = providers

    def __len__(self) -> int:
        return len(self._providers)

    def first(self) -> NotificationProvider | None:
        return self._providers[0] if self._providers else None

    def get(self, name: str) -> NotificationProvider | None:
        for p in self._providers:
            if p.name == name:
                return p
        return None

    def next_after(self, name: str) -> NotificationProvider | None:
        for i, p in enumerate(self._providers):
            if p.name == name:
                if i + 1 < len(self._providers):
                    return self._providers[i + 1]
                return None
        return None

    def all(self) -> list[NotificationProvider]:
        return list(self._providers)


class ProviderRegistry:
    """Configuration-driven provider chains — no hardcoded provider names."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._chains: dict[str, ProviderChain] = {}
        for channel in ("EMAIL", "SMS", "PUSH"):
            clients: list[NotificationProvider] = []
            for url in self._settings.chain_urls(channel):
                name = provider_name_from_url(url)
                clients.append(
                    HttpProviderClient(
                        name=name,
                        base_url=url,
                        timeout_ms=self._settings.provider_timeout_ms,
                    )
                )
            self._chains[channel] = ProviderChain(clients)

    def for_channel(self, channel: str) -> ProviderChain:
        return self._chains.get(channel.upper(), ProviderChain([]))
