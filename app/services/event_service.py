from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import redis.asyncio as redis

from app.config import Settings, get_settings
from app.db.models import Notification, NotificationEvent, User
from app.observability import record_suppression
from app.services.digest_service import DigestService
from app.services.preference_service import PreferenceService, SuppressionService
from app.services.provider_registry import ProviderRegistry
from app.services.queueline_client import QueueLineClient
from app.services.template_service import TemplateService


class EventService:
    def __init__(
        self,
        session: AsyncSession,
        redis_client: redis.Redis,
        queueline: QueueLineClient,
        settings: Settings | None = None,
        providers: ProviderRegistry | None = None,
    ) -> None:
        self._session = session
        self._redis = redis_client
        self._queueline = queueline
        self._settings = settings or get_settings()
        self._providers = providers or ProviderRegistry(self._settings)

    async def ingest(
        self,
        user_id: uuid.UUID,
        category: str,
        channel: str,
        template_data: dict,
    ) -> dict:
        channel = channel.upper()
        prefs = PreferenceService(self._session)
        suppressions = SuppressionService(self._session)
        pref = await prefs.get(user_id, category, channel)

        event = NotificationEvent(
            user_id=user_id,
            category=category,
            channel=channel,
            template_data=template_data,
            status="PENDING",
        )
        self._session.add(event)
        await self._session.flush()

        if (not pref.enabled) or await suppressions.is_suppressed(user_id, channel, category):
            event.status = "SUPPRESSED"
            await self._session.flush()
            record_suppression("ingestion")
            return {
                "event_id": event.id,
                "status": "SUPPRESSED",
                "notification_id": None,
                "digest_window_id": None,
            }

        if pref.batching_mode.startswith("DIGEST"):
            digest = DigestService(
                self._session,
                self._redis,
                self._settings,
                queueline=self._queueline,
                providers=self._providers,
            )
            window_id = await digest.get_or_create_open_window(user_id, category, channel)
            event.digest_window_id = window_id
            event.status = "PENDING"
            await self._session.flush()
            return {
                "event_id": event.id,
                "status": "PENDING",
                "notification_id": None,
                "digest_window_id": window_id,
            }

        # IMMEDIATE
        templates = TemplateService(self._session)
        subject, body = await templates.render_immediate(category, channel, event)
        user = await self._session.get(User, user_id)
        recipient = DigestService._recipient_for(user, channel)

        notification = Notification(
            user_id=user_id,
            channel=channel,
            category=category,
            kind="IMMEDIATE",
            recipient_address=recipient,
            rendered_subject=subject,
            rendered_body=body,
            status="PENDING",
        )
        self._session.add(notification)
        await self._session.flush()

        event.status = "DISPATCHED"
        event.notification_id = notification.id
        await self._session.flush()

        chain = self._providers.for_channel(channel)
        first = chain.first()
        if first is None:
            notification.status = "FAILED"
            notification.updated_at = datetime.now(timezone.utc)
            await self._session.flush()
        else:
            await self._queueline.enqueue(
                self._settings.queueline_dispatch_queue,
                {"notificationId": str(notification.id), "providerName": first.name},
                max_attempts=self._settings.max_same_provider_retries,
            )

        return {
            "event_id": event.id,
            "status": event.status,
            "notification_id": notification.id,
            "digest_window_id": None,
        }

    async def list_notifications(
        self,
        user_id: uuid.UUID,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Notification], str | None]:
        q = (
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit + 1)
        )
        if cursor:
            # cursor = created_at|id
            created_s, id_s = cursor.split("|", 1)
            created_at = datetime.fromisoformat(created_s)
            nid = uuid.UUID(id_s)
            q = q.where(
                (Notification.created_at < created_at)
                | ((Notification.created_at == created_at) & (Notification.id < nid))
            )
        result = await self._session.execute(q)
        rows = list(result.scalars().all())
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = f"{last.created_at.isoformat()}|{last.id}"
        return rows, next_cursor
