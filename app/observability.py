from __future__ import annotations

from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

DISPATCH_OUTCOMES = Counter(
    "notifyhub_dispatch_outcomes_total",
    "Dispatch outcomes by classification, channel, and provider",
    ["outcome", "channel", "provider"],
)

DIGEST_WINDOWS = Gauge(
    "notifyhub_digest_windows",
    "Digest windows by status",
    ["status"],
)

SUPPRESSIONS = Counter(
    "notifyhub_suppressions_total",
    "Suppressions recorded by reason",
    ["reason"],
)


def record_dispatch_success(channel: str, provider: str) -> None:
    DISPATCH_OUTCOMES.labels(outcome="success", channel=channel, provider=provider).inc()


def record_dispatch_ambiguous(channel: str, provider: str) -> None:
    DISPATCH_OUTCOMES.labels(outcome="ambiguous_retry", channel=channel, provider=provider).inc()


def record_dispatch_failover(channel: str, provider: str) -> None:
    DISPATCH_OUTCOMES.labels(outcome="failover", channel=channel, provider=provider).inc()


def record_dispatch_definitive_stop(channel: str, provider: str) -> None:
    DISPATCH_OUTCOMES.labels(outcome="definitive_stop", channel=channel, provider=provider).inc()


def record_suppression(reason: str) -> None:
    SUPPRESSIONS.labels(reason=reason).inc()


def metrics_response() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
