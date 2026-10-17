"""OpenAlex API client: keyword + semantic search, batch lookup, citing works, Redis cache."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import Settings

log = structlog.get_logger(__name__)

SEMANTIC_MAX_RESULTS = 50
BATCH_SIZE = 100
SELECT_FIELDS = (
    "id,doi,display_name,publication_year,authorships,primary_location,biblio,open_access,"
    "referenced_works,related_works,abstract_inverted_index,is_retracted,language,type,cited_by_count"
)


class OpenAlexError(Exception):
    """OpenAlex could not be reached or returned an error after retries."""


class Author(BaseModel):
    family: str
    given: str | None = None


class Work(BaseModel):
    """The subset of an OpenAlex work the pipeline uses. Only real OpenAlex metadata."""

    id: str
    title: str
    abstract: str | None = None
    year: int | None = None
    authors: list[Author] = Field(default_factory=list)
    source: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    doi: str | None = None
    url: str | None = None
    is_open_access: bool = False
    is_retracted: bool = False
    language: str | None = None
    type: str | None = None
    cited_by_count: int = 0
    referenced_works: list[str] = Field(default_factory=list)
    related_works: list[str] = Field(default_factory=list)


def short_id(value: str | None) -> str | None:
    if not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1].upper()


def rebuild_abstract(inverted: Mapping[str, Sequence[int]] | None) -> str | None:
    """OpenAlex stores abstracts as {word: [positions]}; put the words back in order."""
    if not inverted:
        return None
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            positions[int(i)] = word
    if not positions:
        return None
    return " ".join(positions[i] for i in sorted(positions))


def split_name(display_name: str) -> Author:
    name = " ".join(display_name.split())
    if "," in name:  # "Family, Given"
        family, given = (part.strip() for part in name.split(",", 1))
        return Author(family=family or name, given=given or None)
    parts = name.split(" ")
    if len(parts) == 1:
        return Author(family=parts[0], given=None)
    # Keep lowercase particles with the family name: "Ludwig van Beethoven" -> "van Beethoven".
    split = len(parts) - 1
    while split > 1 and parts[split - 1].lower() in {"van", "von", "de", "del", "der", "da", "di", "la", "le", "du"}:
        split -= 1
    return Author(family=" ".join(parts[split:]), given=" ".join(parts[:split]))


def parse_work(raw: Mapping[str, Any]) -> Work:
    location = raw.get("primary_location") or {}
    source = (location.get("source") or {}).get("display_name")
    biblio = raw.get("biblio") or {}
    first, last = biblio.get("first_page"), biblio.get("last_page")
    pages = f"{first}-{last}" if first and last and first != last else (first or None)
    doi = raw.get("doi")
    if doi and not doi.startswith("http"):
        doi = f"https://doi.org/{doi}"
    authors = []
    for authorship in raw.get("authorships") or []:
        name = (authorship.get("author") or {}).get("display_name") or authorship.get("raw_author_name")
        if name:
            authors.append(split_name(name))
    work_id = short_id(raw.get("id")) or ""
    return Work(
        id=work_id,
        title=(raw.get("display_name") or raw.get("title") or "").strip(),
        abstract=rebuild_abstract(raw.get("abstract_inverted_index")),
        year=raw.get("publication_year"),
        authors=authors,
        source=source,
        volume=biblio.get("volume") or None,
        issue=biblio.get("issue") or None,
        pages=pages,
        doi=doi,
        url=doi or location.get("landing_page_url") or f"https://openalex.org/{work_id}",
        is_open_access=bool((raw.get("open_access") or {}).get("is_oa")),
        is_retracted=bool(raw.get("is_retracted")),
        language=raw.get("language"),
        type=raw.get("type"),
        cited_by_count=int(raw.get("cited_by_count") or 0),
        referenced_works=[w for w in (short_id(x) for x in raw.get("referenced_works") or []) if w],
        related_works=[w for w in (short_id(x) for x in raw.get("related_works") or []) if w],
    )


def default_filters(
    *, year_from: int | None, year_to: int | None, open_access_only: bool, language: str | None
) -> dict[str, str]:
    filters = {"has_abstract": "true", "is_retracted": "false"}
    if year_from or year_to:
        filters["publication_year"] = f"{year_from or ''}-{year_to or ''}"
    if open_access_only:
        filters["is_oa"] = "true"
    if language:
        filters["language"] = language
    return filters


def filter_string(filters: Mapping[str, str]) -> str:
    return ",".join(f"{k}:{v}" for k, v in filters.items())


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class _RateLimiter:
    """Allow at most `rps` calls per second (shared across tasks in this process)."""

    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if now < self._next:
                await asyncio.sleep(self._next - now)
                now = loop.time()
            self._next = now + self._interval


class OpenAlexClient:
    def __init__(self, settings: Settings, redis: Redis | None, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._redis = redis
        self._api_key = settings.OPENALEX_API_KEY.get_secret_value() if settings.OPENALEX_API_KEY else None
        self._http = http or httpx.AsyncClient(
            base_url=settings.OPENALEX_BASE_URL,
            timeout=settings.OPENALEX_TIMEOUT_SECONDS,
            headers={"User-Agent": "apa-report-api/1.0"},
        )
        self._semantic_limiter = _RateLimiter(settings.OPENALEX_SEMANTIC_RPS)
        self._attempts = 5

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ transport
    def _cache_key(self, path: str, params: Mapping[str, Any]) -> str:
        canonical = json.dumps([path, sorted(params.items())], separators=(",", ":"), default=str)
        return "openalex:" + hashlib.sha256(canonical.encode()).hexdigest()

    async def _get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        key = self._cache_key(path, params)
        ttl = self._settings.OPENALEX_CACHE_TTL_SECONDS
        if self._redis is not None and ttl > 0:
            try:
                cached = await self._redis.get(key)
            except Exception:  # noqa: BLE001 - cache is optional
                cached = None
            if cached:
                return json.loads(cached)

        request_params = dict(params)
        if self._api_key:
            request_params["api_key"] = self._api_key
        if self._settings.OPENALEX_MAILTO:
            request_params["mailto"] = self._settings.OPENALEX_MAILTO
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception(_is_retryable),
                wait=wait_exponential_jitter(initial=1, max=30),
                stop=stop_after_attempt(self._attempts),
                reraise=True,
            ):
                with attempt:
                    response = await self._http.get(path, params=request_params)
                    response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            log.warning("openalex_request_failed", path=path, status=status, error=type(exc).__name__)
            raise OpenAlexError(f"OpenAlex request to {path} failed ({status or type(exc).__name__})") from exc
        data = response.json()
        if self._redis is not None and ttl > 0:
            try:
                await self._redis.set(key, json.dumps(data), ex=ttl)
            except Exception:  # noqa: BLE001
                pass
        return data

    # ------------------------------------------------------------- methods
    async def search(self, query: str, filters: Mapping[str, str], per_page: int | None = None) -> list[Work]:
        """Keyword search (supports AND / OR / NOT and quoted phrases)."""
        data = await self._get(
            "/works",
            {
                "search": query,
                "filter": filter_string(filters),
                "per_page": per_page or self._settings.OPENALEX_PER_PAGE,
                "select": SELECT_FIELDS,
            },
        )
        return [parse_work(r) for r in data.get("results", [])]

    async def semantic_search(self, text: str, filters: Mapping[str, str], count: int = SEMANTIC_MAX_RESULTS) -> list[Work]:
        """Embedding-based search (`search.semantic`). Rate-limited and capped at 50 results."""
        await self._semantic_limiter.wait()
        data = await self._get(
            "/works",
            {
                "search.semantic": text[:10000],
                "filter": filter_string(filters),
                "per_page": min(count, SEMANTIC_MAX_RESULTS),
                "select": SELECT_FIELDS,
            },
        )
        return [parse_work(r) for r in data.get("results", [])[:SEMANTIC_MAX_RESULTS] if r.get("id")]

    async def get_work(self, work_id: str) -> Work:
        return parse_work(await self._get(f"/works/{short_id(work_id)}"))

    async def get_works(self, ids: Iterable[str]) -> list[Work]:
        """Batch lookup with filter=openalex:W1|W2|..., at most 100 ids per call."""
        unique = list(dict.fromkeys(i for i in (short_id(x) for x in ids) if i))
        results: list[Work] = []
        for start in range(0, len(unique), BATCH_SIZE):
            chunk = unique[start : start + BATCH_SIZE]
            data = await self._get(
                "/works",
                {"filter": "openalex:" + "|".join(chunk), "per_page": BATCH_SIZE, "select": SELECT_FIELDS},
            )
            results.extend(parse_work(r) for r in data.get("results", []))
        return results

    async def get_citing(
        self, work_id: str, filters: Mapping[str, str] | None = None, per_page: int = 25
    ) -> list[Work]:
        all_filters = {"cites": short_id(work_id) or work_id, **(filters or {})}
        data = await self._get(
            "/works",
            {"filter": filter_string(all_filters), "per_page": per_page, "sort": "cited_by_count:desc", "select": SELECT_FIELDS},
        )
        return [parse_work(r) for r in data.get("results", [])]
