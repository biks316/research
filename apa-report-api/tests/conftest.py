"""Test fixtures: Postgres (migrated with Alembic), moto (SQS + S3), fakeredis, JWTs."""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import boto3
import fakeredis
import jwt
import pytest
import pytest_asyncio
import requests
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from moto.server import ThreadedMotoServer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://localhost/apa_reports_test")
JWT_SECRET = "test-secret-not-for-production-0123456789"
JWT_ISSUER = "https://auth.test/"
JWT_AUDIENCE = "apa-report-api"
ACCOUNT = "123456789012"
TABLES = (
    "report_references, report_sections, evidence_cards, screening_results, papers, outbox, sub_questions, plans, reports"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


MOTO_PORT = _free_port()
MOTO_ENDPOINT = f"http://127.0.0.1:{MOTO_PORT}"
PLANNING_QUEUE_URL = f"{MOTO_ENDPOINT}/{ACCOUNT}/apa-planning"
GENERATION_QUEUE_URL = f"{MOTO_ENDPOINT}/{ACCOUNT}/apa-generation"
BUCKET = "apa-report-exports-test"

TEST_ENV = {
    "APP_ENV": "test",
    "LOG_LEVEL": "WARNING",
    "DATABASE_URL": TEST_DATABASE_URL,
    "REDIS_URL": "redis://fake:6379/0",
    "AWS_REGION": "us-east-1",
    "AWS_ENDPOINT_URL": MOTO_ENDPOINT,
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "SQS_PLANNING_QUEUE_URL": PLANNING_QUEUE_URL,
    "SQS_GENERATION_QUEUE_URL": GENERATION_QUEUE_URL,
    "SQS_VISIBILITY_TIMEOUT_SECONDS": "60",
    "SQS_HEARTBEAT_SECONDS": "10",
    "S3_EXPORTS_BUCKET": BUCKET,
    "JWT_ALGORITHM": "HS256",
    "JWT_SECRET": JWT_SECRET,
    "JWT_JWKS_URL": "",
    "JWT_ISSUER": JWT_ISSUER,
    "JWT_AUDIENCE": JWT_AUDIENCE,
    "LLM_PROVIDER": "fake",
    "ANTHROPIC_API_KEY": "",
    "OPENALEX_API_KEY": "test-openalex-key",
    "OPENALEX_CACHE_TTL_SECONDS": "60",
    "RATE_LIMIT_REQUESTS": "100",
    "MAX_ACTIVE_REPORTS_PER_USER": "50",
    "SSE_KEEPALIVE_SECONDS": "15",
}
os.environ.update(TEST_ENV)

from app.config import Settings  # noqa: E402  (after env is set)


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


# --------------------------------------------------------------- moto (AWS)
@pytest.fixture(scope="session")
def moto_server():
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=MOTO_PORT, verbose=False)
    server.start()
    yield server
    server.stop()


def _aws(service: str):
    return boto3.client(
        service, endpoint_url=MOTO_ENDPOINT, region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
    )


@pytest.fixture
def aws(moto_server):
    """Fresh queues (with DLQs) and bucket for every test."""
    requests.post(f"{MOTO_ENDPOINT}/moto-api/reset", timeout=10)
    sqs, s3 = _aws("sqs"), _aws("s3")
    for name in ("apa-planning", "apa-generation"):
        dlq = sqs.create_queue(QueueName=f"{name}-dlq")["QueueUrl"]
        dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        sqs.create_queue(
            QueueName=name,
            Attributes={
                "VisibilityTimeout": "60",
                "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"}),
            },
        )
    s3.create_bucket(Bucket=BUCKET)
    return {"sqs": sqs, "s3": s3}


def drain(sqs, queue_url: str) -> list[dict[str, Any]]:
    """Receive and delete every visible message; return the parsed bodies."""
    bodies = []
    while True:
        resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0)
        messages = resp.get("Messages", [])
        if not messages:
            return bodies
        for m in messages:
            bodies.append(json.loads(m["Body"]))
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"])


# ------------------------------------------------------------------ database
@pytest.fixture(scope="session")
def migrated_db():
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return TEST_DATABASE_URL


@pytest_asyncio.fixture(loop_scope="session")
async def clean_db(migrated_db) -> AsyncIterator[None]:
    engine = create_async_engine(migrated_db)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    await engine.dispose()
    yield


# --------------------------------------------------------------------- redis
@pytest.fixture
def fake_redis_server(monkeypatch):
    server = fakeredis.FakeServer()

    def build(settings):
        return fakeredis.FakeAsyncRedis(server=server, decode_responses=True)

    monkeypatch.setattr("app.core.resources.build_redis", build)
    return server


# ----------------------------------------------------------------------- app
@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {}


@pytest.fixture
def settings(settings_overrides) -> Settings:
    return make_settings(**settings_overrides)


@pytest_asyncio.fixture(loop_scope="session")
async def app(settings, aws, clean_db, fake_redis_server):
    from app.main import create_app

    application = create_app(settings, start_outbox_relay=False)
    async with LifespanManager(application, startup_timeout=30, shutdown_timeout=30):
        yield application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def make_token(sub: str = "user-1", **claims: Any) -> str:
    now = int(time.time())
    payload = {"sub": sub, "iss": JWT_ISSUER, "aud": JWT_AUDIENCE, "iat": now, "exp": now + 3600, **claims}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def auth(sub: str = "user-1") -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub)}"}


@pytest.fixture
def headers() -> dict[str, str]:
    return auth("user-1")


@pytest.fixture
def other_headers() -> dict[str, str]:
    return auth("user-2")


VALID_BODY = {
    "topic": "Social media and teen mental health",
    "description": "For my psychology class. I want to know if social media causes anxiety in teenagers.",
    "options": {"yearFrom": 2010, "length": "STANDARD", "openAccessOnly": False},
}


@pytest.fixture
def create_report(client, headers) -> Callable[..., Any]:
    async def _create(body: dict[str, Any] | None = None, hdrs: dict[str, str] | None = None) -> dict[str, Any]:
        resp = await client.post("/api/v1/reports", json=body or VALID_BODY, headers=hdrs or headers)
        assert resp.status_code == 202, resp.text
        return resp.json()

    return _create


def new_id() -> str:
    return str(uuid.uuid4())
