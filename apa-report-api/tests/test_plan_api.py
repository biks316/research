import pytest

from tests.conftest import GENERATION_QUEUE_URL, drain
from tests.helpers import awaiting_approval, seed_plan, set_status
from tests.test_reports_api import API, assert_problem


def plan_body(version=1, subs=None, central="Does social media use cause anxiety in adolescents aged 13-19?"):
    return {
        "version": version,
        "centralQuestion": central,
        "subQuestions": subs
        if subs is not None
        else [
            {"id": "sq_seed1", "question": "What is known about teen social media use?", "role": "BACKGROUND"},
            {"question": "Is heavy use associated with anxiety symptoms?", "rationale": "Core link", "role": "CORE"},
        ],
    }


async def test_get_plan_before_ready_is_409(client, headers, create_report):
    report = await create_report()
    problem = assert_problem(await client.get(f"{API}/reports/{report['id']}/plan", headers=headers), 409, "REPORT_NOT_READY")
    assert "QUEUED" in problem["detail"]


async def test_get_plan_awaiting_approval_is_editable(app, client, headers, create_report):
    report = await create_report()
    await awaiting_approval(app, report["id"])
    resp = await client.get(f"{API}/reports/{report['id']}/plan", headers=headers)
    assert resp.status_code == 200
    plan = resp.json()
    assert plan["editable"] is True
    assert plan["version"] == 1
    assert plan["interpretationNote"].startswith("Interpreted")
    assert [s["order"] for s in plan["subQuestions"]] == [1, 2, 3]
    assert [s["role"] for s in plan["subQuestions"]] == ["BACKGROUND", "CORE", "DISCUSSION"]
    assert "rationale" in plan["subQuestions"][0]
    assert "rationale" not in plan["subQuestions"][1]  # optional, not nullable: omitted


async def test_get_plan_after_approval_is_read_only(app, client, headers, create_report):
    report = await create_report()
    await seed_plan(app, report["id"], locked=True)
    await set_status(app, report["id"], "WRITING")
    plan = (await client.get(f"{API}/reports/{report['id']}/plan", headers=headers)).json()
    assert plan["editable"] is False


async def test_update_plan_replaces_sub_questions_and_bumps_version(app, client, headers, create_report):
    report = await create_report()
    await awaiting_approval(app, report["id"])
    resp = await client.put(f"{API}/reports/{report['id']}/plan", json=plan_body(), headers=headers)
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["version"] == 2
    assert plan["centralQuestion"].endswith("13-19?")
    assert plan["subQuestions"][0]["id"] == "sq_seed1"
    assert plan["subQuestions"][1]["id"].startswith("sq_") and plan["subQuestions"][1]["id"] != "sq_seed2"
    assert [s["order"] for s in plan["subQuestions"]] == [1, 2]
    assert plan["subQuestions"][1]["rationale"] == "Core link"
    again = (await client.get(f"{API}/reports/{report['id']}/plan", headers=headers)).json()
    assert again == plan


async def test_update_plan_reorders_by_array_order(app, client, headers, create_report):
    report = await create_report()
    await awaiting_approval(app, report["id"])
    subs = [
        {"id": "sq_seed3", "question": "Seed sub-question number 3?", "role": "DISCUSSION"},
        {"id": "sq_seed1", "question": "Seed sub-question number 1?", "role": "BACKGROUND"},
    ]
    plan = (await client.put(f"{API}/reports/{report['id']}/plan", json=plan_body(subs=subs), headers=headers)).json()
    assert [(s["id"], s["order"]) for s in plan["subQuestions"]] == [("sq_seed3", 1), ("sq_seed1", 2)]


async def test_update_plan_version_conflict(app, client, headers, create_report):
    report = await create_report()
    await awaiting_approval(app, report["id"], version=3)
    problem = assert_problem(
        await client.put(f"{API}/reports/{report['id']}/plan", json=plan_body(version=2), headers=headers),
        409,
        "PLAN_VERSION_CONFLICT",
    )
    assert problem["detail"] == "Plan was modified (current version 3, request version 2). Reload and retry."


async def test_update_plan_wrong_status(app, client, headers, create_report):
    report = await create_report()
    await seed_plan(app, report["id"], locked=True)
    await set_status(app, report["id"], "SEARCHING")
    assert_problem(
        await client.put(f"{API}/reports/{report['id']}/plan", json=plan_body(), headers=headers), 409, "INVALID_REPORT_STATUS"
    )


async def test_update_plan_unknown_id_is_400(app, client, headers, create_report):
    report = await create_report()
    await awaiting_approval(app, report["id"])
    subs = [{"id": "sq_nope", "question": "A question that is long enough?", "role": "CORE"}]
    problem = assert_problem(
        await client.put(f"{API}/reports/{report['id']}/plan", json=plan_body(subs=subs), headers=headers), 400
    )
    assert problem["errors"][0]["field"] == "subQuestions[0].id"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"centralQuestion": "Long enough question?", "subQuestions": [{"question": "Long enough question?", "role": "CORE"}]}, "version"),
        (plan_body(central="short"), "centralQuestion"),
        (plan_body(subs=[]), "subQuestions"),
        (plan_body(subs=[{"question": "Long enough question?", "role": "CORE"}] * 9), "subQuestions"),
        (plan_body(subs=[{"question": "short", "role": "CORE"}]), "subQuestions[0].question"),
        (plan_body(subs=[{"question": "Long enough question?", "role": "OTHER"}]), "subQuestions[0].role"),
        (plan_body(subs=[{"question": "Long enough question?", "role": "CORE", "x": 1}]), "subQuestions[0].x"),
        (plan_body(version=0), "version"),
    ],
)
async def test_update_plan_validation(app, client, headers, create_report, body, field):
    report = await create_report()
    problem = assert_problem(await client.put(f"{API}/reports/{report['id']}/plan", json=body, headers=headers), 400)
    assert any(e["field"] == field for e in problem["errors"]), problem["errors"]


async def test_approve_plan_queues_generation(app, client, headers, create_report, aws):
    report = await create_report()
    await awaiting_approval(app, report["id"])
    resp = await client.post(f"{API}/reports/{report['id']}/plan/approve", json={"version": 1}, headers=headers)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["id"] == report["id"]
    assert body["status"] == "QUEUED_FOR_GENERATION"
    assert body["statusUrl"].endswith("/status")
    assert drain(aws["sqs"], GENERATION_QUEUE_URL) == [{"reportId": report["id"], "phase": "GENERATE"}]
    plan = (await client.get(f"{API}/reports/{report['id']}/plan", headers=headers)).json()
    assert plan["editable"] is False
    status = (await client.get(f"{API}/reports/{report['id']}/status", headers=headers)).json()
    assert status["status"] == "QUEUED_FOR_GENERATION"
    # A second approval is no longer allowed.
    assert_problem(
        await client.post(f"{API}/reports/{report['id']}/plan/approve", json={"version": 1}, headers=headers),
        409,
        "INVALID_REPORT_STATUS",
    )


async def test_approve_plan_version_conflict(app, client, headers, create_report):
    report = await create_report()
    await awaiting_approval(app, report["id"], version=2)
    assert_problem(
        await client.post(f"{API}/reports/{report['id']}/plan/approve", json={"version": 1}, headers=headers),
        409,
        "PLAN_VERSION_CONFLICT",
    )


async def test_approve_before_plan_ready_is_409(client, headers, create_report):
    report = await create_report()
    assert_problem(
        await client.post(f"{API}/reports/{report['id']}/plan/approve", json={"version": 1}, headers=headers),
        409,
        "INVALID_REPORT_STATUS",
    )


async def test_approve_validation(client, headers, create_report):
    report = await create_report()
    for body in ({}, {"version": 0}, {"version": 1, "extra": True}):
        assert_problem(await client.post(f"{API}/reports/{report['id']}/plan/approve", json=body, headers=headers), 400)


async def test_plan_endpoints_hide_other_users_reports(app, client, other_headers, create_report):
    report = await create_report()
    await awaiting_approval(app, report["id"])
    assert_problem(await client.put(f"{API}/reports/{report['id']}/plan", json=plan_body(), headers=other_headers), 404)
    assert_problem(
        await client.post(f"{API}/reports/{report['id']}/plan/approve", json={"version": 1}, headers=other_headers), 404
    )
