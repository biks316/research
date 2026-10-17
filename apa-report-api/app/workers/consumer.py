"""SQS consumer: `python -m app.workers.consumer --queue planning|generation`.

- Long-polls SQS (WaitTimeSeconds=20) and runs up to WORKER_CONCURRENCY jobs at once.
- While a job runs, a heartbeat extends the message visibility (ChangeMessageVisibility)
  and the worker's lease on the report every SQS_HEARTBEAT_SECONDS.
- The message is deleted only when the job finished, was skipped, was cancelled, or failed
  permanently. Unexpected crashes leave the message for SQS to redeliver (and eventually move
  to the dead-letter queue after maxReceiveCount).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

import structlog

from app.clients.llm import LLMError, build_llm_client, track_usage
from app.clients.openalex import OpenAlexClient, OpenAlexError
from app.config import Settings, get_settings
from app.core.logging import bind_report, configure_logging
from app.core.resources import Resources, open_resources
from app.models.enums import ErrorCode, JobPhase, QueueName
from app.services.report_service import JobCancelled
from app.workers import generation_job, planning_job
from app.workers.context import JobContext, JobOutcome, PipelineError

log = structlog.get_logger(__name__)

PHASE_FOR_QUEUE = {QueueName.PLANNING: JobPhase.PLAN, QueueName.GENERATION: JobPhase.GENERATE}
JOBS: dict[JobPhase, Callable[[JobContext, uuid.UUID], Awaitable[JobOutcome]]] = {
    JobPhase.PLAN: planning_job.run,
    JobPhase.GENERATE: generation_job.run,
}


class Consumer:
    def __init__(self, queue: QueueName, resources: Resources, ctx: JobContext) -> None:
        self.queue = queue
        self.resources = resources
        self.ctx = ctx
        self.settings: Settings = resources.settings
        self.queue_url = resources.publisher.url_for(queue)
        self.sqs = resources.sqs
        self._stopping = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    def stop(self) -> None:
        self._stopping.set()

    # ------------------------------------------------------------ polling
    async def run(self) -> None:
        log.info("consumer_started", queue=self.queue.value, concurrency=self.settings.WORKER_CONCURRENCY)
        while not self._stopping.is_set():
            free = self.settings.WORKER_CONCURRENCY - len(self._tasks)
            if free <= 0:
                await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
                continue
            try:
                messages = await self.receive(min(10, free))
            except Exception:  # noqa: BLE001 - keep polling through transient SQS errors
                log.exception("sqs_receive_failed")
                try:  # back off, but wake immediately on shutdown
                    await asyncio.wait_for(self._stopping.wait(), timeout=5)
                except TimeoutError:
                    pass
                continue
            for message in messages:
                task = asyncio.create_task(self.handle(message))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        if self._tasks:
            log.info("consumer_draining", jobs=len(self._tasks))
            await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info("consumer_stopped")

    async def receive(self, max_messages: int, wait_seconds: int | None = None) -> list[dict[str, Any]]:
        response = await self.sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=self.settings.SQS_WAIT_TIME_SECONDS if wait_seconds is None else wait_seconds,
            VisibilityTimeout=self.settings.SQS_VISIBILITY_TIMEOUT_SECONDS,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        return response.get("Messages", [])

    async def delete(self, message: dict[str, Any]) -> None:
        await self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])

    # ---------------------------------------------------------- one message
    async def _heartbeat(self, message: dict[str, Any], report_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(self.settings.SQS_HEARTBEAT_SECONDS)
            try:
                await self.sqs.change_message_visibility(
                    QueueUrl=self.queue_url,
                    ReceiptHandle=message["ReceiptHandle"],
                    VisibilityTimeout=self.settings.SQS_VISIBILITY_TIMEOUT_SECONDS,
                )
                await self.ctx.reports.extend_lease(report_id, self.ctx.worker_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a missed beat is retried on the next one
                log.warning("heartbeat_failed", exc_info=True)

    async def handle(self, message: dict[str, Any]) -> None:
        structlog.contextvars.clear_contextvars()
        try:
            body = json.loads(message["Body"])
            report_id = uuid.UUID(body["reportId"])
            phase = JobPhase(body["phase"])
        except (ValueError, KeyError, TypeError):
            log.error("invalid_message_discarded", body=message.get("Body", "")[:500])
            await self.delete(message)
            return
        bind_report(report_id)
        structlog.contextvars.bind_contextvars(phase=phase.value, queue=self.queue.value)
        if phase != PHASE_FOR_QUEUE[self.queue]:
            log.error("message_on_wrong_queue_discarded")
            await self.delete(message)
            return
        receive_count = int(message.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
        outcome = await self.process(message, report_id, phase, receive_count)
        if outcome is not None:
            await self.delete(message)
            log.info("message_done", outcome=outcome.value)

    async def process(
        self, message: dict[str, Any], report_id: uuid.UUID, phase: JobPhase, receive_count: int
    ) -> JobOutcome | None:
        """Run the job. Returns the outcome (message gets deleted) or None (leave it to SQS)."""
        heartbeat = asyncio.create_task(self._heartbeat(message, report_id))
        reports = self.ctx.reports
        try:
            with track_usage() as usage:
                try:
                    return await JOBS[phase](self.ctx, report_id)
                except JobCancelled:
                    log.info("job_cancelled")
                    return JobOutcome.CANCELLED
                except PipelineError as exc:
                    await reports.fail(report_id, exc.code, exc.message, owner=self.ctx.worker_id)
                    return JobOutcome.FAILED
                except LLMError as exc:
                    await reports.fail(report_id, ErrorCode.LLM_UNAVAILABLE, str(exc), owner=self.ctx.worker_id)
                    return JobOutcome.FAILED
                except OpenAlexError as exc:
                    await reports.fail(report_id, ErrorCode.RETRIEVAL_FAILED, str(exc), owner=self.ctx.worker_id)
                    return JobOutcome.FAILED
                except Exception:
                    log.exception("job_crashed", receive_count=receive_count)
                    if receive_count >= self.settings.SQS_MAX_RECEIVE_COUNT:
                        # Last delivery before the DLQ: record the failure so the report isn't stuck.
                        await reports.fail(
                            report_id, ErrorCode.INTERNAL_ERROR, "An internal error stopped report generation.",
                            owner=self.ctx.worker_id,
                        )
                    else:
                        # Let the redelivered message resume immediately from the last checkpoint.
                        await reports.release_lease(report_id, self.ctx.worker_id)
                    return None
                finally:
                    if usage:
                        log.info("llm_token_usage", **usage)
                        try:
                            await reports.add_token_usage(report_id, usage)
                        except Exception:  # noqa: BLE001
                            log.warning("token_usage_save_failed")
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass


def worker_id(queue: QueueName) -> str:
    return f"{queue.value}:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


async def main(queue: QueueName) -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    async with AsyncExitStack() as stack:
        resources = await open_resources(settings, stack)
        llm = build_llm_client(settings)
        stack.push_async_callback(llm.aclose)
        openalex = OpenAlexClient(settings, resources.redis)
        stack.push_async_callback(openalex.aclose)
        ctx = JobContext(settings=settings, reports=resources.reports, llm=llm, openalex=openalex, worker_id=worker_id(queue))
        consumer = Consumer(queue, resources, ctx)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, consumer.stop)
        await consumer.run()


def cli() -> None:
    parser = argparse.ArgumentParser(description="APA report worker")
    parser.add_argument("--queue", required=True, choices=[q.value for q in QueueName])
    args = parser.parse_args()
    asyncio.run(main(QueueName(args.queue)))


if __name__ == "__main__":
    cli()
