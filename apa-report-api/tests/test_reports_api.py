import time

import jwt
import pytest

from tests.conftest import (
    JWT_AUDIENCE,
    JWT_ISSUER,
    JWT_SECRET,
    PLANNING_QUEUE_URL,
    VALID_BODY,
    auth,
    drain,
    make_token,
    new_id,
)
from tests.helpers import complete, set_status

API = "/api/v1"
PROBLEM = "application/problem+json"


def assert_problem(resp, status: int, code: str | None = None):
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith(PROBLEM)
    body = resp.json()
    for field in ("type", "title", "status", "detail", "instance", "code"):
        assert field in body, f"problem missing {field}"
    assert body["status"] == status
    if code:
        assert body["code"] == code
    assert "Traceback" not in resp.text
    return body


# ----------------------------------------------------------------- create
async def test_create_report_returns_202_and_queues_planning(client, headers, aws):
    resp = await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "QUEUED"
    assert body["statusUrl"] == f"{API}/reports/{body['id']}/status"
    assert body["createdAt"].endswith("Z") or "+00:00" in body["createdAt"]
    assert resp.headers["location"] == f"{API}/reports/{body['id']}"
    assert resp.headers["x-request-id"]
    messages = drain(aws["sqs"], PLANNING_QUEUE_URL)
    assert messages == [{"reportId": body["id"], "phase": "PLAN"}]


async def test_create_marks_outbox_sent(app, client, headers):
    from sqlalchemy import select

    from app.models import Outbox

    resp = await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)
    async with app.state.resources.db.session() as session:
        rows = (await session.execute(select(Outbox))).scalars().all()
    assert len(rows) == 1
    assert str(rows[0].report_id) == resp.json()["id"]
    assert rows[0].sent_at is not None


async def test_create_with_minimal_body(client, headers):
    resp = await client.post(f"{API}/reports", json={"topic": "Sleep", "description": "Effects of sleep on memory"}, headers=headers)
    assert resp.status_code == 202


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"topic": "Sleep", "description": "short"}, "description"),
        ({"topic": "ab", "description": "long enough description"}, "topic"),
        ({"description": "long enough description"}, "topic"),
        ({"topic": "Sleep", "description": "long enough description", "extra": 1}, "extra"),
        ({"topic": "Sleep", "description": "long enough description", "options": {"yearFrom": 1800}}, "options.yearFrom"),
        ({"topic": "Sleep", "description": "long enough description", "options": {"length": "HUGE"}}, "options.length"),
        ({"topic": "Sleep", "description": "long enough description", "options": {"bogus": True}}, "options.bogus"),
        ({"topic": "Sleep", "description": "long enough description", "options": {"citationStyle": "MLA"}}, "options.citationStyle"),
        ({"topic": "Sleep", "description": "long enough description", "options": {"yearFrom": 2020, "yearTo": 2010}}, "options"),
        ({"topic": "Sleep", "description": "x" * 4001}, "description"),
    ],
)
async def test_create_validation_errors_are_400_problems(client, headers, body, field):
    resp = await client.post(f"{API}/reports", json=body, headers=headers)
    problem = assert_problem(resp, 400, "VALIDATION_ERROR")
    assert problem["title"] == "Validation failed"
    assert any(e["field"] == field for e in problem["errors"]), problem["errors"]


async def test_validation_message_format(client, headers):
    resp = await client.post(f"{API}/reports", json={"topic": "Sleep", "description": "short"}, headers=headers)
    problem = resp.json()
    assert problem["errors"] == [{"field": "description", "message": "must be at least 10 characters"}]
    assert problem["detail"] == "description must be at least 10 characters"
    assert problem["type"] == "https://api.example.com/problems/validation-error"
    assert problem["instance"] == f"{API}/reports"


async def test_malformed_json_is_400(client, headers):
    resp = await client.post(
        f"{API}/reports", content=b"{not json", headers={**headers, "Content-Type": "application/json"}
    )
    assert_problem(resp, 400, "VALIDATION_ERROR")


@pytest.mark.parametrize(
    "hdrs",
    [
        {},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": f"Bearer {make_token(exp=int(time.time()) - 3600)}"},
        {"Authorization": f"Bearer {make_token(aud='someone-else')}"},
        {"Authorization": f"Bearer {make_token(iss='https://evil/')}"},
        {"Authorization": "Bearer " + jwt.encode({"sub": "u", "iss": JWT_ISSUER, "aud": JWT_AUDIENCE, "exp": int(time.time()) + 60}, "wrong-secret-0123456789012345678901", algorithm="HS256")},
        {"Authorization": "Bearer " + jwt.encode({"iss": JWT_ISSUER, "aud": JWT_AUDIENCE, "exp": int(time.time()) + 60}, JWT_SECRET, algorithm="HS256")},
    ],
)
async def test_create_requires_valid_token(client, hdrs):
    resp = await client.post(f"{API}/reports", json=VALID_BODY, headers=hdrs)
    assert_problem(resp, 401, "UNAUTHORIZED")
    assert resp.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/reports"),
        ("get", "/reports/{id}"),
        ("delete", "/reports/{id}"),
        ("get", "/reports/{id}/status"),
        ("get", "/reports/{id}/events"),
        ("get", "/reports/{id}/plan"),
        ("put", "/reports/{id}/plan"),
        ("post", "/reports/{id}/plan/approve"),
        ("get", "/reports/{id}/references"),
        ("get", "/reports/{id}/export?format=pdf"),
    ],
)
async def test_every_endpoint_requires_auth(client, method, path):
    resp = await client.request(method.upper(), API + path.format(id=new_id()))
    assert_problem(resp, 401, "UNAUTHORIZED")


# ------------------------------------------------------------- rate limit
@pytest.mark.parametrize("settings_overrides", [{"RATE_LIMIT_REQUESTS": 2, "RATE_LIMIT_WINDOW_SECONDS": 60}])
async def test_rate_limit_returns_429_with_retry_after(client, headers):
    for _ in range(2):
        assert (await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)).status_code == 202
    resp = await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)
    assert_problem(resp, 429, "RATE_LIMITED")
    assert 1 <= int(resp.headers["retry-after"]) <= 61
    # Other users are unaffected.
    assert (await client.post(f"{API}/reports", json=VALID_BODY, headers=auth("user-9"))).status_code == 202


@pytest.mark.parametrize("settings_overrides", [{"MAX_ACTIVE_REPORTS_PER_USER": 2}])
async def test_active_report_limit_returns_429(app, client, headers):
    first = (await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)).json()
    await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)
    resp = await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)
    assert_problem(resp, 429, "ACTIVE_REPORT_LIMIT")
    assert resp.headers["retry-after"] == "60"
    await set_status(app, first["id"], "COMPLETED")
    assert (await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)).status_code == 202


# ------------------------------------------------------------------- list
async def test_list_reports_paginates_newest_first(client, headers, other_headers, create_report):
    ids = [(await create_report())["id"] for _ in range(5)]
    await create_report(hdrs=other_headers)
    page1 = await client.get(f"{API}/reports", params={"limit": 2}, headers=headers)
    assert page1.status_code == 200
    body1 = page1.json()
    assert [i["id"] for i in body1["items"]] == ids[::-1][:2]
    assert body1["nextCursor"]
    page2 = (await client.get(f"{API}/reports", params={"limit": 2, "cursor": body1["nextCursor"]}, headers=headers)).json()
    page3 = (await client.get(f"{API}/reports", params={"limit": 2, "cursor": page2["nextCursor"]}, headers=headers)).json()
    assert [i["id"] for i in page2["items"]] == ids[::-1][2:4]
    assert [i["id"] for i in page3["items"]] == ids[::-1][4:]
    assert page3["nextCursor"] is None
    item = body1["items"][0]
    assert set(item) == {"id", "topic", "title", "status", "createdAt", "updatedAt"}
    assert item["title"] is None


async def test_list_reports_status_filter(app, client, headers, create_report):
    a = await create_report()
    await create_report()
    await set_status(app, a["id"], "FAILED")
    body = (await client.get(f"{API}/reports", params={"status": "FAILED"}, headers=headers)).json()
    assert [i["id"] for i in body["items"]] == [a["id"]]


@pytest.mark.parametrize(
    ("params", "field"),
    [({"limit": 0}, "limit"), ({"limit": 101}, "limit"), ({"status": "NOPE"}, "status"), ({"cursor": "%%%"}, "cursor")],
)
async def test_list_reports_validation(client, headers, params, field):
    problem = assert_problem(await client.get(f"{API}/reports", params=params, headers=headers), 400, "VALIDATION_ERROR")
    assert problem["errors"][0]["field"] == field


# ------------------------------------------------------------------- read
async def test_get_report_not_ready_is_409(client, headers, create_report):
    report = await create_report()
    problem = assert_problem(await client.get(f"{API}/reports/{report['id']}", headers=headers), 409, "REPORT_NOT_READY")
    assert problem["detail"] == "Report is in status QUEUED; this resource is available when COMPLETED."


async def test_get_completed_report(app, client, headers, create_report):
    report = await create_report()
    await complete(app, report["id"])
    resp = await client.get(f"{API}/reports/{report['id']}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["title"].startswith("Social Media")
    assert body["centralQuestion"] == "Does social media use cause anxiety in adolescents?"
    assert [s["heading"] for s in body["sections"]] == ["Introduction", "Usage and Anxiety"]
    assert body["sections"][0]["subQuestionId"] is None
    assert [r["id"] for r in body["references"]] == ["W1", "W2"]
    assert body["methodology"] == {
        "database": "OpenAlex", "searchedAt": "2026-10-17T19:10:00Z", "yearRange": "2010-2026",
        "papersScreened": 40, "papersIncluded": 2,
    }
    assert body["keywords"] == ["social media", "anxiety"]
    assert body["completedAt"]


async def test_other_users_report_is_404(client, other_headers, create_report):
    report = await create_report()
    for path in ("", "/status", "/plan", "/references", "/events", "/export?format=docx"):
        assert_problem(await client.get(f"{API}/reports/{report['id']}{path}", headers=other_headers), 404, "REPORT_NOT_FOUND")
    assert_problem(await client.delete(f"{API}/reports/{report['id']}", headers=other_headers), 404)


async def test_unknown_and_malformed_ids(client, headers):
    assert_problem(await client.get(f"{API}/reports/{new_id()}", headers=headers), 404, "REPORT_NOT_FOUND")
    problem = assert_problem(await client.get(f"{API}/reports/not-a-uuid", headers=headers), 400, "VALIDATION_ERROR")
    assert problem["errors"][0]["field"] == "reportId"


async def test_unknown_route_is_problem_404(client, headers):
    assert_problem(await client.get("/api/v1/nope", headers=headers), 404, "NOT_FOUND")


# ----------------------------------------------------------------- delete
async def test_delete_active_report_cancels_then_deletes(app, client, headers, create_report):
    report = await create_report()
    await set_status(app, report["id"], "SEARCHING")
    events = []

    async def capture():
        async with app.state.resources.progress.subscribe(report["id"]) as sub:
            ready.set()
            events.append(await sub.next_event(timeout=5))

    import asyncio

    ready = asyncio.Event()
    task = asyncio.create_task(capture())
    await ready.wait()
    resp = await client.delete(f"{API}/reports/{report['id']}", headers=headers)
    assert resp.status_code == 204
    assert resp.content == b""
    await task
    assert events[0].status == "CANCELLED"
    assert_problem(await client.get(f"{API}/reports/{report['id']}/status", headers=headers), 404)


async def test_delete_removes_children_and_exports(app, client, headers, create_report, aws):
    from sqlalchemy import func, select

    from app.models import ReportReference, ReportSection, SubQuestion
    from tests.conftest import BUCKET

    report = await create_report()
    await complete(app, report["id"])
    aws["s3"].put_object(Bucket=BUCKET, Key=f"exports/{report['id']}/1.pdf", Body=b"%PDF")
    assert (await client.delete(f"{API}/reports/{report['id']}", headers=headers)).status_code == 204
    async with app.state.resources.db.session() as session:
        for model in (ReportSection, ReportReference, SubQuestion):
            assert (await session.execute(select(func.count()).select_from(model))).scalar_one() == 0
    assert aws["s3"].list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0
