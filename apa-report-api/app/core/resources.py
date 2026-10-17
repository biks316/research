"""Long-lived clients (DB, Redis, SQS, S3) shared by the API and workers."""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import aioboto3
from botocore.config import Config as BotoConfig
from redis.asyncio import Redis

from app.config import Settings
from app.core.database import Database
from app.services.idempotency import IdempotencyStore
from app.services.outbox import OutboxDispatcher
from app.services.progress import ProgressBus
from app.services.queue import QueuePublisher
from app.services.rate_limit import RateLimiter
from app.services.report_service import ReportService
from app.services.storage import Storage


def build_redis(settings: Settings) -> Redis:
    return Redis.from_url(settings.REDIS_URL, decode_responses=True, health_check_interval=30)


def aws_client_kwargs(settings: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "region_name": settings.AWS_REGION,
        "config": BotoConfig(retries={"max_attempts": 5, "mode": "standard"}),
    }
    if settings.AWS_ENDPOINT_URL:
        kwargs["endpoint_url"] = settings.AWS_ENDPOINT_URL
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY.get_secret_value()
    return kwargs


@dataclass
class Resources:
    settings: Settings
    db: Database
    redis: Redis
    sqs: Any
    s3: Any
    publisher: QueuePublisher
    dispatcher: OutboxDispatcher
    progress: ProgressBus
    storage: Storage
    reports: ReportService


async def open_resources(settings: Settings, stack: AsyncExitStack) -> Resources:
    db = Database(settings)
    stack.push_async_callback(db.dispose)
    redis = build_redis(settings)
    stack.push_async_callback(redis.aclose)
    session = aioboto3.Session()
    sqs = await stack.enter_async_context(session.client("sqs", **aws_client_kwargs(settings)))
    s3 = await stack.enter_async_context(session.client("s3", **aws_client_kwargs(settings)))
    publisher = QueuePublisher(sqs, settings)
    dispatcher = OutboxDispatcher(db, publisher)
    progress = ProgressBus(redis)
    return Resources(
        settings=settings,
        db=db,
        redis=redis,
        sqs=sqs,
        s3=s3,
        publisher=publisher,
        dispatcher=dispatcher,
        progress=progress,
        storage=Storage(s3, settings.S3_EXPORTS_BUCKET, settings.S3_PRESIGN_TTL_SECONDS),
        reports=ReportService(db, dispatcher, progress, settings),
    )


def api_helpers(resources: Resources) -> tuple[IdempotencyStore, RateLimiter]:
    settings = resources.settings
    return (
        IdempotencyStore(resources.redis, settings.IDEMPOTENCY_TTL_SECONDS),
        RateLimiter(resources.redis, limit=settings.RATE_LIMIT_REQUESTS, window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS),
    )
