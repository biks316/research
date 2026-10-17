"""SQS publisher."""

from __future__ import annotations

import json
from typing import Any, Protocol

import structlog

from app.config import Settings
from app.models.enums import QueueName

log = structlog.get_logger(__name__)


class SQSClient(Protocol):
    async def send_message(self, **kwargs: Any) -> dict[str, Any]: ...


class QueuePublisher:
    def __init__(self, sqs: SQSClient, settings: Settings) -> None:
        self._sqs = sqs
        self._urls = {
            QueueName.PLANNING: settings.SQS_PLANNING_QUEUE_URL,
            QueueName.GENERATION: settings.SQS_GENERATION_QUEUE_URL,
        }

    def url_for(self, queue: QueueName | str) -> str:
        return self._urls[QueueName(queue)]

    async def publish(self, queue: QueueName | str, payload: dict[str, Any]) -> str:
        response = await self._sqs.send_message(
            QueueUrl=self.url_for(queue), MessageBody=json.dumps(payload, separators=(",", ":"))
        )
        message_id = response.get("MessageId", "")
        log.info("queue_published", queue=str(queue), message_id=message_id, report_id=payload.get("reportId"))
        return message_id
