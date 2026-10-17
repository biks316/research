"""Idempotency-Key handling for POST /reports, stored in Redis per user + key."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from app.core.errors import Conflict, IdempotencyConflict


class IdempotencyInProgress(Conflict):
    code, title = "IDEMPOTENCY_REQUEST_IN_PROGRESS", "Request in progress"


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]
    headers: dict[str, str]


def body_hash(body: Any) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class IdempotencyStore:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    @staticmethod
    def _key(user_id: str, key: str) -> str:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return f"idem:{user_id}:{digest}"

    async def begin(self, user_id: str, key: str, request_hash: str) -> StoredResponse | None:
        """Reserve the key. Returns the stored response to replay, or None to proceed.

        Raises 409 when the key was used with a different body, or when the first request
        with this key is still being processed.
        """
        redis_key = self._key(user_id, key)
        pending = json.dumps({"hash": request_hash, "state": "pending"})
        if await self._redis.set(redis_key, pending, nx=True, ex=self._ttl):
            return None
        raw = await self._redis.get(redis_key)
        if raw is None:  # expired between SET NX and GET; retry once
            if await self._redis.set(redis_key, pending, nx=True, ex=self._ttl):
                return None
            raw = await self._redis.get(redis_key)
        record = json.loads(raw)
        if record["hash"] != request_hash:
            raise IdempotencyConflict(
                "This Idempotency-Key was already used with a different request body."
            )
        if record["state"] != "done":
            raise IdempotencyInProgress("A request with this Idempotency-Key is still being processed. Retry shortly.")
        return StoredResponse(record["status_code"], record["body"], record["headers"])

    async def complete(self, user_id: str, key: str, request_hash: str, response: StoredResponse) -> None:
        record = {
            "hash": request_hash,
            "state": "done",
            "status_code": response.status_code,
            "body": response.body,
            "headers": response.headers,
        }
        await self._redis.set(self._key(user_id, key), json.dumps(record), ex=self._ttl)

    async def release(self, user_id: str, key: str) -> None:
        """Drop a pending reservation after a failed request so the client can retry."""
        await self._redis.delete(self._key(user_id, key))
