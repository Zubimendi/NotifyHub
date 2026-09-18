from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)


class EventCreate(CamelModel):
    user_id: UUID = Field(alias="userId")
    category: str
    channel: str
    template_data: dict[str, Any] = Field(default_factory=dict, alias="templateData")


class EventResponse(CamelModel):
    event_id: UUID = Field(alias="eventId")
    status: str
    notification_id: UUID | None = Field(default=None, alias="notificationId")
    digest_window_id: UUID | None = Field(default=None, alias="digestWindowId")


class PreferenceUpdate(CamelModel):
    category: str
    channel: str
    enabled: bool | None = None
    batching_mode: str | None = Field(default=None, alias="batchingMode")


class PreferenceResponse(CamelModel):
    user_id: UUID = Field(alias="userId")
    category: str
    channel: str
    enabled: bool
    batching_mode: str = Field(alias="batchingMode")


class BounceWebhook(CamelModel):
    event_id: str = Field(alias="eventId")
    user_id: UUID = Field(alias="userId")
    channel: str
    reason: str
    category: str | None = None


class BounceResponse(CamelModel):
    inserted: bool


class NotificationResponse(CamelModel):
    id: UUID
    channel: str
    category: str
    kind: str
    status: str
    recipient_address: str = Field(alias="recipientAddress")
    rendered_subject: str = Field(alias="renderedSubject")
    rendered_body: str = Field(alias="renderedBody")
    created_at: str = Field(alias="createdAt")


class NotificationListResponse(CamelModel):
    items: list[NotificationResponse]
    next_cursor: str | None = Field(default=None, alias="nextCursor")
