"""Digest flush sweep process.

    python -m app.digest_sweep
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

import redis.asyncio as redis

from app.config import get_settings
from app.db.session import SessionLocal
from app.services.digest_service import DigestService
from app.services.queueline_client import QueueLineClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notifyhub.digest_sweep")


async def run_sweep() -> None:
    settings = get_settings()
    queueline = QueueLineClient(settings.queueline_base_url)
    r = redis.from_url(settings.redis_url, decode_responses=True)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    interval = settings.digest_sweep_interval_ms / 1000.0
    log.info("digest sweep interval=%.1fs", interval)

    while not stop.is_set():
        try:
            async with SessionLocal() as session:
                digest = DigestService(session, r, settings, queueline=queueline)
                recovered = await digest.recover_stuck_flushing()
                flushed = await digest.flush_due_windows()
                await session.commit()
                if recovered or flushed:
                    log.info("recovered=%s flushed=%s", recovered, flushed)
        except Exception:
            log.exception("sweep iteration failed")

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    await r.aclose()
    log.info("digest sweep shut down")


def main() -> None:
    asyncio.run(run_sweep())


if __name__ == "__main__":
    main()
