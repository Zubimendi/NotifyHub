from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import Notification, NotificationSendAttempt
from app.observability import (
    record_dispatch_ambiguous,
    record_dispatch_definitive_stop,
    record_dispatch_failover,
    record_dispatch_success,
)
from app.services.preference_service import PreferenceService, SuppressionService
from app.services.provider_registry import ProviderRegistry
from app.services.queueline_client import QueueLineClient

DispatchAction = Literal["complete", "fail"]


@dataclass
class DispatchOutcome:
    action: DispatchAction
    error: str | None = None


def idempotency_key_for(notification_id: uuid.UUID, provider_name: str) -> str:
    """Stable across retries against the same provider — excludes attempt_number."""
    raw = f"{notification_id}:{provider_name}"
    return hashlib.sha256(raw.encode()).hexdigest()


class DispatchService:
    TERMINAL = frozenset({"SENT", "FAILED", "SUPPRESSED"})

    def __init__(
        self,
        session: AsyncSession,
        queueline: QueueLineClient,
        settings: Settings | None = None,
        providers: ProviderRegistry | None = None,
    ) -> None:
        self._session = session
        self._queueline = queueline
        self._settings = settings or get_settings()
        self._providers = providers or ProviderRegistry(self._settings)

    async def dispatch(
        self,
        notification_id: uuid.UUID,
        provider_name: str,
        *,
        queueline_job_id: str | None = None,
    ) -> DispatchOutcome:
        notification = await self._session.get(Notification, notification_id)
        if notification is None:
            return DispatchOutcome(action="complete", error="notification_not_found")

        if notification.status in self.TERMINAL:
            # Stale job — prior attempt already resolved it.
            return DispatchOutcome(action="complete")

        prefs = PreferenceService(self._session)
        suppressions = SuppressionService(self._session)
        pref = await prefs.get(notification.user_id, notification.category, notification.channel)
        if (not pref.enabled) or await suppressions.is_suppressed(
            notification.user_id, notification.channel, notification.category
        ):
            notification.status = "SUPPRESSED"
            notification.suppression_reason = "preference_or_suppression"
            notification.updated_at = datetime.now(timezone.utc)
            await self._session.flush()
            return DispatchOutcome(action="complete")

        chain = self._providers.for_channel(notification.channel)
        provider = chain.get(provider_name)
        if provider is None:
            notification.status = "FAILED"
            notification.updated_at = datetime.now(timezone.utc)
            await self._session.flush()
            return DispatchOutcome(action="complete", error="unknown_provider")

        # NotifyHub audit attempt_number — independent of QueueLine's attempts.
        attempt_number = await self._next_attempt_number(notification_id, provider_name)
        key = idempotency_key_for(notification_id, provider_name)

        attempt = NotificationSendAttempt(
            notification_id=notification_id,
            provider_name=provider_name,
            attempt_number=attempt_number,
            idempotency_key=key,
            queueline_job_id=queueline_job_id,
            started_at=datetime.now(timezone.utc),
        )
        self._session.add(attempt)
        await self._session.flush()

        notification.status = "DISPATCHING"
        notification.updated_at = datetime.now(timezone.utc)
        await self._session.flush()

        result = await provider.send(
            recipient=notification.recipient_address,
            subject=notification.rendered_subject,
            body=notification.rendered_body,
            idempotency_key=key,
        )

        attempt.outcome = result.outcome
        attempt.provider_message_id = result.provider_message_id
        attempt.error_detail = result.error_detail
        attempt.completed_at = datetime.now(timezone.utc)
        await self._session.flush()

        if result.outcome == "SUCCESS":
            notification.status = "SENT"
            notification.updated_at = datetime.now(timezone.utc)
            await self._session.flush()
            record_dispatch_success(notification.channel, provider_name)
            return DispatchOutcome(action="complete")

        if result.outcome == "AMBIGUOUS_FAILURE":
            record_dispatch_ambiguous(notification.channel, provider_name)
            # Exhaustion: failover (or fail) instead of asking QueueLine to retry forever.
            if attempt_number >= self._settings.max_same_provider_retries:
                return await self._failover_or_fail(notification, chain, provider_name)
            return DispatchOutcome(
                action="fail",
                error=result.error_detail or "ambiguous_failure",
            )

        if result.outcome == "DEFINITIVE_RECIPIENT_INVALID":
            notification.status = "FAILED"
            notification.updated_at = datetime.now(timezone.utc)
            await self._session.flush()
            record_dispatch_definitive_stop(notification.channel, provider_name)
            return DispatchOutcome(action="complete")

        if result.outcome == "DEFINITIVE_PROVIDER_FAILURE":
            record_dispatch_failover(notification.channel, provider_name)
            return await self._failover_or_fail(notification, chain, provider_name)

        # Unknown outcome — treat as ambiguous
        return DispatchOutcome(action="fail", error="unknown_outcome")

    async def _failover_or_fail(self, notification: Notification, chain, provider_name: str) -> DispatchOutcome:
        nxt = chain.next_after(provider_name)
        if nxt is None:
            notification.status = "FAILED"
            notification.updated_at = datetime.now(timezone.utc)
            await self._session.flush()
            return DispatchOutcome(action="complete")

        await self._queueline.enqueue(
            self._settings.queueline_dispatch_queue,
            {"notificationId": str(notification.id), "providerName": nxt.name},
            max_attempts=self._settings.max_same_provider_retries,
        )
        # Reset to PENDING so the next provider's job can proceed.
        if notification.status == "DISPATCHING":
            notification.status = "PENDING"
            notification.updated_at = datetime.now(timezone.utc)
            await self._session.flush()
        return DispatchOutcome(action="complete")

    async def _next_attempt_number(self, notification_id: uuid.UUID, provider_name: str) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.max(NotificationSendAttempt.attempt_number), 0)).where(
                NotificationSendAttempt.notification_id == notification_id,
                NotificationSendAttempt.provider_name == provider_name,
            )
        )
        return int(result.scalar_one()) + 1
