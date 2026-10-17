# APA Report Generator API

FastAPI service and background workers that turn a topic and description into a
citation-backed APA 7 research report. The API contract is [`openapi.yaml`](openapi.yaml)
(served as-is at `/openapi.json`).

```
client ──POST /reports──▶ API ──(DB txn: report + outbox)──▶ SQS planning ──▶ planning worker
   ▲                       │                                                     │ plan
   │  /status, /events ◀── Redis pub/sub ◀── progress ───────────────────────────┤
   │                       │                                                     ▼
   └──POST /plan/approve──▶ API ──(outbox)──▶ SQS generation ──▶ generation worker
                                                 search → screen → extract → write → verify → APA
```

## Stack

Python 3.12 · FastAPI/Uvicorn · Pydantic v2 + pydantic-settings · PostgreSQL + SQLAlchemy 2.0
(async, asyncpg) · Alembic · SQS + S3 via aioboto3 (LocalStack in development) · Redis
(idempotency, rate limits, progress pub/sub, OpenAlex cache) · httpx + tenacity (OpenAlex) ·
Anthropic SDK behind an `LLMClient` interface · PyJWT · citeproc-py (`apa` CSL) · python-docx ·
WeasyPrint · structlog · pytest, moto, fakeredis · uv.

## Setup

```bash
cp .env.example .env          # then edit .env (see below)
uv sync                       # creates .venv with app + dev dependencies
```

### `.env`

Every setting is read from the environment (or `.env`) by `app/config.py`. The app exits at
startup with a list of every missing or invalid key.

| Required | Notes |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `REDIS_URL` | |
| `SQS_PLANNING_QUEUE_URL`, `SQS_GENERATION_QUEUE_URL` | |
| `S3_EXPORTS_BUCKET` | |
| `JWT_ISSUER`, `JWT_AUDIENCE` | plus `JWT_SECRET` for HS256/384/512, or `JWT_JWKS_URL` for RS/ES/PS |

To generate real reports set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`, and set
`OPENALEX_API_KEY` (free at openalex.org; without it OpenAlex allows only a small shared daily
budget). `LLM_PROVIDER=fake` runs the whole pipeline offline-from-the-LLM with deterministic
outputs, which is useful for local development. Never commit `.env`.

## Running locally

### With Docker

```bash
docker compose up --build
```

This starts Postgres, Redis, LocalStack (the init script `scripts/localstack-init.sh` creates
`apa-planning` and `apa-generation`, each with a dead-letter queue, and the exports bucket), runs
`alembic upgrade head`, then starts the API on `http://localhost:8080` and one worker per queue.

```bash
TOKEN=$(uv run python -c "import jwt,time; print(jwt.encode({'sub':'me','iss':'https://auth.example.com/','aud':'apa-report-api','exp':int(time.time())+3600}, 'change-me-to-a-long-random-string', algorithm='HS256'))")
curl -i -X POST http://localhost:8080/api/v1/reports \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"topic":"Social media and teen mental health","description":"Does social media cause anxiety in teenagers?"}'
```

### Without Docker

Run Postgres, Redis and LocalStack (or real AWS) yourself, point `.env` at them, then:

```bash
uv run alembic upgrade head
uv run uvicorn app.main:get_app --factory --port 8080
uv run python -m app.workers.consumer --queue planning
uv run python -m app.workers.consumer --queue generation
```

`GET /health` is liveness; `GET /ready` checks the database, Redis and SQS (both outside the
API prefix and without auth).

## Tests

```bash
createdb apa_reports_test            # once; or set TEST_DATABASE_URL
uv run pytest
```

Tests use a real Postgres database (migrated with Alembic at session start, truncated between
tests), a moto server for SQS and S3, and fakeredis. The LLM and OpenAlex are mocked in worker
tests. Suites:

| File | Covers |
|---|---|
| `test_openapi_contract.py` | every spec path/method is served with its `operationId`; every Pydantic model matches its component (fields, required, enums, limits, nullability, `additionalProperties`); `/openapi.json` equals the spec |
| `test_reports_api.py` | create, list (cursor, filter), get, delete; 400 Problem details; 401 on every endpoint; 404 for other users; 409 not ready; 429 rate limit and active-report limit |
| `test_idempotency.py` | replay, body mismatch 409, in-progress 409, per-user keys, TTL |
| `test_plan_api.py` | plan get/edit/approve, version conflicts, wrong status, validation |
| `test_status_and_events.py` | `/status`, SSE initial state, live events, keep-alives, close on terminal statuses, client disconnect |
| `test_output_api.py` | references, `.docx`/`.pdf` export, S3 caching, headers |
| `test_outbox.py` | report not stuck when SQS is down; relay re-publishes |
| `test_worker_jobs.py` | full happy path through every status, resume from checkpoint, mid-stage resume, duplicate message skipped, cancellation and deletion mid-run, each failure code, heartbeat, `[W999]` removed |
| `test_clients.py`, `test_pipeline.py` | OpenAlex client (API key, batching, cache, retries, rate limit), LLM client (request shape, JSON retry, semaphore, usage), RRF, screening rules, verifier, APA formatting |

## Report lifecycle

```
QUEUED → PLANNING → AWAITING_APPROVAL → QUEUED_FOR_GENERATION
→ SEARCHING → SCREENING → EXTRACTING → WRITING → VERIFYING → COMPLETED
Any active status → FAILED (RETRIEVAL_FAILED | INSUFFICIENT_SOURCES | LLM_UNAVAILABLE | INTERNAL_ERROR)
Any non-terminal status → CANCELLED (DELETE /reports/{id})
```

- **Create** - one transaction inserts the report (`QUEUED`) and an outbox row; after commit the
  planning message is published and the row marked sent. A relay in the API lifespan re-publishes
  unsent rows every `OUTBOX_RELAY_INTERVAL_SECONDS`, so a report is never stuck if SQS was down.
- **Transitions** live in `app/services/report_service.py` as a table and are applied with
  `UPDATE ... WHERE id = :id AND status IN (:expected)`; workers add `AND lease_owner = :me`.
- **Workers** long-poll SQS, claim a job with a conditional update and a lease, and heartbeat
  (`ChangeMessageVisibility` + lease renewal) every `SQS_HEARTBEAT_SECONDS`. A message for a job
  that is already claimed or finished is deleted and skipped.
- **Checkpoints** - each stage saves its output and its checkpoint in one transaction; search,
  screening and extraction also save per sub-question. A retried job resumes after the last saved
  work. Status is checked between stages; a cancelled or deleted report stops the job.
- **Failures** - transient errors are retried inside the job (tenacity for OpenAlex, the SDK for
  the LLM). Permanent failures set `FAILED` with a code and delete the message. Unexpected crashes
  leave the message for SQS (moved to the DLQ after `maxReceiveCount`); on the last delivery the
  report is marked `FAILED / INTERNAL_ERROR` so it is not left hanging.
- **Citations** - the writer may only cite `[W123]` placeholders for works on its evidence cards;
  the verifier removes any id that was not retrieved and checks each cited sentence against the
  cards (rewriting or dropping unsupported claims); citeproc-py then turns placeholders into APA
  in-text citations and builds the reference list from OpenAlex metadata only.

## Assumptions

Recorded where the spec or the build prompt left a choice open.

**Inputs**
1. The provided files were named `fast api built prompt.md` and `myapa.yaml`; `myapa.yaml` is
   copied unchanged to `openapi.yaml`.
2. No `.env.example` was provided. It was written from the settings the build prompt names; the
   defaults and these additional keys are this project's choices: `PROBLEM_TYPE_BASE_URL`,
   `CORS_ALLOW_ORIGINS`, `DB_*`, `AWS_*`, `SQS_WAIT_TIME_SECONDS`, `SQS_VISIBILITY_TIMEOUT_SECONDS`,
   `SQS_MAX_RECEIVE_COUNT`, `S3_PRESIGN_TTL_SECONDS`, `JWT_LEEWAY_SECONDS`, `RATE_LIMIT_REQUESTS`,
   `RATE_LIMIT_WINDOW_SECONDS`, `ACTIVE_LIMIT_RETRY_AFTER_SECONDS`, `OUTBOX_RELAY_BATCH_SIZE`,
   `OPENALEX_MAILTO`, `OPENALEX_TIMEOUT_SECONDS`, `OPENALEX_PER_PAGE`, `LLM_DEFAULT_MODEL`,
   `LLM_EFFORT`, `LLM_MAX_RETRIES`, `MAX_CANDIDATES_PER_SUBQUESTION`, `RETRIEVAL_EXPANSION_SEEDS`,
   `MIN_INCLUDED_PAPERS`.

**Contract**
3. `/openapi.json` serves `openapi.yaml` verbatim, so the served contract cannot drift from the
   spec. The contract test separately checks the code against the spec.
4. Fields that are optional but not nullable in the spec are omitted when empty instead of sent
   as `null` (`SubQuestion.rationale`, `ProgressEvent.message`, `Report.abstract`/`keywords`,
   `Methodology.yearRange`, `Reference.isOpenAccess`).
5. A `reportId` that is not a UUID is a `400` validation error (`field: reportId`); an unknown or
   foreign id is `404 REPORT_NOT_FOUND`.
6. Problem `code` values beyond the spec's examples: `UNAUTHORIZED`, `REPORT_NOT_FOUND`,
   `NOT_FOUND`, `INVALID_REPORT_STATUS` (plan edit/approve outside `AWAITING_APPROVAL`),
   `IDEMPOTENCY_KEY_REUSED`, `IDEMPOTENCY_REQUEST_IN_PROGRESS` (same key while the first request is
   still running), `RATE_LIMITED`, `ACTIVE_REPORT_LIMIT`, `INTERNAL_ERROR`.
7. The SSE stream also closes on `CANCELLED` (the build prompt lists it; the spec's description
   does not). Because `AWAITING_APPROVAL` ends the stream, clients reconnect after approving.
8. A request body with valid JSON but `yearFrom > yearTo` is a `400` on field `options`.

**Plan**
9. `GET /plan` works once a plan exists and the report is past `PLANNING` (also for reports that
   later failed or were cancelled); `editable` is true only in `AWAITING_APPROVAL`.
10. `PUT /plan` keeps `interpretationNote` unchanged. A sub-question `id` that is not in the
    current plan, or appears twice, is a `400` on `subQuestions[i].id`.
11. For approve and edit, a wrong status is reported before a version mismatch.

**Limits and idempotency**
12. The per-user rate limit is a fixed window (`RATE_LIMIT_REQUESTS` per
    `RATE_LIMIT_WINDOW_SECONDS`) on `POST /reports` only. The active-report limit counts all
    non-terminal statuses, including `AWAITING_APPROVAL`, and returns
    `Retry-After: ACTIVE_LIMIT_RETRY_AFTER_SECONDS`.
13. Idempotency is checked before the rate limit (replays are free). The body hash is over the
    validated body with unset fields excluded, so key order does not matter. A failed request
    (4xx/5xx) releases the key so the client can retry with it.

**Outputs**
14. Cursor = base64url of `{createdAt, id}`; an invalid cursor is a `400` on `cursor`.
15. Export keys are `exports/{reportId}/{contentVersion}.{ext}`; `contentVersion` starts at 1. The
    S3 object itself is the cache. The file name comes from the title, with an ASCII
    `filename` and an RFC 5987 `filename*`.
16. The reference list contains only works cited in the final text (APA rule). Same author(s) and
    year get `a`/`b` suffixes, ordered by title, because citeproc-py does not disambiguate.
17. `methodology.papersScreened` = distinct works screened; `papersIncluded` = distinct works with
    evidence cards. Without `yearFrom` no year filter is applied and `yearRange` is omitted; with
    it, `yearTo` defaults to the current year.
18. The abstract contains no citations.

**Workers and pipeline**
19. A worker's lease lasts `2 × SQS_HEARTBEAT_SECONDS + 10` seconds and must be shorter than
    `SQS_VISIBILITY_TIMEOUT_SECONDS` (validated at startup), so a killed worker's lease has expired
    by the time SQS redelivers its message. `SQS_MAX_RECEIVE_COUNT` must match the queues'
    `maxReceiveCount`.
20. Writing and verification are redone as a whole if interrupted (search, screening and
    extraction resume per sub-question).
21. OpenAlex semantic search uses `GET /works?search.semantic=...`, which accepts the same filters
    and returns at most 50 results. The `/find/works` endpoint mentioned in some OpenAlex docs
    returned 404 when tested on 2026-10-17. The API key is sent as the `api_key` query parameter.
22. A "strong" candidate is one found by both the keyword and semantic searches, by one search
    plus a seed link, or linked to two or more seeds. Each seed link adds `1/(60+1)` to its RRF score.
23. Screening score 1 is kept only for `BACKGROUND` sub-questions (stored as `background_only`).
    Fewer than `MIN_INCLUDED_PAPERS` included works fails with `INSUFFICIENT_SOURCES`, as does a
    draft with no supportable citation left after verification.
24. LLM provider `anthropic`: `claude-opus-5-5` for planning/extraction/verification and writing,
    `claude-haiku-4-5` for screening. Opus 5.5 rejects sampling parameters, so depth is set with
    `LLM_EFFORT` and `LLM_TEMPERATURE` is sent only to Haiku. Structured JSON outputs are validated
    with Pydantic and retried once with the validation error. Rate limits, timeouts and 5xx are
    retried by the SDK (`LLM_MAX_RETRIES`); the server-side refusal fallback (`fallbacks: "default"`)
    is enabled for Opus. After retries, LLM errors fail the report with `LLM_UNAVAILABLE`.
25. Token usage is logged per job and accumulated in `reports.token_usage`.
