from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NotificationPreference, Suppression


@dataclass
class Preference:
    user_id: uuid.UUID
    category: str
    channel: str
    enabled: bool
    batching_mode: str


class PreferenceService:
    """User preference lookups and updates.

    Default when no row exists: enabled=True, batching_mode=IMMEDIATE.
    An unconfigured category/channel pair is opted in by default.
    """

    DEFAULT_ENABLED = True
    DEFAULT_BATCHING_MODE = "IMMEDIATE"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID, category: str, channel: str) -> Preference:
        row = await self._session.get(NotificationPreference, (user_id, category, channel))
        if row is None:
            return Preference(
                user_id=user_id,
                category=category,
                channel=channel,
                enabled=self.DEFAULT_ENABLED,
                batching_mode=self.DEFAULT_BATCHING_MODE,
            )
        return Preference(
            user_id=row.user_id,
            category=row.category,
            channel=row.channel,
            enabled=row.enabled,
            batching_mode=row.batching_mode,
        )

    async def set(
        self,
        user_id: uuid.UUID,
        category: str,
        channel: str,
        *,
        enabled: bool | None = None,
        batching_mode: str | None = None,
    ) -> Preference:
        row = await self._session.get(NotificationPreference, (user_id, category, channel))
        now = datetime.now(timezone.utc)
        if row is None:
            row = NotificationPreference(
                user_id=user_id,
                category=category,
                channel=channel,
                enabled=enabled if enabled is not None else self.DEFAULT_ENABLED,
                batching_mode=batching_mode if batching_mode is not None else self.DEFAULT_BATCHING_MODE,
                updated_at=now,
            )
            self._session.add(row)
        else:
            if enabled is not None:
                row.enabled = enabled
            if batching_mode is not None:
                row.batching_mode = batching_mode
            row.updated_at = now

        # Setting enabled=true clears matching UNSUBSCRIBE suppressions only —
        # never HARD_BOUNCE or COMPLAINT. Kept here so no API path can bypass it.
        if row.enabled:
            await self._session.execute(
                update(Suppression)
                .where(
                    Suppression.user_id == user_id,
                    Suppression.channel == channel,
                    Suppression.category == category,
                    Suppression.reason == "UNSUBSCRIBE",
                    Suppression.cleared_at.is_(None),
                )
                .values(cleared_at=now)
            )

        await self._session.flush()
        return Preference(
            user_id=row.user_id,
            category=row.category,
            channel=row.channel,
            enabled=row.enabled,
            batching_mode=row.batching_mode,
        )

    async def list_for_user(self, user_id: uuid.UUID) -> list[Preference]:
        result = await self._session.execute(
            select(NotificationPreference).where(NotificationPreference.user_id == user_id)
        )
        rows = result.scalars().all()
        return [
            Preference(
                user_id=r.user_id,
                category=r.category,
                channel=r.channel,
                enabled=r.enabled,
                batching_mode=r.batching_mode,
            )
            for r in rows
        ]


class SuppressionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_suppressed(self, user_id: uuid.UUID, channel: str, category: str) -> bool:
        """Active suppression for this channel, matching category or channel-wide (NULL)."""
        result = await self._session.execute(
            text(
                """
                SELECT 1 FROM suppressions
                WHERE user_id = :user_id
                  AND channel = :channel
                  AND (category = :category OR category IS NULL)
                  AND cleared_at IS NULL
                LIMIT 1
                """
            ),
            {"user_id": user_id, "channel": channel, "category": category},
        )
        return result.first() is not None

    async def record_bounce(
        self,
        provider: str,
        source_event_id: str,
        user_id: uuid.UUID,
        channel: str,
        reason: str,
        category: str | None = None,
    ) -> bool:
        """Insert a suppression idempotently by source_event_id.

        Returns True if a new row was inserted, False if the event was already seen.
        """
        # HARD_BOUNCE / COMPLAINT are channel-wide (category NULL).
        cat = None if reason in ("HARD_BOUNCE", "COMPLAINT") else category
        result = await self._session.execute(
            text(
                """
                INSERT INTO suppressions (user_id, channel, reason, category, source_event_id)
                VALUES (:user_id, :channel, :reason, :category, :source_event_id)
                ON CONFLICT (source_event_id) WHERE source_event_id IS NOT NULL
                DO NOTHING
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "channel": channel,
                "reason": reason,
                "category": cat,
                "source_event_id": source_event_id,
            },
        )
        inserted = result.first() is not None
        await self._session.flush()
        return inserted

    async def record_unsubscribe(
        self,
        user_id: uuid.UUID,
        channel: str,
        category: str,
    ) -> None:
        self._session.add(
            Suppression(
                user_id=user_id,
                channel=channel,
                reason="UNSUBSCRIBE",
                category=category,
            )
        )
        await self._session.flush()
