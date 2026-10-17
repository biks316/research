import asyncio
import json
import uuid

import pytest

from app.schemas import ProgressEvent
from app.services.progress import channel_for
from tests.helpers import set_status
from tests.test_reports_api import API, assert_problem


def parse_sse(text: str) -> tuple[list[dict], int]:
    events, keepalives = [], 0
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        if block.startswith(":"):
            keepalives += 1
            continue
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        assert lines["event"] == "progress"
        events.append(json.loads(lines["data"]))
    return events, keepalives


async def test_status_of_new_report(client, headers, create_report):
    report = await create_report()
    resp = await client.get(f"{API}/reports/{report['id']}/status", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "id": report["id"],
        "status": "QUEUED",
        "progressPercent": 0,
        "message": "Queued for planning",
        "updatedAt": body["updatedAt"],
        "error": None,
    }


async def test_status_of_failed_report_includes_error(app, client, headers, create_report):
    report = await create_report()
    await set_status(app, report["id"], "FAILED", error_code="INSUFFICIENT_SOURCES", error_message="Only 1 paper found")
    body = (await client.get(f"{API}/reports/{report['id']}/status", headers=headers)).json()
    assert body["error"] == {"code": "INSUFFICIENT_SOURCES", "message": "Only 1 paper found"}


async def test_status_unknown_report(client, headers):
    assert_problem(await client.get(f"{API}/reports/{uuid.uuid4()}/status", headers=headers), 404)


async def test_events_closes_immediately_for_terminal_status(app, client, headers, create_report):
    report = await create_report()
    await set_status(app, report["id"], "AWAITING_APPROVAL", progress_percent=10, message="Research plan ready")
    resp = await client.get(f"{API}/reports/{report['id']}/events", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events, _ = parse_sse(resp.text)
    assert len(events) == 1
    assert events[0]["status"] == "AWAITING_APPROVAL"
    assert events[0]["progressPercent"] == 10
    ProgressEvent.model_validate(events[0])


async def _publish_later(app, report_id: str, events: list[ProgressEvent], delay: float = 0.3):
    await asyncio.sleep(delay)
    redis = app.state.resources.redis
    for event in events:
        await redis.publish(channel_for(report_id), json.dumps(event.model_dump(mode="json", by_alias=True)))
        await asyncio.sleep(0.05)


async def test_events_sends_current_state_then_live_events_until_terminal(app, client, headers, create_report):
    report = await create_report()
    await set_status(app, report["id"], "SEARCHING", progress_percent=15, message="Searching")
    live = [
        ProgressEvent(status="SEARCHING", progress_percent=25, message="Searching OpenAlex for sub-question 2 of 5", timestamp="2026-10-17T19:10:05Z"),
        ProgressEvent(status="SCREENING", progress_percent=45, message="Screening 124 candidate papers", timestamp="2026-10-17T19:11:42Z"),
        ProgressEvent(status="COMPLETED", progress_percent=100, message="Report completed", timestamp="2026-10-17T19:20:00Z"),
        ProgressEvent(status="SEARCHING", progress_percent=1, message="never delivered", timestamp="2026-10-17T19:21:00Z"),
    ]
    publisher = asyncio.create_task(_publish_later(app, report["id"], live))
    resp = await asyncio.wait_for(client.get(f"{API}/reports/{report['id']}/events", headers=headers), timeout=10)
    await publisher
    events, _ = parse_sse(resp.text)
    assert [e["status"] for e in events] == ["SEARCHING", "SEARCHING", "SCREENING", "COMPLETED"]
    assert events[0]["progressPercent"] == 15
    assert events[2] == {
        "status": "SCREENING", "progressPercent": 45, "message": "Screening 124 candidate papers",
        "timestamp": "2026-10-17T19:11:42Z",
    }


@pytest.mark.parametrize("terminal", ["FAILED", "CANCELLED", "AWAITING_APPROVAL"])
async def test_events_close_on_each_terminal_status(app, client, headers, create_report, terminal):
    report = await create_report()
    await set_status(app, report["id"], "PLANNING")
    publisher = asyncio.create_task(
        _publish_later(app, report["id"], [ProgressEvent(status=terminal, progress_percent=10, timestamp="2026-10-17T19:10:05Z")])
    )
    resp = await asyncio.wait_for(client.get(f"{API}/reports/{report['id']}/events", headers=headers), timeout=10)
    await publisher
    events, _ = parse_sse(resp.text)
    assert events[-1]["status"] == terminal
    assert "message" not in events[-1]  # optional, not nullable: omitted


@pytest.mark.parametrize("settings_overrides", [{"SSE_KEEPALIVE_SECONDS": 0.2}])
async def test_events_send_keepalive_comments(app, client, headers, create_report):
    report = await create_report()
    await set_status(app, report["id"], "WRITING")
    publisher = asyncio.create_task(
        _publish_later(
            app, report["id"], [ProgressEvent(status="COMPLETED", progress_percent=100, timestamp="2026-10-17T19:10:05Z")], delay=1.0
        )
    )
    resp = await asyncio.wait_for(client.get(f"{API}/reports/{report['id']}/events", headers=headers), timeout=10)
    await publisher
    events, keepalives = parse_sse(resp.text)
    assert keepalives >= 2
    assert events[-1]["status"] == "COMPLETED"


async def test_events_stop_when_client_disconnects(app, create_report, headers):
    """Drive the ASGI app directly and send http.disconnect mid-stream."""
    report = await create_report()
    await set_status(app, report["id"], "SEARCHING")
    sent: list[dict] = []
    disconnect = asyncio.Event()
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            disconnect.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": f"{API}/reports/{report['id']}/events", "raw_path": b"", "query_string": b"", "root_path": "",
        "headers": [(b"authorization", headers["Authorization"].encode()), (b"host", b"test")],
        "client": ("127.0.0.1", 1), "server": ("test", 80), "state": {},
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    assert sent[0]["status"] == 200
    bodies = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"event: progress" in bodies
