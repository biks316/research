"""Retrieval per sub-question.

1. Ask the LLM for a Boolean keyword query and a semantic query.
2. Run both against OpenAlex; merge with Reciprocal Rank Fusion (k = 60).
3. Expand from the top seeds through their references, citing works and related works;
   papers linked to several seeds get a larger boost.
4. If there are fewer than MIN_PAPERS_PER_SUBQUESTION strong candidates, ask for new terms
   and search again, up to MAX_RETRIEVAL_ROUNDS.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

import structlog

from app.clients.llm import LLMClient, embed_input
from app.clients.openalex import OpenAlexClient, Work
from app.config import Settings
from app.pipeline.models import SearchTerms

log = structlog.get_logger(__name__)

RRF_K = 60
CITING_PER_SEED = 25
KEYWORD, SEMANTIC, EXPANSION = "keyword", "semantic", "expansion"

SYSTEM = """TASK: search_terms
You write literature-search queries for OpenAlex.
Given a research sub-question (and the central question for context), return:
- boolean_query: a keyword query for OpenAlex full-text search. Use AND / OR / NOT in capitals,
  quoted phrases for multi-word terms, and parentheses. Include synonyms joined with OR
  (e.g. ("social media" OR "social networking") AND (adolescen* OR teen*) AND anxiety).
  Keep it under 300 characters and avoid over-narrow terms.
- semantic_query: one or two plain sentences describing the papers that would answer the question.
If previous queries are listed, they found too few relevant papers: use different vocabulary,
broader synonyms, or adjacent constructs - do not repeat them. Return only the JSON object."""


@dataclass
class Candidate:
    work: Work
    score: float = 0.0
    sources: set[str] = field(default_factory=set)
    seed_connections: int = 0

    @property
    def strong(self) -> bool:
        """Found by both searches, or by one search plus a seed link, or linked to 2+ seeds."""
        searches = len(self.sources & {KEYWORD, SEMANTIC})
        return searches >= 2 or (searches == 1 and self.seed_connections >= 1) or self.seed_connections >= 2


def rrf_merge(ranked_lists: Sequence[Sequence[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion: score(d) = sum over lists of 1 / (k + rank), rank starting at 1."""
    scores: dict[str, float] = defaultdict(float)
    for ranked in ranked_lists:
        for rank, work_id in enumerate(dict.fromkeys(ranked), start=1):
            scores[work_id] += 1.0 / (k + rank)
    return dict(scores)


def passes_filters(work: Work, filters: Mapping[str, str]) -> bool:
    """Apply the report's filters to works found through expansion (which bypass the API filters)."""
    if not work.abstract or work.is_retracted or not work.title:
        return False
    years = filters.get("publication_year")
    if years and work.year is not None:
        start, _, end = years.partition("-")
        if (start and work.year < int(start)) or (end and work.year > int(end)):
            return False
    elif years and work.year is None:
        return False
    if filters.get("is_oa") == "true" and not work.is_open_access:
        return False
    lang = filters.get("language")
    return not (lang and work.language and work.language != lang)


ProgressFn = Callable[[str], Awaitable[None]]


class Retriever:
    def __init__(self, openalex: OpenAlexClient, llm: LLMClient, settings: Settings) -> None:
        self.openalex = openalex
        self.llm = llm
        self.settings = settings

    async def _terms(self, central: str, question: str, previous: list[SearchTerms]) -> SearchTerms:
        user = "Write search queries for this sub-question.\n" + embed_input(
            {
                "centralQuestion": central,
                "subQuestion": question,
                "previousQueries": [p.model_dump() for p in previous],
            }
        )
        return await self.llm.complete_json(SYSTEM, user, SearchTerms)

    async def retrieve(
        self,
        *,
        central_question: str,
        question: str,
        filters: Mapping[str, str],
        progress: ProgressFn | None = None,
    ) -> list[Candidate]:
        works: dict[str, Work] = {}
        ranked_lists: list[list[str]] = []
        sources: dict[str, set[str]] = defaultdict(set)
        connections: dict[str, set[str]] = defaultdict(set)
        expanded_seeds: set[str] = set()
        previous: list[SearchTerms] = []
        candidates: list[Candidate] = []

        for round_no in range(1, self.settings.MAX_RETRIEVAL_ROUNDS + 1):
            terms = await self._terms(central_question, question, previous)
            previous.append(terms)
            if progress:
                await progress(f"search round {round_no}")
            keyword = await self.openalex.search(terms.boolean_query, filters)
            semantic = await self.openalex.semantic_search(terms.semantic_query, filters)
            for label, found in ((KEYWORD, keyword), (SEMANTIC, semantic)):
                ranked_lists.append([w.id for w in found])
                for w in found:
                    works.setdefault(w.id, w)
                    sources[w.id].add(label)

            fused = rrf_merge(ranked_lists)
            seeds = [wid for wid, _ in sorted(fused.items(), key=lambda kv: -kv[1])][: self.settings.RETRIEVAL_EXPANSION_SEEDS]
            await self._expand([s for s in seeds if s not in expanded_seeds], works, connections, filters)
            expanded_seeds.update(seeds)

            candidates = self._rank(works, fused, sources, connections, filters)
            strong = sum(1 for c in candidates if c.strong)
            log.info("retrieval_round", round=round_no, candidates=len(candidates), strong=strong)
            if strong >= self.settings.MIN_PAPERS_PER_SUBQUESTION:
                break
        return candidates[: self.settings.MAX_CANDIDATES_PER_SUBQUESTION]

    async def _expand(
        self,
        seeds: list[str],
        works: dict[str, Work],
        connections: dict[str, set[str]],
        filters: Mapping[str, str],
    ) -> None:
        """Follow references, citing works and related works from each seed."""
        linked_ids: set[str] = set()
        for seed_id in seeds:
            seed = works.get(seed_id)
            if seed is None:
                continue
            neighbours = set(seed.referenced_works) | set(seed.related_works)
            citing = await self.openalex.get_citing(seed_id, filters, per_page=CITING_PER_SEED)
            for w in citing:
                works.setdefault(w.id, w)
                neighbours.add(w.id)
            for nid in neighbours - {seed_id}:
                connections[nid].add(seed_id)
            linked_ids |= neighbours
        missing = [i for i in linked_ids if i not in works]
        if missing:
            for w in await self.openalex.get_works(missing):
                works.setdefault(w.id, w)

    def _rank(
        self,
        works: dict[str, Work],
        fused: dict[str, float],
        sources: dict[str, set[str]],
        connections: dict[str, set[str]],
        filters: Mapping[str, str],
    ) -> list[Candidate]:
        out: list[Candidate] = []
        boost = 1.0 / (RRF_K + 1)  # each seed link is worth a top-ranked hit in one list
        for work_id, work in works.items():
            links = len(connections.get(work_id, ()))
            if work_id not in fused and links == 0:
                continue
            if not passes_filters(work, filters):
                continue
            src = set(sources.get(work_id, set()))
            if links:
                src.add(EXPANSION)
            out.append(
                Candidate(work=work, score=fused.get(work_id, 0.0) + boost * links, sources=src, seed_connections=links)
            )
        out.sort(key=lambda c: (-c.score, -c.work.cited_by_count, c.work.id))
        return out
