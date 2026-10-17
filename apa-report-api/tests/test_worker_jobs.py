"""Worker jobs with the LLM and OpenAlex mocked: happy path, resume, duplicates, cancellation, failures."""

from __future__ import annotations

import asyncio
import re
import uuid

import pytest
from sqlalchemy import func, select

from app.clients.llm import FakeLLMClient, LLMUnavailable
from app.clients.openalex import OpenAlexError
from app.models import EvidenceCard, Paper, Report, ReportSection, ScreeningResult
from app.models.enums import JobPhase, QueueName
from app.pipeline import fake_handlers
from app.pipeline.models import SectionDraft
from app.workers.consumer import Consumer
from app.workers.context import JobContext
from tests.conftest import GENERATION_QUEUE_URL, PLANNING_QUEUE_URL, VALID_BODY
from tests.fakes import FakeOpenAlex
from tests.helpers import set_status
from tests.test_reports_api import API

GEN_STATUSES = ["SEARCHING", "SCREENING", "EXTRACTING", "WRITING", "VERIFYING", "COMPLETED"]


@pytest.fixture
def fake_llm(settings):
    return FakeLLMClient(settings)


@pytest.fixture
def fake_openalex():
    return FakeOpenAlex()


@pytest.fixture
def make_consumer(app, settings, fake_llm, fake_openalex):
    def _make(queue: QueueName, worker: str = "worker-a") -> Consumer:
        resources = app.state.resources
        ctx = JobContext(settings=settings, reports=resources.reports, llm=fake_llm, openalex=fake_openalex, worker_id=worker)
        return Consumer(queue, resources, ctx)

    return _make


async def run_one(consumer: Consumer) -> list[dict]:
    messages = await consumer.receive(10, wait_seconds=0)
    for m in messages:
        await consumer.handle(m)
    return messages


def queue_depth(aws, url: str) -> int:
    attrs = aws["sqs"].get_queue_attributes(
        QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
    )["Attributes"]
    return int(attrs["ApproximateNumberOfMessages"]) + int(attrs["ApproximateNumberOfMessagesNotVisible"])


async def plan_and_approve(client, headers, make_consumer) -> str:
    report = (await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)).json()
    assert len(await run_one(make_consumer(QueueName.PLANNING))) == 1
    plan = (await client.get(f"{API}/reports/{report['id']}/plan", headers=headers)).json()
    resp = await client.post(f"{API}/reports/{report['id']}/plan/approve", json={"version": plan["version"]}, headers=headers)
    assert resp.status_code == 202
    return report["id"]


async def count(app, model, report_id: str) -> int:
    async with app.state.resources.db.session() as session:
        stmt = select(func.count()).select_from(model).where(model.report_id == uuid.UUID(report_id))
        return (await session.execute(stmt)).scalar_one()


# ---------------------------------------------------------------- planning
async def test_planning_job_produces_plan_and_awaits_approval(app, client, headers, make_consumer, aws, fake_llm):
    report = (await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)).json()
    handled = await run_one(make_consumer(QueueName.PLANNING))
    assert len(handled) == 1
    status = (await client.get(f"{API}/reports/{report['id']}/status", headers=headers)).json()
    assert status["status"] == "AWAITING_APPROVAL"
    assert status["progressPercent"] == 10
    plan = (await client.get(f"{API}/reports/{report['id']}/plan", headers=headers)).json()
    assert plan["editable"] is True and plan["version"] == 1
    assert 3 <= len(plan["subQuestions"]) <= 6
    roles = [s["role"] for s in plan["subQuestions"]]
    assert "CORE" in roles and roles == sorted(roles, key=["BACKGROUND", "CORE", "DISCUSSION"].index)
    # "causes" in the description becomes a sub-question to test, not a fact.
    assert any("causal" in s["question"] for s in plan["subQuestions"])
    assert "causal claim" in plan["interpretationNote"]
    assert queue_depth(aws, PLANNING_QUEUE_URL) == 0  # message deleted


# --------------------------------------------------------------- happy path
async def test_full_generation_happy_path(app, client, headers, make_consumer, aws, fake_openalex):
    report_id = await plan_and_approve(client, headers, make_consumer)
    events: list[str] = []
    ready = asyncio.Event()

    async def listen():
        async with app.state.resources.progress.subscribe(report_id) as sub:
            ready.set()
            while True:
                event = await sub.next_event(timeout=10)
                if event is None:
                    return
                events.append(event.status.value)
                if event.status.value == "COMPLETED":
                    return

    listener = asyncio.create_task(listen())
    await ready.wait()
    sse = asyncio.create_task(client.get(f"{API}/reports/{report_id}/events", headers=headers))
    await asyncio.sleep(0.3)
    assert len(await run_one(make_consumer(QueueName.GENERATION))) == 1
    await listener
    sse_resp = await asyncio.wait_for(sse, timeout=10)

    # Every status, in order, visible through pub/sub and SSE.
    seen = list(dict.fromkeys(events))
    assert seen == GEN_STATUSES
    sse_statuses = list(dict.fromkeys(re.findall(r'"status":"([A-Z_]+)"', sse_resp.text)))
    assert sse_statuses[-1] == "COMPLETED"
    assert set(GEN_STATUSES[1:]) <= set(sse_statuses)

    status = (await client.get(f"{API}/reports/{report_id}/status", headers=headers)).json()
    assert status["status"] == "COMPLETED" and status["progressPercent"] == 100
    body = (await client.get(f"{API}/reports/{report_id}", headers=headers)).json()
    headings = [s["heading"] for s in body["sections"]]
    assert headings[0] == "Introduction" and headings[-3:] == ["Discussion", "Limitations", "Conclusion"]
    assert body["abstract"] and body["keywords"] and body["title"]
    assert body["methodology"]["database"] == "OpenAlex"
    assert body["methodology"]["yearRange"].startswith("2010-")
    assert body["methodology"]["papersScreened"] >= body["methodology"]["papersIncluded"] > 0

    # Every in-text citation maps to a reference; every reference is a real (retrieved) work.
    refs = (await client.get(f"{API}/reports/{report_id}/references", headers=headers)).json()["items"]
    ref_ids = {r["id"] for r in refs}
    assert ref_ids and ref_ids <= set(fake_openalex.works)
    in_texts = {r["inTextCitation"].strip("()") for r in refs}
    text = "\n".join(s["content"] for s in body["sections"])
    assert "[W" not in text
    for group in re.findall(r"\(([^()]*\d{4}[a-z]?)\)", text):
        for cite in group.split("; "):
            assert cite in in_texts, f"citation {cite!r} has no reference"
    for ref in refs:  # and every reference is cited
        assert ref["inTextCitation"].strip("()") in text
    assert [r["apaCitation"] for r in refs] == sorted(r["apaCitation"] for r in refs)
    assert queue_depth(aws, GENERATION_QUEUE_URL) == 0

    # Export works on the generated report.
    pdf = await client.get(f"{API}/reports/{report_id}/export?format=pdf", headers=headers)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


# --------------------------------------------------------- resume / crash
async def test_crash_mid_job_resumes_from_checkpoint_without_repeating_work(
    app, client, headers, make_consumer, aws, fake_llm, fake_openalex
):
    report_id = await plan_and_approve(client, headers, make_consumer)
    original = fake_llm.handlers["extract"]
    crashes = {"n": 0}

    def crash_once(system, data):
        if crashes["n"] == 0:
            crashes["n"] += 1
            raise RuntimeError("worker killed")
        return original(system, data)

    fake_llm.set_handler("extract", crash_once)
    consumer = make_consumer(QueueName.GENERATION, worker="worker-a")
    messages = await consumer.receive(1, wait_seconds=0)
    outcome = await consumer.process(messages[0], uuid.UUID(report_id), JobPhase.GENERATE, 1)
    assert outcome is None  # unexpected crash: message left for SQS
    async with app.state.resources.db.session() as session:
        row = await session.get(Report, uuid.UUID(report_id))
    assert row.status == "EXTRACTING" and row.checkpoint_stage == "SCREENING"
    assert row.lease_owner is None  # released so the redelivery can resume at once
    searches_before = len([c for c in fake_openalex.calls if c[0] == "search"])
    screens_before = len([c for c in fake_llm.calls if c["task"] == "screen"])
    papers_before = await count(app, Paper, report_id)

    # A different worker picks up the redelivered message and resumes.
    resumed = make_consumer(QueueName.GENERATION, worker="worker-b")
    await asyncio.to_thread(
        aws["sqs"].change_message_visibility, QueueUrl=GENERATION_QUEUE_URL, ReceiptHandle=messages[0]["ReceiptHandle"], VisibilityTimeout=0
    )
    assert len(await run_one(resumed)) == 1
    async with app.state.resources.db.session() as session:
        row = await session.get(Report, uuid.UUID(report_id))
    assert row.status == "COMPLETED"
    assert len([c for c in fake_openalex.calls if c[0] == "search"]) == searches_before  # no re-search
    assert len([c for c in fake_llm.calls if c["task"] == "screen"]) == screens_before  # no re-screening
    assert await count(app, Paper, report_id) == papers_before
    assert await count(app, EvidenceCard, report_id) > 0
    assert queue_depth(aws, GENERATION_QUEUE_URL) == 0


async def test_crash_mid_stage_skips_finished_sub_questions(app, client, headers, make_consumer, aws, fake_llm, fake_openalex):
    report_id = await plan_and_approve(client, headers, make_consumer)
    original = fake_llm.handlers["search_terms"]
    seen = {"n": 0}

    def crash_on_third(system, data):
        seen["n"] += 1
        if seen["n"] == 3:
            raise RuntimeError("worker killed during search")
        return original(system, data)

    fake_llm.set_handler("search_terms", crash_on_third)
    consumer = make_consumer(QueueName.GENERATION)
    messages = await consumer.receive(1, wait_seconds=0)
    assert await consumer.process(messages[0], uuid.UUID(report_id), JobPhase.GENERATE, 1) is None
    async with app.state.resources.db.session() as session:
        saved = set((await session.execute(select(Paper.sub_question_id).distinct())).scalars().all())
    assert len(saved) == 2  # two sub-questions were saved before the crash
    asked = [c["user"] for c in fake_llm.calls if c["task"] == "search_terms"]

    fake_llm.set_handler("search_terms", original)
    await asyncio.to_thread(
        aws["sqs"].change_message_visibility, QueueUrl=GENERATION_QUEUE_URL, ReceiptHandle=messages[0]["ReceiptHandle"], VisibilityTimeout=0
    )
    assert len(await run_one(make_consumer(QueueName.GENERATION, worker="worker-b"))) == 1
    asked_after = [c["user"] for c in fake_llm.calls if c["task"] == "search_terms"][len(asked):]
    for done_prompt in asked[:2]:
        assert done_prompt not in asked_after  # finished sub-questions were not searched again
    status = (await client.get(f"{API}/reports/{report_id}/status", headers=headers)).json()
    assert status["status"] == "COMPLETED"


async def test_claim_respects_live_lease_and_takes_over_expired_one(app, client, headers, make_consumer):
    """A worker that died leaves a lease; another worker can claim only once it has expired."""
    from datetime import UTC, datetime, timedelta

    report_id = await plan_and_approve(client, headers, make_consumer)
    reports = app.state.resources.reports
    rid = uuid.UUID(report_id)
    await set_status(
        app, report_id, "SCREENING", checkpoint_stage="SEARCHING", lease_owner="dead-worker",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    assert await reports.claim(rid, JobPhase.GENERATE, "worker-x") is None  # still live
    await set_status(app, report_id, "SCREENING", lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    claimed = await reports.claim(rid, JobPhase.GENERATE, "worker-x")
    assert claimed is not None
    assert claimed.status == "SCREENING" and claimed.checkpoint_stage == "SEARCHING"
    assert claimed.lease_owner == "worker-x"


# --------------------------------------------------------------- duplicates
async def test_duplicate_message_is_skipped_while_job_is_live(app, client, headers, make_consumer, aws, fake_llm):
    report_id = await plan_and_approve(client, headers, make_consumer)
    # Worker A holds a live claim.
    assert await app.state.resources.reports.claim(uuid.UUID(report_id), JobPhase.GENERATE, "worker-a") is not None
    calls_before = len(fake_llm.calls)
    assert len(await run_one(make_consumer(QueueName.GENERATION, worker="worker-b"))) == 1
    assert len(fake_llm.calls) == calls_before  # no work done
    assert queue_depth(aws, GENERATION_QUEUE_URL) == 0  # duplicate deleted
    status = (await client.get(f"{API}/reports/{report_id}/status", headers=headers)).json()
    assert status["status"] == "SEARCHING"


async def test_message_for_finished_report_is_skipped(app, client, headers, make_consumer, aws):
    report = (await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)).json()
    await set_status(app, report["id"], "COMPLETED")
    assert len(await run_one(make_consumer(QueueName.PLANNING))) == 1
    assert queue_depth(aws, PLANNING_QUEUE_URL) == 0
    assert (await client.get(f"{API}/reports/{report['id']}/status", headers=headers)).json()["status"] == "COMPLETED"


async def test_invalid_message_is_discarded(app, make_consumer, aws):
    aws["sqs"].send_message(QueueUrl=PLANNING_QUEUE_URL, MessageBody="not json")
    aws["sqs"].send_message(QueueUrl=PLANNING_QUEUE_URL, MessageBody='{"reportId": "x", "phase": "PLAN"}')
    aws["sqs"].send_message(QueueUrl=PLANNING_QUEUE_URL, MessageBody=f'{{"reportId": "{uuid.uuid4()}", "phase": "GENERATE"}}')
    assert len(await run_one(make_consumer(QueueName.PLANNING))) == 3
    assert queue_depth(aws, PLANNING_QUEUE_URL) == 0


# ------------------------------------------------------------- cancellation
async def test_cancellation_mid_run_stops_the_job(app, client, headers, make_consumer, aws, fake_llm):
    report_id = await plan_and_approve(client, headers, make_consumer)
    cancelled = asyncio.Event()

    # The fake LLM is called inside the event loop, so schedule the cancel and let it run before the next stage.
    async def cancelling_complete(system, messages, model, max_tokens, schema):
        if "TASK: screen" in system and not cancelled.is_set():
            cancelled.set()
            await app.state.resources.reports.cancel_for_delete("user-1", uuid.UUID(report_id))
        return await original_complete(system, messages, model, max_tokens, schema)

    original_complete = fake_llm._complete
    fake_llm._complete = cancelling_complete
    assert len(await run_one(make_consumer(QueueName.GENERATION))) == 1
    status = (await client.get(f"{API}/reports/{report_id}/status", headers=headers)).json()
    assert status["status"] == "CANCELLED"
    assert await count(app, ReportSection, report_id) == 0
    assert await count(app, ScreeningResult, report_id) == 0  # stopped before saving screening
    assert not [c for c in fake_llm.calls if c["task"] in ("extract", "write_section")]
    assert queue_depth(aws, GENERATION_QUEUE_URL) == 0


async def test_deleted_report_stops_the_job(app, client, headers, make_consumer, aws, fake_llm):
    report_id = await plan_and_approve(client, headers, make_consumer)
    original_complete = fake_llm._complete

    async def delete_during_search(system, messages, model, max_tokens, schema):
        if "TASK: search_terms" in system:
            await client.delete(f"{API}/reports/{report_id}", headers=headers)
        return await original_complete(system, messages, model, max_tokens, schema)

    fake_llm._complete = delete_during_search
    assert len(await run_one(make_consumer(QueueName.GENERATION))) == 1
    assert queue_depth(aws, GENERATION_QUEUE_URL) == 0
    assert (await client.get(f"{API}/reports/{report_id}/status", headers=headers)).status_code == 404


# ---------------------------------------------------------------- failures
async def test_openalex_failure_sets_failed_retrieval(app, client, headers, make_consumer, aws, fake_openalex):
    report_id = await plan_and_approve(client, headers, make_consumer)
    fake_openalex.fail_with = OpenAlexError("OpenAlex request to /works failed (503)")
    assert len(await run_one(make_consumer(QueueName.GENERATION))) == 1
    status = (await client.get(f"{API}/reports/{report_id}/status", headers=headers)).json()
    assert status["status"] == "FAILED"
    assert status["error"]["code"] == "RETRIEVAL_FAILED"
    assert queue_depth(aws, GENERATION_QUEUE_URL) == 0


async def test_llm_unavailable_sets_failed(app, client, headers, make_consumer, aws, fake_llm):
    report = (await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)).json()

    def down(system, data):
        raise LLMUnavailable("LLM rate limit exceeded after retries")

    fake_llm.set_handler("plan", down)
    assert len(await run_one(make_consumer(QueueName.PLANNING))) == 1
    status = (await client.get(f"{API}/reports/{report['id']}/status", headers=headers)).json()
    assert status["status"] == "FAILED"
    assert status["error"] == {"code": "LLM_UNAVAILABLE", "message": "LLM rate limit exceeded after retries"}
    assert queue_depth(aws, PLANNING_QUEUE_URL) == 0


async def test_insufficient_sources_sets_failed(app, client, headers, make_consumer, aws, fake_llm):
    report_id = await plan_and_approve(client, headers, make_consumer)
    fake_llm.set_handler(
        "screen",
        lambda system, data: fake_handlers.ScreeningBatch(
            results=[fake_handlers.ScreeningScore(work_id=p["work_id"], score=0, reason="off topic") for p in data["papers"]]
        ),
    )
    assert len(await run_one(make_consumer(QueueName.GENERATION))) == 1
    status = (await client.get(f"{API}/reports/{report_id}/status", headers=headers)).json()
    assert status["status"] == "FAILED" and status["error"]["code"] == "INSUFFICIENT_SOURCES"


@pytest.mark.parametrize("settings_overrides", [{"SQS_MAX_RECEIVE_COUNT": 1}])
async def test_crash_on_last_delivery_marks_internal_error(app, client, headers, make_consumer, aws, fake_llm):
    report = (await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)).json()

    def boom(system, data):
        raise RuntimeError("bug")

    fake_llm.set_handler("plan", boom)
    assert len(await run_one(make_consumer(QueueName.PLANNING))) == 1
    status = (await client.get(f"{API}/reports/{report['id']}/status", headers=headers)).json()
    assert status["status"] == "FAILED" and status["error"]["code"] == "INTERNAL_ERROR"
    assert "bug" not in status["error"]["message"]  # no internals leak
    assert queue_depth(aws, PLANNING_QUEUE_URL) == 1  # left for SQS -> DLQ


# ---------------------------------------------------------------- heartbeat
@pytest.mark.parametrize("settings_overrides", [{"SQS_HEARTBEAT_SECONDS": 1}])
async def test_heartbeat_extends_visibility_and_lease(app, client, headers, make_consumer, fake_llm, monkeypatch):
    report = (await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)).json()
    original_complete = fake_llm._complete

    async def slow(system, messages, model, max_tokens, schema):
        await asyncio.sleep(2.5)
        return await original_complete(system, messages, model, max_tokens, schema)

    fake_llm._complete = slow
    consumer = make_consumer(QueueName.PLANNING)
    beats: list[int] = []
    real = consumer.sqs.change_message_visibility

    async def spy(**kwargs):
        beats.append(kwargs["VisibilityTimeout"])
        return await real(**kwargs)

    monkeypatch.setattr(consumer.sqs, "change_message_visibility", spy)
    leases: list[bool] = []
    real_extend = consumer.ctx.reports.extend_lease

    async def spy_extend(report_id, owner):
        leases.append(await real_extend(report_id, owner))
        return leases[-1]

    monkeypatch.setattr(consumer.ctx.reports, "extend_lease", spy_extend)
    assert len(await run_one(consumer)) == 1
    assert len(beats) >= 2 and set(beats) == {consumer.settings.SQS_VISIBILITY_TIMEOUT_SECONDS}
    assert leases and all(leases)
    assert (await client.get(f"{API}/reports/{report['id']}/status", headers=headers)).json()["status"] == "AWAITING_APPROVAL"


# --------------------------------------------------------- citation safety
async def test_unknown_citation_from_writer_never_reaches_the_report(
    app, client, headers, make_consumer, aws, fake_llm, fake_openalex
):
    report_id = await plan_and_approve(client, headers, make_consumer)
    original = fake_llm.handlers["write_section"]

    def hallucinate(system, data):
        draft = original(system, data)
        return SectionDraft(heading=draft.heading, content=draft.content + " A fabricated finding was reported [W999].")

    fake_llm.set_handler("write_section", hallucinate)
    assert len(await run_one(make_consumer(QueueName.GENERATION))) == 1
    body = (await client.get(f"{API}/reports/{report_id}", headers=headers)).json()
    refs = (await client.get(f"{API}/reports/{report_id}/references", headers=headers)).json()["items"]
    assert "W999" not in {r["id"] for r in refs}
    text = " ".join(s["content"] for s in body["sections"])
    assert "W999" not in text and "[W" not in text
    async with app.state.resources.db.session() as session:
        drafts = (await session.execute(select(ReportSection.draft_content))).scalars().all()
    assert any("[W999]" in d for d in drafts)  # it was in the draft, and the verifier removed it
