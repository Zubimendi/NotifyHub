from __future__ import annotations

from jinja2 import BaseLoader, Environment, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NotificationEvent, NotificationTemplate


class ErrTemplateNotFound(Exception):
    def __init__(self, category: str, channel: str, kind: str) -> None:
        self.category = category
        self.channel = channel
        self.kind = kind
        super().__init__(f"No notification template for category={category!r} channel={channel!r} kind={kind!r}")


class TemplateService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._env = Environment(
            loader=BaseLoader(),
            autoescape=select_autoescape(enabled_extensions=("html", "xml")),
        )

    async def render_immediate(
        self,
        category: str,
        channel: str,
        event: NotificationEvent,
    ) -> tuple[str, str]:
        tmpl = await self._load(category, channel, "IMMEDIATE")
        ctx = dict(event.template_data or {})
        subject = self._env.from_string(tmpl.subject_template).render(**ctx)
        body = self._env.from_string(tmpl.body_template).render(**ctx)
        return subject, body

    async def render_digest(
        self,
        category: str,
        channel: str,
        events: list[NotificationEvent],
    ) -> tuple[str, str]:
        tmpl = await self._load(category, channel, "DIGEST")
        event_dicts = [dict(e.template_data or {}) for e in events]
        ctx = {"events": event_dicts}
        subject = self._env.from_string(tmpl.subject_template).render(**ctx)
        body = self._env.from_string(tmpl.body_template).render(**ctx)
        return subject, body

    async def _load(self, category: str, channel: str, kind: str) -> NotificationTemplate:
        row = await self._session.get(NotificationTemplate, (category, channel, kind))
        if row is None:
            raise ErrTemplateNotFound(category, channel, kind)
        return row
