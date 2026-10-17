# Prompt: Build the FastAPI backend for the APA Report Generator

## Role

You are a senior Python backend engineer. Build a production-quality FastAPI service that implements the attached OpenAPI specification (`openapi.yaml`) exactly, plus the background workers that generate the reports.

## Context

The product generates citation-backed APA 7 research reports. A user submits a topic and description. The API saves the request, queues it on AWS SQS, and returns a report ID immediately with `202 Accepted`. Workers then:

1. **Planning phase** (short): turn the topic into a central question and 3–6 sub-questions, then set status `AWAITING_APPROVAL`.
2. The user reviews, optionally edits, and approves the plan.
3. **Generation phase** (long, several minutes): search OpenAlex, screen papers, extract evidence, write the report, verify citations, format APA 7, set status `COMPLETED`.

The client follows progress by polling `/status` or listening on `/events` (Server-Sent Events), then fetches the report, references, or a .docx/.pdf export.

## Inputs

- `openapi.yaml`: the API contract. **It is the source of truth.** Paths, methods, request and response schemas, status codes, and error formats must match it exactly.
- `.env.example`: all configuration keys. Read every setting from environment variables; hard-code nothing.

## Tech stack (use exactly these)

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Web framework | FastAPI, served by Uvicorn |
| Validation and settings | Pydantic v2, `pydantic-settings` |
| Database | PostgreSQL with SQLAlchemy 2.0 (async, `asyncpg`) |
| Migrations | Alembic |
| Queue | AWS SQS via `aioboto3` (LocalStack in development) |
| Cache, idempotency keys, progress pub/sub | Redis (`redis.asyncio`) |
| File storage for exports | AWS S3 via `aioboto3` |
| HTTP client (OpenAlex) | `httpx.AsyncClient` with retries (`tenacity`) |
| LLM | Provider SDK selected by `LLM_PROVIDER`; wrap it behind an interface |
| Auth | JWT bearer tokens via `PyJWT` |
| APA formatting | `citeproc-py` with the `apa` CSL style |
| Exports | `python-docx` for .docx; `weasyprint` (from HTML) for .pdf |
| Logging | `structlog` with JSON output |
| Tests | `pytest`, `pytest-asyncio`, `httpx` test client, `moto` for AWS |
| Packaging | `uv` with `pyproject.toml` |
| Containers | `Dockerfile` and `docker-compose.yml` (api, worker, postgres, redis, localstack) |

## Project structure

```
apa-report-api/
├── pyproject.toml
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── alembic.ini
├── migrations/
├── openapi.yaml
├── app/
│   ├── main.py                 # FastAPI app, routers, exception handlers, lifespan
│   ├── config.py               # Settings (pydantic-settings) loaded from .env
│   ├── api/
│   │   ├── deps.py             # DB session, current user, settings dependencies
│   │   ├── reports.py          # POST/GET /reports, GET/DELETE /reports/{id}
│   │   ├── progress.py         # /status and /events (SSE)
│   │   ├── plan.py             # GET/PUT /plan, POST /plan/approve
│   │   └── output.py           # /references, /export
│   ├── schemas/                # Pydantic models mirroring openapi.yaml components
│   ├── models/                 # SQLAlchemy ORM models
│   ├── repositories/           # Data access; no SQL in routers
│   ├── services/
│   │   ├── report_service.py   # Business rules and status transitions
│   │   ├── queue.py            # SQS publisher
│   │   ├── idempotency.py      # Idempotency-Key handling (Redis)
│   │   ├── progress.py         # Publish and subscribe progress (Redis pub/sub)
│   │   └── storage.py          # S3 upload and presigned download
│   ├── core/
│   │   ├── security.py         # JWT verification
│   │   ├── errors.py           # Problem Details exceptions and handlers
│   │   └── logging.py
│   ├── clients/
│   │   ├── openalex.py         # OpenAlex API client
│   │   └── llm.py              # LLM interface and provider implementation
│   ├── pipeline/
│   │   ├── planner.py          # Topic → central question and sub-questions
│   │   ├── retriever.py        # Keyword + semantic search, RRF merge, citation expansion
│   │   ├── screener.py         # Relevance scoring 0–3 per sub-question
│   │   ├── extractor.py        # Evidence cards
│   │   ├── writer.py           # Section-by-section writing with [W123] placeholders
│   │   ├── verifier.py         # Citation existence and claim-support checks
│   │   ├── apa.py              # citeproc-py formatting: in-text and reference list
│   │   └── exporter.py         # .docx and .pdf generation
│   └── workers/
│       ├── consumer.py         # SQS long-poll loop, visibility heartbeat, dispatch
│       ├── planning_job.py
│       └── generation_job.py
└── tests/
    ├── conftest.py
    ├── test_reports_api.py
    ├── test_plan_api.py
    ├── test_status_and_events.py
    ├── test_output_api.py
    ├── test_idempotency.py
    ├── test_worker_jobs.py
    └── test_openapi_contract.py
```

## Requirements

### 1. Contract fidelity
- Serve all routes under `API_PREFIX` (default `/api/v1`).
- Use the same `operationId` values as the spec for route function names.
- Pydantic schemas must match the spec's component schemas, including enums, required fields, nullability, min/max lengths, and `additionalProperties: false` (use `model_config = ConfigDict(extra="forbid")`).
- Return the exact status codes in the spec, for example `202` for create and approve, `204` for delete, and `409` for not-ready or version conflicts.
- Add a test that loads `openapi.yaml` and checks every path and method in it exists in the app.

### 2. Errors
- Every error response uses RFC 9457 Problem Details with `Content-Type: application/problem+json` and the fields `type`, `title`, `status`, `detail`, `instance`, `code`, and optional `errors[]`.
- Convert FastAPI's request validation errors (normally 422) into `400` Problem responses with field-level `errors`, as the spec requires.
- Never expose stack traces or internal messages to clients.

### 3. Authentication and ownership
- Every endpoint requires a valid JWT bearer token, verified with settings from `.env` (secret or JWKS URL, issuer, audience, algorithm).
- The user ID comes from the token's `sub` claim.
- A user can only access their own reports. Return `404` (not `403`) for reports owned by someone else, so IDs can't be probed.

### 4. Create report (`POST /reports`)
1. Validate the body.
2. If an `Idempotency-Key` header is present, look it up in Redis (keyed by user + key):
   - Same key and same body hash → return the original `202` response.
   - Same key, different body → `409`.
3. In one database transaction, insert the report with status `QUEUED` **and** an outbox row.
4. After commit, publish `{ "reportId": "...", "phase": "PLAN" }` to the planning queue and mark the outbox row sent.
5. Store the idempotency record in Redis with TTL `IDEMPOTENCY_TTL_SECONDS`.
6. Return `202` with a `Location` header and the `ReportAccepted` body.
- Run a background **outbox relay** task (started in the app lifespan) every `OUTBOX_RELAY_INTERVAL_SECONDS` that re-publishes unsent outbox rows. This guarantees no report gets stuck if SQS was unavailable.
- Enforce `MAX_ACTIVE_REPORTS_PER_USER` and a per-user rate limit; return `429` with `Retry-After` when exceeded.

### 5. Status lifecycle
Allowed transitions only. Enforce them in `report_service`, never in routers:

```
QUEUED → PLANNING → AWAITING_APPROVAL → QUEUED_FOR_GENERATION
→ SEARCHING → SCREENING → EXTRACTING → WRITING → VERIFYING → COMPLETED
Any active status → FAILED (with error code and message)
Any non-terminal status → CANCELLED (on delete)
```
Implement transitions as conditional updates (`UPDATE ... WHERE id = :id AND status = :expected`) so two workers can never claim the same job.

### 6. Progress (`/status` and `/events`)
- Workers write `status`, `progressPercent`, and `message` to the database and publish the same event to the Redis channel `report:{id}:progress`.
- `/status` reads from the database.
- `/events` returns `text/event-stream` with `event: progress` and a JSON `data` payload matching `ProgressEvent`. Send the current state immediately on connect, then live events. Send a keep-alive comment every `SSE_KEEPALIVE_SECONDS`. Close after a terminal status (`AWAITING_APPROVAL`, `COMPLETED`, `FAILED`, `CANCELLED`) and when the client disconnects.

### 7. Plan endpoints
- `GET /plan`: `409 REPORT_NOT_READY` before `AWAITING_APPROVAL`. Set `editable` to true only while `AWAITING_APPROVAL`.
- `PUT /plan`: only while `AWAITING_APPROVAL`. Compare the request `version` with the stored version; mismatch returns `409 PLAN_VERSION_CONFLICT`. Replace the sub-question list in array order, keep existing IDs where provided, generate IDs for new ones, and increment `version`.
- `POST /plan/approve`: version must match. Atomically set status `QUEUED_FOR_GENERATION`, lock the plan, and publish `{ "reportId": "...", "phase": "GENERATE" }` to the generation queue through the outbox. Return `202`.

### 8. Read and output endpoints
- `GET /reports/{id}`, `/references`, and `/export` return `409 REPORT_NOT_READY` unless status is `COMPLETED`.
- `GET /reports` uses cursor pagination (opaque, base64-encoded `createdAt` + `id`), newest first, with an optional `status` filter.
- `/export?format=docx|pdf`: generate the file on first request, store it in S3 under `exports/{reportId}/{version}.{ext}`, cache it, and stream it back with the correct `Content-Type` and `Content-Disposition`.
- `DELETE /reports/{id}`: if active, set `CANCELLED` first (workers check for cancellation between stages and stop), then delete the report, its child rows, and its S3 objects. Return `204`.

### 9. Workers
- A separate process: `python -m app.workers.consumer --queue planning|generation`.
- Long-poll SQS (`WaitTimeSeconds=20`), process up to `WORKER_CONCURRENCY` messages concurrently.
- **Heartbeat:** while a job runs, extend the message visibility every `SQS_HEARTBEAT_SECONDS` using `ChangeMessageVisibility`.
- **Idempotent:** claim the job with a conditional status update. If the claim fails (already claimed or finished), delete the message and skip.
- **Checkpoints:** save each stage's output (plan, candidate papers, screening scores, evidence cards, draft sections, verified report) to the database. On retry, resume from the last completed stage.
- **Cancellation:** check status between stages; stop if `CANCELLED`.
- **Failures:** retry transient errors (network, rate limits, 5xx) with exponential backoff inside the job. On permanent failure, set `FAILED` with a code (`RETRIEVAL_FAILED`, `INSUFFICIENT_SOURCES`, `LLM_UNAVAILABLE`, `INTERNAL_ERROR`) and delete the message. Unexpected crashes leave the message to SQS, which moves it to the dead-letter queue after `maxReceiveCount`.
- Delete the message only after the job finishes or fails permanently.

### 10. OpenAlex client
- Base URL and API key from settings; send the key on every request.
- Methods: `search(query, filters, per_page)`, `semantic_search(text, filters)`, `get_work(id)`, `get_works(ids)` (batch with `filter=openalex:W1|W2|...`, max 100 per call), `get_citing(work_id)`.
- Rebuild abstracts from `abstract_inverted_index`.
- Rate-limit semantic search to `OPENALEX_SEMANTIC_RPS` and cap results at 50.
- Cache responses in Redis for `OPENALEX_CACHE_TTL_SECONDS`.
- Default filters: `has_abstract:true`, `is_retracted:false`, plus the report's year range and open-access option.

### 11. Pipeline quality rules (non-negotiable)
- **Planner:** produce a precise central question, 3–6 sub-questions with roles (`BACKGROUND`, `CORE`, `DISCUSSION`), and an `interpretationNote` stating how the topic was understood. Flag hidden assumptions in the user's wording (e.g. "causes") as sub-questions to test, not facts.
- **Retriever:** per sub-question, run a Boolean keyword query and a semantic query, merge with Reciprocal Rank Fusion (k = 60), expand from the top seeds through references, citing works, and `related_works`, and boost papers connected to several seeds. If a sub-question has fewer than `MIN_PAPERS_PER_SUBQUESTION` strong candidates, generate new terms and search again, up to `MAX_RETRIEVAL_ROUNDS`.
- **Screener:** score each abstract 0–3 against the specific sub-question, with a one-line reason. Keep scores 2–3; use 1 only for background. Use `LLM_SCREENING_MODEL`, batching `SCREENING_BATCH_SIZE` abstracts per call.
- **Extractor:** one evidence card per kept paper: finding, design, sample, effect direction, claim strength (`causal`, `predictive`, `associational`, `descriptive`), limitations, sub-question ID. Mark unknown values as null; never infer them.
- **Writer:** write section by section from evidence cards only, organized by theme, citing every empirical claim with `[W123]` placeholders. Match wording to claim strength. Include abstract, introduction, body sections, discussion, limitations, and conclusion. Use `LLM_WRITING_MODEL`.
- **Verifier:** remove or flag any `[W…]` not in the retrieved set; run a claim-support check of each cited sentence against its evidence card, and rewrite or drop unsupported claims.
- **APA formatter:** convert placeholders into APA 7 in-text citations and build the reference list with `citeproc-py`, using only OpenAlex metadata. Omit missing fields; never let the LLM invent volume, issue, pages, or DOIs.
- **The LLM must never produce a citation that was not retrieved from OpenAlex.**
- Store screened and included counts, search date, and year range for the `methodology` object.

### 12. LLM client
- One interface, `LLMClient`, with `complete_json(system, user, schema, model)` returning a validated Pydantic object, and `complete_text(...)`.
- Implementation selected by `LLM_PROVIDER`; model names, timeout, max tokens, and temperature from settings.
- Retry on rate limits and timeouts; validate JSON output against the schema and retry once with the validation error if it fails.
- Limit concurrent LLM calls with a semaphore (`LLM_MAX_CONCURRENCY`).
- Log token usage per report.

### 13. Configuration
- `app/config.py` defines a single `Settings` class with `pydantic-settings`, reading `.env`, with types and defaults matching `.env.example`. Use `SecretStr` for secrets.
- The app must fail fast at startup with a clear message if a required setting is missing.

### 14. Observability
- Structured JSON logs with `request_id` (from `X-Request-ID` or generated) and `report_id` on every log line where available.
- `GET /health` (liveness) and `GET /ready` (checks DB, Redis, SQS). These are outside the spec and not under auth.

### 15. Tests
- API tests for every endpoint: success, validation errors, `401`, `404` for other users' reports, `409` cases (not ready, wrong status, version conflict), idempotency replay and mismatch, and `429`.
- Worker tests with the LLM and OpenAlex clients mocked: full happy path, resume from checkpoint, duplicate message skipped, cancellation mid-run, and failure to `FAILED`.
- A citation test: the writer output containing an unknown `[W999]` is removed by the verifier.
- Use `moto` for SQS and S3; use a test Postgres via docker-compose or testcontainers.

## Deliverables

1. The complete project in the structure above, with all code written in full (no placeholders or `TODO`s in required features).
2. Alembic migration creating all tables: `reports`, `plans`, `sub_questions`, `outbox`, `papers`, `screening_results`, `evidence_cards`, `report_sections`, `report_references`.
3. `docker-compose.yml` that starts api, planning worker, generation worker, Postgres, Redis, and LocalStack, with an init script creating both queues, their dead-letter queues, and the S3 bucket.
4. A `README.md` with setup, `.env` instructions, running locally, running tests, and the report lifecycle.
5. The test suite, passing.

## Acceptance criteria

- `docker compose up` starts everything, and `POST /api/v1/reports` returns `202` with an ID.
- The served OpenAPI schema (`/openapi.json`) matches `openapi.yaml` for all paths, methods, and schemas.
- A report moves through every status to `COMPLETED`, visible through `/status` and `/events`.
- Every citation in the final report appears in the reference list, and every reference is a real OpenAlex work.
- Killing a worker mid-job and restarting it resumes the report without duplicate work.
- All tests pass.

## Working method

1. Read `openapi.yaml` and `.env.example` fully before writing code.
2. Build in this order: config → models and migration → schemas → errors and auth → API routes with the service layer → queue and outbox → workers → OpenAlex and LLM clients → pipeline stages → exports → tests → Docker and README.
3. After each layer, run the tests for that layer before continuing.
4. If the spec is ambiguous, follow the spec's descriptions, and list each assumption in the README under "Assumptions".