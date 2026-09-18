from __future__ import annotations

from collections.abc import AsyncGenerator

import redis.asyncio as redis

from app.config import Settings, get_settings
from app.services.queueline_client import QueueLineClient

_redis: redis.Redis | None = None
_queueline: QueueLineClient | None = None


async def init_deps() -> None:
    global _redis, _queueline
    settings = get_settings()
    _redis = redis.from_url(settings.redis_url, decode_responses=True)
    _queueline = QueueLineClient(settings.queueline_base_url)


async def close_deps() -> None:
    global _redis, _queueline
    if _redis is not None:
        await _redis.aclose()
        _redis = None
    _queueline = None


def get_settings_dep() -> Settings:
    return get_settings()


async def get_redis() -> AsyncGenerator[redis.Redis, None]:
    assert _redis is not None
    yield _redis


async def get_queueline() -> AsyncGenerator[QueueLineClient, None]:
    assert _queueline is not None
    yield _queueline
