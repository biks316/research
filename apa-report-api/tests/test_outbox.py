from sqlalchemy import select

from app.models import Outbox
from tests.conftest import PLANNING_QUEUE_URL, VALID_BODY, drain
from tests.test_reports_api import API


async def test_report_is_not_stuck_when_sqs_is_down(app, client, headers, aws, monkeypatch):
    publisher = app.state.resources.publisher
    real_publish = publisher.publish

    async def broken(*args, **kwargs):
        raise ConnectionError("SQS unavailable")

    monkeypatch.setattr(publisher, "publish", broken)
    resp = await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)
    assert resp.status_code == 202  # the report is saved even though SQS is down
    async with app.state.resources.db.session() as session:
        row = (await session.execute(select(Outbox))).scalar_one()
    assert row.sent_at is None and row.attempts == 1 and "SQS unavailable" in row.last_error
    assert drain(aws["sqs"], PLANNING_QUEUE_URL) == []

    monkeypatch.setattr(publisher, "publish", real_publish)
    sent = await app.state.resources.dispatcher.relay_once(batch_size=10, older_than_seconds=0)
    assert sent == 1
    assert drain(aws["sqs"], PLANNING_QUEUE_URL) == [{"reportId": resp.json()["id"], "phase": "PLAN"}]
    async with app.state.resources.db.session() as session:
        row = (await session.execute(select(Outbox))).scalar_one()
    assert row.sent_at is not None
    # Nothing left to relay.
    assert await app.state.resources.dispatcher.relay_once(batch_size=10, older_than_seconds=0) == 0


async def test_relay_skips_rows_newer_than_grace_period(app, client, headers, aws, monkeypatch):
    publisher = app.state.resources.publisher

    async def broken(*args, **kwargs):
        raise ConnectionError("down")

    monkeypatch.setattr(publisher, "publish", broken)
    await client.post(f"{API}/reports", json=VALID_BODY, headers=headers)
    monkeypatch.undo()
    assert await app.state.resources.dispatcher.relay_once(batch_size=10, older_than_seconds=3600) == 0
