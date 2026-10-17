import fakeredis
import pytest

from app.core.errors import IdempotencyConflict
from app.services.idempotency import IdempotencyInProgress, IdempotencyStore, StoredResponse, body_hash
from tests.conftest import PLANNING_QUEUE_URL, VALID_BODY, drain
from tests.test_reports_api import API, assert_problem


@pytest.fixture
def store():
    return IdempotencyStore(fakeredis.FakeAsyncRedis(decode_responses=True), ttl_seconds=60)


def test_body_hash_is_order_independent():
    assert body_hash({"a": 1, "b": {"c": 2, "d": 3}}) == body_hash({"b": {"d": 3, "c": 2}, "a": 1})
    assert body_hash({"a": 1}) != body_hash({"a": 2})


async def test_store_replay_and_conflict(store):
    assert await store.begin("u1", "k", "h1") is None
    with pytest.raises(IdempotencyInProgress):
        await store.begin("u1", "k", "h1")
    await store.complete("u1", "k", "h1", StoredResponse(202, {"id": "x"}, {"Location": "/r/x"}))
    replay = await store.begin("u1", "k", "h1")
    assert replay == StoredResponse(202, {"id": "x"}, {"Location": "/r/x"})
    with pytest.raises(IdempotencyConflict):
        await store.begin("u1", "k", "h2")
    assert await store.begin("u2", "k", "h2") is None


async def test_store_ttl_is_applied(store):
    await store.begin("u1", "k", "h1")
    ttl = await store._redis.ttl(store._key("u1", "k"))
    assert 0 < ttl <= 60


async def test_release_allows_retry(store):
    await store.begin("u1", "k", "h1")
    await store.release("u1", "k")
    assert await store.begin("u1", "k", "h1") is None


async def test_idempotent_replay_returns_original_response(client, headers, aws):
    hdrs = {**headers, "Idempotency-Key": "key-123"}
    first = await client.post(f"{API}/reports", json=VALID_BODY, headers=hdrs)
    second = await client.post(f"{API}/reports", json=VALID_BODY, headers=hdrs)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert second.headers["location"] == first.headers["location"]
    assert len(drain(aws["sqs"], PLANNING_QUEUE_URL)) == 1


async def test_idempotency_key_reuse_with_different_body_is_409(client, headers):
    hdrs = {**headers, "Idempotency-Key": "key-456"}
    assert (await client.post(f"{API}/reports", json=VALID_BODY, headers=hdrs)).status_code == 202
    other = {**VALID_BODY, "topic": "A different topic"}
    assert_problem(await client.post(f"{API}/reports", json=other, headers=hdrs), 409, "IDEMPOTENCY_KEY_REUSED")


async def test_idempotency_keys_are_per_user(client, headers, other_headers):
    a = await client.post(f"{API}/reports", json=VALID_BODY, headers={**headers, "Idempotency-Key": "shared"})
    b = await client.post(f"{API}/reports", json=VALID_BODY, headers={**other_headers, "Idempotency-Key": "shared"})
    assert a.status_code == b.status_code == 202
    assert a.json()["id"] != b.json()["id"]


async def test_failed_request_releases_idempotency_key(client, headers):
    hdrs = {**headers, "Idempotency-Key": "retry-me"}
    bad = await client.post(f"{API}/reports", json={"topic": "x"}, headers=hdrs)
    assert bad.status_code == 400
    assert (await client.post(f"{API}/reports", json=VALID_BODY, headers=hdrs)).status_code == 202


async def test_idempotency_key_too_long_is_400(client, headers):
    resp = await client.post(f"{API}/reports", json=VALID_BODY, headers={**headers, "Idempotency-Key": "k" * 129})
    problem = assert_problem(resp, 400, "VALIDATION_ERROR")
    assert problem["errors"][0]["field"] == "Idempotency-Key"


