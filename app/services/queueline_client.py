"""Thin async HTTP client for the sibling QueueLine service (../QueueLine).

Mirrors ../QueueLine/docs/API.md and pkg/client/client.go.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class Job:
    id: str
    queue: str
    payload: dict[str, Any]
    attempts: int = 0
    lease_id: str | None = None


class QueueLineClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def enqueue(
        self,
        queue: str,
        payload: dict[str, Any],
        *,
        priority: int = 0,
        delay_seconds: int = 0,
        max_attempts: int = 5,
        dedup_key: str | None = None,
    ) -> Job:
        body: dict[str, Any] = {
            "payload": payload,
            "priority": priority,
            "delaySeconds": delay_seconds,
            "maxAttempts": max_attempts,
        }
        if dedup_key:
            body["dedupKey"] = dedup_key
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{self.base_url}/v1/queues/{queue}/jobs", json=body)
            resp.raise_for_status()
            return self._parse_job(resp.json())

    async def lease(self, queue: str, lease_seconds: int = 30) -> Job | None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/queues/{queue}/lease",
                json={"leaseSeconds": lease_seconds},
            )
            if resp.status_code == 204:
                return None
            resp.raise_for_status()
            return self._parse_job(resp.json())

    async def heartbeat(self, job_id: str, lease_id: str, extend_seconds: int = 30) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/jobs/{job_id}/heartbeat",
                json={"leaseId": lease_id, "extendSeconds": extend_seconds},
            )
            resp.raise_for_status()

    async def complete(self, job_id: str, lease_id: str) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/jobs/{job_id}/complete",
                json={"leaseId": lease_id},
            )
            resp.raise_for_status()

    async def fail(self, job_id: str, lease_id: str, error: str) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/jobs/{job_id}/fail",
                json={"leaseId": lease_id, "error": error},
            )
            resp.raise_for_status()

    async def ready(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self.base_url}/readyz")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    @staticmethod
    def _parse_job(data: dict[str, Any]) -> Job:
        payload = data.get("payload") or {}
        if isinstance(payload, str):
            import json

            payload = json.loads(payload)
        return Job(
            id=data["id"],
            queue=data.get("queue", ""),
            payload=payload if isinstance(payload, dict) else {},
            attempts=int(data.get("attempts") or 0),
            lease_id=data.get("leaseId"),
        )
