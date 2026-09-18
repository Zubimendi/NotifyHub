from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import DigestWindow, Notification, NotificationEvent, User
from app.services.preference_service import PreferenceService, SuppressionService
from app.services.provider_registry import ProviderRegistry
from app.services.queueline_client import QueueLineClient
from app.services.template_service import TemplateService


def digest_redis_key(user_id: uuid.UUID, category: str, channel: str) -> str:
    return f"digest:{user_id}:{category}:{channel}"


class DigestService:
    def __init__(
        self,
        session: AsyncSession,
        redis_client: redis.Redis,
        settings: Settings | None = None,
        queueline: QueueLineClient | None = None,
        providers: ProviderRegistry | None = None,
    ) -> None:
        self._session = session
        self._redis = redis_client
        self._settings = settings or get_settings()
        self._queueline = queueline
        self._providers = providers or ProviderRegistry(self._settings)

    async def get_or_create_open_window(
        self,
        user_id: uuid.UUID,
        category: str,
        channel: str,
        window_duration: timedelta | None = None,
    ) -> uuid.UUID:
        duration = window_duration or timedelta(minutes=self._settings.default_digest_window_minutes)
        key = digest_redis_key(user_id, category, channel)

        cached = await self._redis.get(key)
        if cached:
            return uuid.UUID(cached if isinstance(cached, str) else cached.decode())

        ttl_seconds = int(duration.total_seconds())
        result = await self._session.execute(
            text(
                """
                INSERT INTO digest_windows (user_id, category, channel, flush_at, status)
                VALUES (:user_id, :category, :channel, now() + make_interval(secs => :secs), 'OPEN')
                ON CONFLICT (user_id, category, channel) WHERE status = 'OPEN'
                DO NOTHING
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "category": category,
                "channel": channel,
                "secs": ttl_seconds,
            },
        )
        row = result.first()
        if row is not None:
            window_id = row[0]
            await self._redis.set(key, str(window_id), ex=ttl_seconds)
            await self._session.flush()
            return window_id

        # Lost the race — load the winner's OPEN window
        existing = await self._session.execute(
            select(DigestWindow.id).where(
                DigestWindow.user_id == user_id,
                DigestWindow.category == category,
                DigestWindow.channel == channel,
                DigestWindow.status == "OPEN",
            )
        )
        window_id = existing.scalar_one()
        await self._redis.set(key, str(window_id), ex=ttl_seconds)
        return window_id

    async def flush_due_windows(self) -> int:
        """Primary sweep: claim OPEN windows past flush_at and flush them."""
        result = await self._session.execute(
            select(DigestWindow.id).where(
                DigestWindow.status == "OPEN",
                DigestWindow.flush_at <= datetime.now(timezone.utc),
            )
        )
        ids = list(result.scalars().all())
        flushed = 0
        for window_id in ids:
            if await self._claim_and_flush(window_id):
                flushed += 1
        return flushed

    async def recover_stuck_flushing(self) -> int:
        """Crash-recovery backstop: reset stuck FLUSHING rows to OPEN.

        Distinct from the primary flush trigger — only recovers windows left
        FLUSHING after a crashed worker (docs/CURSOR_CONTEXT.md §4).
        """
        timeout = self._settings.flushing_stuck_timeout_seconds
        result = await self._session.execute(
            text(
                """
                UPDATE digest_windows
                SET status = 'OPEN',
                    flush_at = now(),
                    updated_at = now()
                WHERE status = 'FLUSHING'
                  AND updated_at < now() - make_interval(secs => :timeout)
                RETURNING id
                """
            ),
            {"timeout": timeout},
        )
        recovered = len(result.fetchall())
        await self._session.flush()
        return recovered

    async def _claim_and_flush(self, window_id: uuid.UUID) -> bool:
        claim = await self._session.execute(
            text(
                """
                UPDATE digest_windows
                SET status = 'FLUSHING', updated_at = now()
                WHERE id = :id AND status = 'OPEN'
                RETURNING id, user_id, category, channel
                """
            ),
            {"id": window_id},
        )
        claimed = claim.first()
        if claimed is None:
            return False

        _, user_id, category, channel = claimed
        key = digest_redis_key(user_id, category, channel)
        await self._redis.delete(key)

        events_result = await self._session.execute(
            select(NotificationEvent).where(NotificationEvent.digest_window_id == window_id)
        )
        events = list(events_result.scalars().all())

        prefs = PreferenceService(self._session)
        suppressions = SuppressionService(self._session)
        pref = await prefs.get(user_id, category, channel)
        if (not pref.enabled) or await suppressions.is_suppressed(user_id, channel, category):
            for ev in events:
                ev.status = "SUPPRESSED"
            await self._session.execute(
                update(DigestWindow)
                .where(DigestWindow.id == window_id)
                .values(status="FLUSHED", notification_id=None, updated_at=datetime.now(timezone.utc))
            )
            await self._session.flush()
            return True

        templates = TemplateService(self._session)
        subject, body = await templates.render_digest(category, channel, events)

        user = await self._session.get(User, user_id)
        recipient = self._recipient_for(user, channel)

        notification = Notification(
            user_id=user_id,
            channel=channel,
            category=category,
            kind="DIGEST",
            digest_window_id=window_id,
            recipient_address=recipient,
            rendered_subject=subject,
            rendered_body=body,
            status="PENDING",
        )
        self._session.add(notification)
        await self._session.flush()

        for ev in events:
            ev.status = "BATCHED"
            ev.notification_id = notification.id

        await self._session.execute(
            update(DigestWindow)
            .where(DigestWindow.id == window_id)
            .values(
                status="FLUSHED",
                notification_id=notification.id,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await self._session.flush()

        chain = self._providers.for_channel(channel)
        first = chain.first()
        if first is None:
            notification.status = "FAILED"
            await self._session.flush()
            return True

        if self._queueline is not None:
            job = await self._queueline.enqueue(
                self._settings.queueline_dispatch_queue,
                {"notificationId": str(notification.id), "providerName": first.name},
                max_attempts=self._settings.max_same_provider_retries,
            )
            # Store job id on a placeholder attempt row is optional; dispatch creates attempts.
            _ = job.id

        return True

    @staticmethod
    def _recipient_for(user: User | None, channel: str) -> str:
        if user is None:
            return "unknown"
        if channel == "EMAIL":
            return user.email or "unknown@example.com"
        if channel == "SMS":
            return user.phone or "unknown"
        if channel == "PUSH":
            return user.push_token or "unknown"
        return "unknown"
