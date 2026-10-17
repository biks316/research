"""In-memory OpenAlex stand-in with a small, linked corpus."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from app.clients.openalex import Author, OpenAlexError, Work

TOPIC_WORDS = "social media use adolescents anxiety depression"


def _work(n: int, **kw) -> Work:
    year = 2012 + n % 12
    base = dict(
        id=f"W{n}",
        title=f"Social media use and adolescent anxiety: study {n}",
        abstract=(
            f"This study of adolescents examined social media use and anxiety symptoms in sample {n}. "
            "Heavier use was associated with higher anxiety and depression scores."
        ),
        year=year,
        authors=[Author(family=f"Author{n}", given="Alex"), Author(family=f"Second{n}", given="Sam")],
        source="Journal of Adolescent Health",
        volume=str(10 + n),
        issue="2",
        pages=f"{n}-{n + 9}",
        doi=f"https://doi.org/10.1000/test.{n}",
        url=f"https://doi.org/10.1000/test.{n}",
        is_open_access=n % 2 == 0,
        language="en",
        type="article",
        cited_by_count=100 - n,
        referenced_works=[f"W{n + 1}", f"W{n + 2}"],
        related_works=[f"W{n + 3}"],
    )
    base.update(kw)
    return Work(**base)


class FakeOpenAlex:
    def __init__(self, corpus: Iterable[Work] | None = None) -> None:
        works = list(corpus) if corpus is not None else [_work(n) for n in range(100, 116)]
        self.works = {w.id: w for w in works}
        self.calls: list[tuple[str, str]] = []
        self.fail_with: Exception | None = None

    def _check(self, name: str, arg: str) -> None:
        self.calls.append((name, arg))
        if self.fail_with is not None:
            raise self.fail_with

    def _ordered(self, offset: int) -> list[Work]:
        ids = sorted(self.works)
        return [self.works[i] for i in ids[offset:] + ids[:offset]]

    async def search(self, query: str, filters: Mapping[str, str], per_page: int | None = None) -> list[Work]:
        self._check("search", query)
        return self._ordered(0)[:6]

    async def semantic_search(self, text: str, filters: Mapping[str, str], count: int = 50) -> list[Work]:
        self._check("semantic_search", text)
        return self._ordered(3)[:6]

    async def get_work(self, work_id: str) -> Work:
        self._check("get_work", work_id)
        if work_id not in self.works:
            raise OpenAlexError("not found")
        return self.works[work_id]

    async def get_works(self, ids: Iterable[str]) -> list[Work]:
        ids = list(ids)
        self._check("get_works", ",".join(ids))
        return [self.works[i] for i in ids if i in self.works]

    async def get_citing(self, work_id: str, filters: Mapping[str, str] | None = None, per_page: int = 25) -> list[Work]:
        self._check("get_citing", work_id)
        n = int(work_id[1:])
        return [w for w in self.works.values() if work_id in w.referenced_works and int(w.id[1:]) != n][:per_page]

    async def aclose(self) -> None:
        pass


def corpus_work(n: int, **kw) -> Work:
    return _work(n, **kw)
