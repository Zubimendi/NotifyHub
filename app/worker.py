"""QueueLine-consuming dispatch worker.

    python -m app.worker
"""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from contextlib import suppress

from app.config import get_settings
from app.db.session import SessionLocal
from app.services.dispatch_service import DispatchService
from app.services.queueline_client import QueueLineClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notifyhub.worker")

LEASE_SECONDS = 30


async def process_job(queueline: QueueLineClient, job) -> None:
    payload = job.payload
    notification_id = uuid.UUID(payload["notificationId"])
    provider_name = payload["providerName"]
    lease_id = job.lease_id
    if not lease_id:
        log.error("job %s missing leaseId", job.id)
        return

    stop_heartbeat = asyncio.Event()

    async def heartbeat_loop() -> None:
        while not stop_heartbeat.is_set():
            try:
                await asyncio.wait_for(stop_heartbeat.wait(), timeout=LEASE_SECONDS / 2)
                return
            except asyncio.TimeoutError:
                with suppress(Exception):
                    await queueline.heartbeat(job.id, lease_id, LEASE_SECONDS)

    hb_task = asyncio.create_task(heartbeat_loop())
    try:
        async with SessionLocal() as session:
            dispatch = DispatchService(session, queueline)
            outcome = await dispatch.dispatch(
                notification_id,
                provider_name,
                queueline_job_id=job.id,
            )
            await session.commit()

        if outcome.action == "fail":
            await queueline.fail(job.id, lease_id, outcome.error or "ambiguous_failure")
        else:
            await queueline.complete(job.id, lease_id)
    except Exception as exc:
        log.exception("dispatch failed for job %s", job.id)
        with suppress(Exception):
            await queueline.fail(job.id, lease_id, str(exc))
    finally:
        stop_heartbeat.set()
        hb_task.cancel()
        with suppress(asyncio.CancelledError):
            await hb_task


async def run_worker() -> None:
    settings = get_settings()
    queueline = QueueLineClient(settings.queueline_base_url)
    queue = settings.queueline_dispatch_queue
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("worker listening on queue=%s queueline=%s", queue, settings.queueline_base_url)
    in_flight: asyncio.Task | None = None

    while not stop.is_set():
        try:
            job = await queueline.lease(queue, LEASE_SECONDS)
        except Exception:
            log.exception("lease error")
            await asyncio.sleep(0.5)
            continue

        if job is None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            continue

        # Finish in-flight before accepting shutdown after this job starts.
        in_flight = asyncio.create_task(process_job(queueline, job))
        await in_flight
        in_flight = None

    if in_flight is not None:
        await in_flight
    log.info("worker shut down cleanly")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
