"""Integration tests — require `make up` (Postgres, Redis, mock providers)."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.models import DigestWindow, Notification, NotificationEvent, NotificationSendAttempt, Suppression, User
from app.services.digest_service import DigestService
from app.services.dispatch_service import DispatchService, idempotency_key_for
from app.services.preference_service import PreferenceService, SuppressionService
from app.services.provider_client import HttpProviderClient
from app.services.provider_registry import ProviderRegistry, provider_name_from_url
from app.services.queueline_client import QueueLineClient

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://notifyhub:notifyhub@localhost:5432/notifyhub",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
MOCK_PRIMARY = os.getenv("MOCK_EMAIL_PRIMARY", "http://localhost:9101")
MOCK_FALLBACK = os.getenv("MOCK_EMAIL_FALLBACK", "http://localhost:9102")


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: needs Docker services")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        database_url=DATABASE_URL,
        redis_url=REDIS_URL,
        email_provider_chain=f"{MOCK_PRIMARY},{MOCK_FALLBACK}",
        provider_timeout_ms=2000,
        max_same_provider_retries=3,
        queueline_base_url=os.getenv("QUEUELINE_BASE_URL", "http://localhost:8080"),
    )


@pytest_asyncio.fixture
async def session(settings: Settings):
    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        yield s
        await s.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def redis_client(settings: Settings):
    r = redis.from_url(settings.redis_url, decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def user(session: AsyncSession) -> User:
    u = User(email=f"test-{uuid.uuid4().hex[:8]}@example.com", phone="+15550001111", push_token="tok")
    session.add(u)
    await session.flush()
    return u


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mock_provider_reachable():
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{MOCK_PRIMARY}/health")
        assert r.status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ambiguous_timeout_classification(settings: Settings):
    """Against AMBIGUOUS_TIMEOUT mode the client must see AMBIGUOUS_FAILURE.

    Reconfigure is env-based on the container; here we simulate by pointing
    at a URL that won't respond in time, OR use the primary if already in
    AMBIGUOUS_TIMEOUT. Default compose is NORMAL — we assert timeout path
    via a non-routable / blocked port.
    """
    client = HttpProviderClient(
        name="slow",
        base_url="http://127.0.0.1:59999",  # nothing listening
        timeout_ms=300,
    )
    result = await client.send("a@b.com", "s", "b", "key-timeout")
    assert result.outcome == "AMBIGUOUS_FAILURE"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_normal_send_and_idempotent_debug_count():
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(f"{MOCK_PRIMARY}/debug/reset")
        key = f"idem-{uuid.uuid4()}"
        r1 = await client.post(
            f"{MOCK_PRIMARY}/send",
            json={"recipient": "a@b.com", "subject": "s", "body": "b", "idempotencyKey": key},
        )
        assert r1.status_code == 200
        mid = r1.json()["messageId"]
        r2 = await client.post(
            f"{MOCK_PRIMARY}/send",
            json={"recipient": "a@b.com", "subject": "s", "body": "b", "idempotencyKey": key},
        )
        assert r2.json()["messageId"] == mid
        dbg = await client.get(f"{MOCK_PRIMARY}/debug/send-count", params={"idempotencyKey": key})
        assert dbg.json()["distinctSends"] == 1
        assert dbg.json()["requestCount"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_digest_window_concurrency(session: AsyncSession, redis_client, user: User, settings: Settings):
    await redis_client.flushdb()
    digest = DigestService(session, redis_client, settings, queueline=None)

    async def create_once():
        return await digest.get_or_create_open_window(user.id, "comments", "EMAIL", timedelta(minutes=60))

    ids = await asyncio.gather(*[create_once() for _ in range(20)])
    assert len(set(ids)) == 1
    result = await session.execute(
        select(DigestWindow).where(
            DigestWindow.user_id == user.id,
            DigestWindow.category == "comments",
            DigestWindow.channel == "EMAIL",
            DigestWindow.status == "OPEN",
        )
    )
    assert len(result.scalars().all()) == 1
    await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_flush_claim_once(session: AsyncSession, redis_client, user: User, settings: Settings):
    await redis_client.flushdb()
    digest = DigestService(session, redis_client, settings, queueline=None)
    window_id = await digest.get_or_create_open_window(user.id, "alerts", "EMAIL", timedelta(seconds=1))
    # Force due
    await session.execute(
        text("UPDATE digest_windows SET flush_at = now() - interval '1 second' WHERE id = :id"),
        {"id": window_id},
    )
    await session.flush()

    claimed = []
    for _ in range(2):
        claim = await session.execute(
            text(
                """
                UPDATE digest_windows
                SET status = 'FLUSHING', updated_at = now()
                WHERE id = :id AND status = 'OPEN'
                RETURNING id
                """
            ),
            {"id": window_id},
        )
        claimed.append(claim.first() is not None)
    assert claimed.count(True) == 1
    assert claimed.count(False) == 1
    await session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stuck_flushing_recovery(session: AsyncSession, redis_client, user: User, settings: Settings):
    settings.flushing_stuck_timeout_seconds = 0
    digest = DigestService(session, redis_client, settings, queueline=None)
    window_id = await digest.get_or_create_open_window(user.id, "alerts", "SMS", timedelta(minutes=60))
    await session.execute(
        text(
            """
            UPDATE digest_windows
            SET status = 'FLUSHING', updated_at = now() - interval '1 hour'
            WHERE id = :id
            """
        ),
        {"id": window_id},
    )
    await session.flush()
    recovered = await digest.recover_stuck_flushing()
    assert recovered >= 1
    row = await session.get(DigestWindow, window_id)
    assert row is not None
    assert row.status == "OPEN"
    await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unsubscribe_clears_only_matching(
    session: AsyncSession, user: User
):
    prefs = PreferenceService(session)
    suppressions = SuppressionService(session)

    await prefs.set(user.id, "comments", "EMAIL", enabled=False)
    await suppressions.record_unsubscribe(user.id, "EMAIL", "comments")
    session.add(
        Suppression(
            user_id=user.id,
            channel="EMAIL",
            reason="MANUAL",
            category="alerts",
        )
    )
    await session.flush()

    await prefs.set(user.id, "comments", "EMAIL", enabled=True)
    await session.flush()

    result = await session.execute(
        select(Suppression).where(Suppression.user_id == user.id)
    )
    rows = list(result.scalars().all())
    unsub = next(r for r in rows if r.reason == "UNSUBSCRIBE")
    manual = next(r for r in rows if r.reason == "MANUAL")
    assert unsub.cleared_at is not None
    assert manual.cleared_at is None
    await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hard_bounce_not_cleared_by_preference(session: AsyncSession, user: User):
    prefs = PreferenceService(session)
    suppressions = SuppressionService(session)
    await suppressions.record_bounce(
        "mock", f"evt-{uuid.uuid4()}", user.id, "EMAIL", "HARD_BOUNCE"
    )
    await prefs.set(user.id, "comments", "EMAIL", enabled=False)
    await prefs.set(user.id, "comments", "EMAIL", enabled=True)
    await session.flush()
    result = await session.execute(
        select(Suppression).where(
            Suppression.user_id == user.id,
            Suppression.reason == "HARD_BOUNCE",
        )
    )
    row = result.scalar_one()
    assert row.cleared_at is None
    await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotent_bounce_webhook(session: AsyncSession, user: User):
    svc = SuppressionService(session)
    eid = f"bounce-{uuid.uuid4()}"
    assert await svc.record_bounce("mock", eid, user.id, "EMAIL", "HARD_BOUNCE") is True
    assert await svc.record_bounce("mock", eid, user.id, "EMAIL", "HARD_BOUNCE") is False
    result = await session.execute(
        select(Suppression).where(Suppression.source_event_id == eid)
    )
    assert len(result.scalars().all()) == 1
    await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_freshness_suppression(
    session: AsyncSession, user: User, settings: Settings
):
    """Immediate notification suppressed at dispatch if bounce added after create."""
    from app.db.models import Notification

    n = Notification(
        user_id=user.id,
        channel="EMAIL",
        category="comments",
        kind="IMMEDIATE",
        recipient_address=user.email or "x@y.com",
        rendered_subject="s",
        rendered_body="b",
        status="PENDING",
    )
    session.add(n)
    await session.flush()

    await SuppressionService(session).record_bounce(
        "mock", f"evt-{uuid.uuid4()}", user.id, "EMAIL", "HARD_BOUNCE"
    )
    await session.flush()

    # Fake queueline that no-ops
    class FakeQL(QueueLineClient):
        def __init__(self):
            super().__init__("http://localhost:9")

        async def enqueue(self, *a, **k):
            from app.services.queueline_client import Job

            return Job(id="x", queue="q", payload={})

    providers = ProviderRegistry(settings)
    first = providers.for_channel("EMAIL").first()
    assert first is not None
    dispatch = DispatchService(session, FakeQL(), settings, providers)
    outcome = await dispatch.dispatch(n.id, first.name)
    await session.flush()
    assert outcome.action == "complete"
    refreshed = await session.get(Notification, n.id)
    assert refreshed is not None
    assert refreshed.status == "SUPPRESSED"
    await session.commit()
