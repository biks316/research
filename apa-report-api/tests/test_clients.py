import asyncio
import json
from urllib.parse import parse_qs, urlparse

import fakeredis
import httpx
import pytest
from pydantic import BaseModel, Field

from app.clients import openalex as oa
from app.clients.llm import (
    AnthropicLLMClient,
    FakeLLMClient,
    LLMClient,
    LLMOutputError,
    strict_schema,
    track_usage,
)
from app.pipeline.models import EvidenceBatch, PlanDraft, SearchTerms
from tests.conftest import make_settings

RAW_WORK = {
    "id": "https://openalex.org/W2741809807",
    "doi": "https://doi.org/10.1000/xyz123",
    "display_name": "Screen time and anxiety",
    "publication_year": 2019,
    "authorships": [
        {"author": {"display_name": "Jane Q. Smith"}},
        {"author": {"display_name": "Ludwig van Beethoven"}},
        {"raw_author_name": "Lee, Kim"},
    ],
    "primary_location": {"source": {"display_name": "Journal of Youth"}, "landing_page_url": "https://j.example/1"},
    "biblio": {"volume": "12", "issue": "3", "first_page": "45", "last_page": "60"},
    "open_access": {"is_oa": True},
    "referenced_works": ["https://openalex.org/W1", "https://openalex.org/W2"],
    "related_works": ["https://openalex.org/W3"],
    "abstract_inverted_index": {"Screen": [0], "time": [1, 5], "predicts": [2], "anxiety.": [3], "More": [4]},
    "is_retracted": False,
    "language": "en",
    "type": "article",
    "cited_by_count": 7,
}


# ---------------------------------------------------------------- OpenAlex
def test_rebuild_abstract_and_parse_work():
    work = oa.parse_work(RAW_WORK)
    assert work.id == "W2741809807"
    assert work.abstract == "Screen time predicts anxiety. More time"
    assert [(a.family, a.given) for a in work.authors] == [("Smith", "Jane Q."), ("van Beethoven", "Ludwig"), ("Lee", "Kim")]
    assert work.pages == "45-60" and work.volume == "12" and work.issue == "3"
    assert work.doi == "https://doi.org/10.1000/xyz123"
    assert work.referenced_works == ["W1", "W2"] and work.related_works == ["W3"]
    assert oa.rebuild_abstract(None) is None


def test_missing_metadata_stays_missing():
    work = oa.parse_work({"id": "https://openalex.org/W9", "display_name": "Bare"})
    assert work.volume is None and work.issue is None and work.pages is None and work.doi is None
    assert work.url == "https://openalex.org/W9"


def test_default_filters():
    f = oa.default_filters(year_from=2010, year_to=2026, open_access_only=True, language="en")
    assert oa.filter_string(f) == "has_abstract:true,is_retracted:false,publication_year:2010-2026,is_oa:true,language:en"
    assert "is_oa" not in oa.default_filters(year_from=None, year_to=None, open_access_only=False, language=None)


def _client(handler, redis=None, **settings):
    s = make_settings(**{"OPENALEX_API_KEY": "k-123", "OPENALEX_SEMANTIC_RPS": 50, **settings})
    http = httpx.AsyncClient(base_url="https://api.openalex.test", transport=httpx.MockTransport(handler))
    client = oa.OpenAlexClient(s, redis, http=http)
    return client


async def test_api_key_sent_on_every_request_and_search_params():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"results": [RAW_WORK]})

    client = _client(handler)
    works = await client.search('"social media" AND anxiety', {"has_abstract": "true"}, per_page=10)
    await client.get_work("W2741809807")
    await client.get_citing("W1", {"is_retracted": "false"})
    assert works[0].id == "W2741809807"
    for request in seen:
        assert parse_qs(urlparse(str(request.url)).query)["api_key"] == ["k-123"]
    q = parse_qs(urlparse(str(seen[0].url)).query)
    assert q["search"] == ['"social media" AND anxiety'] and q["per_page"] == ["10"]
    assert parse_qs(urlparse(str(seen[2].url)).query)["filter"] == ["cites:W1,is_retracted:false"]


async def test_get_works_batches_100_ids_per_call():
    calls = []

    def handler(request):
        ids = parse_qs(urlparse(str(request.url)).query)["filter"][0].removeprefix("openalex:").split("|")
        calls.append(ids)
        return httpx.Response(200, json={"results": [{**RAW_WORK, "id": f"https://openalex.org/{i}"} for i in ids]})

    client = _client(handler)
    works = await client.get_works([f"W{i}" for i in range(250)] + ["W1"])
    assert [len(c) for c in calls] == [100, 100, 50]
    assert len(works) == 250


async def test_semantic_search_uses_search_semantic_and_caps_at_50():
    def handler(request):
        q = parse_qs(urlparse(str(request.url)).query)
        assert request.url.path == "/works"
        assert q["search.semantic"] == ["social media and teen anxiety"]
        assert q["per_page"] == ["50"] and q["filter"] == ["has_abstract:true"]
        return httpx.Response(200, json={"results": [{**RAW_WORK, "id": f"https://openalex.org/W{i}"} for i in range(70)]})

    works = await _client(handler).semantic_search("social media and teen anxiety", {"has_abstract": "true"}, count=500)
    assert len(works) == 50


async def test_semantic_search_is_rate_limited():
    def handler(request):
        return httpx.Response(200, json={"results": []})

    client = _client(handler, OPENALEX_SEMANTIC_RPS=5)
    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(3):
        await client.semantic_search("x", {})
    assert loop.time() - start >= 0.38  # 3 calls at 5 rps -> at least 2 intervals of 0.2 s


async def test_responses_are_cached_in_redis():
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(200, json={"results": [RAW_WORK]})

    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    client = _client(handler, redis=redis)
    await client.search("anxiety", {})
    await client.search("anxiety", {})
    assert len(hits) == 1
    keys = await redis.keys("openalex:*")
    assert len(keys) == 1 and 0 < await redis.ttl(keys[0]) <= 60
    assert "k-123" not in json.dumps(keys)  # the API key is not part of the cache key


async def test_retries_transient_errors_then_raises(monkeypatch):
    monkeypatch.setattr(oa, "wait_exponential_jitter", lambda **kw: (lambda rs: 0))
    attempts = []

    def flaky(request):
        attempts.append(1)
        return httpx.Response(503) if len(attempts) < 3 else httpx.Response(200, json={"results": []})

    assert await _client(flaky).search("x", {}) == []
    assert len(attempts) == 3

    def always_429(request):
        return httpx.Response(429)

    with pytest.raises(oa.OpenAlexError):
        await _client(always_429).search("x", {})

    calls = []

    def bad_request(request):
        calls.append(1)
        return httpx.Response(400)

    with pytest.raises(oa.OpenAlexError):
        await _client(bad_request).search("x", {})
    assert len(calls) == 1  # 4xx (other than 429) is not retried


# --------------------------------------------------------------------- LLM
def test_request_for_opus_uses_effort_and_fallbacks_without_sampling():
    client = AnthropicLLMClient(make_settings(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-test"))
    req = client.build_request("sys", [{"role": "user", "content": "hi"}], "claude-opus-5-5", 1000, EvidenceBatch)
    assert "temperature" not in req and "extra_body" not in req and "thinking" not in req
    assert req["output_config"]["effort"] == "high"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]


def test_request_for_haiku_sends_temperature_not_effort():
    client = AnthropicLLMClient(make_settings(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-test", LLM_TEMPERATURE=0.1))
    req = client.build_request("sys", [{"role": "user", "content": "hi"}], "claude-haiku-4-5", 1000, None)
    assert req["extra_body"] == {"temperature": 0.1}
    assert "output_config" not in req and "fallbacks" not in req


def test_strict_schema_closes_objects_and_requires_every_field():
    schema = strict_schema(PlanDraft)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for key in ("minLength", "maxLength", "minimum", "maximum", "maxItems", "default", "title"):
                assert key not in node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)


class Thing(BaseModel):
    name: str = Field(min_length=3)
    count: int


class ScriptedLLM(LLMClient):
    def __init__(self, settings, outputs):
        super().__init__(settings)
        self.outputs = list(outputs)
        self.requests = []
        self.active = 0
        self.peak = 0

    async def _complete(self, system, messages, model, max_tokens, schema):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.requests.append(messages)
        await asyncio.sleep(0.01)
        self.active -= 1
        return self.outputs.pop(0)


async def test_complete_json_retries_once_with_the_validation_error():
    llm = ScriptedLLM(make_settings(), ['{"name": "ab", "count": 1}', '{"name": "abc", "count": 1}'])
    result = await llm.complete_json("sys", "make a thing", Thing)
    assert result == Thing(name="abc", count=1)
    retry = llm.requests[1]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]
    assert "failed validation" in retry[-1]["content"] and "name" in retry[-1]["content"]


async def test_complete_json_gives_up_after_one_retry():
    llm = ScriptedLLM(make_settings(), ["nope", '{"name": 1}'])
    with pytest.raises(LLMOutputError):
        await llm.complete_json("sys", "make a thing", Thing)


async def test_llm_concurrency_is_limited_by_semaphore():
    llm = ScriptedLLM(make_settings(LLM_MAX_CONCURRENCY=2), ["x"] * 6)
    await asyncio.gather(*(llm.complete_text("s", "u") for _ in range(6)))
    assert llm.peak == 2


async def test_token_usage_is_tracked_per_context():
    llm = FakeLLMClient(make_settings())
    with track_usage() as usage:
        await llm.complete_json(
            "TASK: search_terms", 'q <input>\n{"subQuestion": "Does social media affect sleep?", "previousQueries": []}\n</input>', SearchTerms
        )
    assert usage["calls"] == 1 and usage["input_tokens"] > 0 and usage["output_tokens"] > 0
