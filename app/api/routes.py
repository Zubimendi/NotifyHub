from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from app.db.session import get_session
from app.deps import get_queueline, get_redis, get_settings_dep
from app.schemas import (
    BounceResponse,
    BounceWebhook,
    EventCreate,
    EventResponse,
    NotificationListResponse,
    NotificationResponse,
    PreferenceResponse,
    PreferenceUpdate,
)
from app.config import Settings
from app.observability import record_suppression
from app.services.event_service import EventService
from app.services.preference_service import PreferenceService, SuppressionService
from app.services.queueline_client import QueueLineClient
from app.services.unsubscribe_token import UnsubscribeTokenService

import redis.asyncio as redis

router = APIRouter(prefix="/v1")


@router.post("/events", response_model=EventResponse)
async def create_event(
    body: EventCreate,
    session: AsyncSession = Depends(get_session),
    r: redis.Redis = Depends(get_redis),
    ql: QueueLineClient = Depends(get_queueline),
):
    svc = EventService(session, r, ql)
    try:
        result = await svc.ingest(body.user_id, body.category, body.channel, body.template_data)
        await session.commit()
    except httpx.HTTPError as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail=f"queueline_unavailable: {exc}") from exc
    return EventResponse(
        eventId=result["event_id"],
        status=result["status"],
        notificationId=result["notification_id"],
        digestWindowId=result["digest_window_id"],
    )


@router.put("/users/{user_id}/preferences", response_model=PreferenceResponse)
async def put_preference(
    user_id: UUID,
    body: PreferenceUpdate,
    session: AsyncSession = Depends(get_session),
):
    svc = PreferenceService(session)
    pref = await svc.set(
        user_id,
        body.category,
        body.channel.upper(),
        enabled=body.enabled,
        batching_mode=body.batching_mode,
    )
    await session.commit()
    return PreferenceResponse(
        userId=pref.user_id,
        category=pref.category,
        channel=pref.channel,
        enabled=pref.enabled,
        batchingMode=pref.batching_mode,
    )


@router.get("/users/{user_id}/preferences", response_model=list[PreferenceResponse])
async def get_preferences(
    user_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    svc = PreferenceService(session)
    prefs = await svc.list_for_user(user_id)
    return [
        PreferenceResponse(
            userId=p.user_id,
            category=p.category,
            channel=p.channel,
            enabled=p.enabled,
            batchingMode=p.batching_mode,
        )
        for p in prefs
    ]


@router.post("/users/{user_id}/unsubscribe")
async def unsubscribe(
    user_id: UUID,
    token: str = Query(...),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
):
    tokens = UnsubscribeTokenService(settings.unsubscribe_token_secret)
    claims = tokens.verify(token)
    if claims is None or claims.user_id != user_id:
        raise HTTPException(status_code=400, detail="invalid_token")

    prefs = PreferenceService(session)
    suppressions = SuppressionService(session)
    await prefs.set(
        claims.user_id,
        claims.category,
        claims.channel,
        enabled=False,
    )
    await suppressions.record_unsubscribe(claims.user_id, claims.channel, claims.category)
    await session.commit()
    record_suppression("UNSUBSCRIBE")
    return {"status": "unsubscribed", "category": claims.category, "channel": claims.channel}


@router.post("/webhooks/{provider}/bounce", response_model=BounceResponse)
async def bounce_webhook(
    provider: str,
    body: BounceWebhook,
    session: AsyncSession = Depends(get_session),
):
    svc = SuppressionService(session)
    inserted = await svc.record_bounce(
        provider=provider,
        source_event_id=body.event_id,
        user_id=body.user_id,
        channel=body.channel.upper(),
        reason=body.reason,
        category=body.category,
    )
    await session.commit()
    if inserted:
        record_suppression(body.reason)
    return BounceResponse(inserted=inserted)


@router.get("/users/{user_id}/notifications", response_model=NotificationListResponse)
async def list_notifications(
    user_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
    session: AsyncSession = Depends(get_session),
    r: redis.Redis = Depends(get_redis),
    ql: QueueLineClient = Depends(get_queueline),
):
    svc = EventService(session, r, ql)
    rows, next_cursor = await svc.list_notifications(user_id, limit=limit, cursor=cursor)
    items = [
        NotificationResponse(
            id=n.id,
            channel=n.channel,
            category=n.category,
            kind=n.kind,
            status=n.status,
            recipientAddress=n.recipient_address,
            renderedSubject=n.rendered_subject,
            renderedBody=n.rendered_body,
            createdAt=n.created_at.isoformat(),
        )
        for n in rows
    ]
    return NotificationListResponse(items=items, nextCursor=next_cursor)
