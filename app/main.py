from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from sqlalchemy import text

from app.api.routes import router as v1_router
from app.config import get_settings
from app.db.session import engine
from app.deps import close_deps, init_deps
from app.observability import metrics_response
from app.services.queueline_client import QueueLineClient

import redis.asyncio as redis


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_deps()
    yield
    await close_deps()
    await engine.dispose()


app = FastAPI(title="NotifyHub", version="0.1.0", lifespan=lifespan)
app.include_router(v1_router)


@app.get("/health/live")
async def health_live():
    return {"status": "live"}


@app.get("/health/ready")
async def health_ready():
    settings = get_settings()
    checks: dict[str, bool] = {"postgres": False, "redis": False, "queueline": False}

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:
        checks["postgres"] = False

    try:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        checks["redis"] = (await r.ping()) is True
        await r.aclose()
    except Exception:
        checks["redis"] = False

    ql = QueueLineClient(settings.queueline_base_url)
    checks["queueline"] = await ql.ready()

    ok = all(checks.values())
    return Response(
        content=__import__("json").dumps({"status": "ready" if ok else "not_ready", "checks": checks}),
        status_code=200 if ok else 503,
        media_type="application/json",
    )


@app.get("/metrics")
async def metrics():
    body, content_type = metrics_response()
    return Response(content=body, media_type=content_type)
