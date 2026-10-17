"""Per-user fixed-window rate limiter in Redis."""

from __future__ import annotations

import time

from redis.asyncio import Redis

from app.core.errors import TooManyRequests


class RateLimiter:
    def __init__(self, redis: Redis, *, limit: int, window_seconds: int) -> None:
        self._redis = redis
        self._limit = limit
        self._window = window_seconds

    async def hit(self, user_id: str, action: str) -> None:
        window_start = int(time.time()) // self._window * self._window
        key = f"rl:{action}:{user_id}:{window_start}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, self._window + 1)
            count, _ = await pipe.execute()
        if int(count) > self._limit:
            retry_after = window_start + self._window - int(time.time())
            raise TooManyRequests(
                f"Rate limit exceeded: at most {self._limit} requests per {self._window} seconds.",
                retry_after=max(1, retry_after),
            )
