"""APA 7 formatting with citeproc-py (the `apa` CSL style), from OpenAlex metadata only.

- Reference entries and parenthetical in-text citations come from citeproc-py.
- Same author(s) + same year get a/b/... suffixes (citeproc-py does not disambiguate).
- [W123] / [W1; W2] placeholders become "(Smith & Lee, 2021)" / "(Smith & Lee, 2021; Zhang et al., 2020)".
Missing metadata is omitted, never filled in.
"""

from __future__ import annotations

import html
import re
import string
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import structlog
from citeproc import Citation, CitationItem, CitationStylesBibliography, CitationStylesStyle, formatter
from citeproc.source.json import CiteProcJSON
from citeproc_styles import get_style_filepath

from app.clients.openalex import Work
from app.pipeline.writer import PLACEHOLDER

log = structlog.get_logger(__name__)

MAX_AUTHORS = 100
_TYPE_MAP = {
    "article": "article-journal",
    "review": "article-journal",
    "letter": "article-journal",
    "editorial": "article-journal",
    "book": "book",
    "monograph": "book",
    "book-chapter": "chapter",
    "dissertation": "thesis",
    "report": "report",
    "dataset": "dataset",
    "preprint": "article",
    "posted-content": "article",
    "proceedings-article": "paper-conference",
}


@dataclass
class FormattedReference:
    work_id: str
    apa_citation: str
    in_text_citation: str
    authors: list[dict[str, Any]]
    year: int | None
    title: str
    source: str | None
    volume: str | None
    issue: str | None
    pages: str | None
    doi: str | None
    url: str | None
    is_open_access: bool
    position: int = 0
    sort_key: tuple[Any, ...] = field(default=(), repr=False)


@lru_cache
def _style() -> CitationStylesStyle:
    return CitationStylesStyle(get_style_filepath("apa"), validate=False)


def _bare_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)


def csl_item(work: Work) -> dict[str, Any]:
    item: dict[str, Any] = {"id": work.id, "type": _TYPE_MAP.get(work.type or "", "article-journal"), "title": work.title}
    if work.authors:
        item["author"] = [
            {"family": a.family, **({"given": a.given} if a.given else {})} for a in work.authors[:MAX_AUTHORS]
        ]
    if work.year:
        item["issued"] = {"date-parts": [[work.year]]}
    if work.source:
        item["container-title"] = work.source
    for key, value in (("volume", work.volume), ("issue", work.issue), ("page", work.pages)):
        if value:
            item[key] = value
    doi = _bare_doi(work.doi)
    if doi:
        item["DOI"] = doi
    elif work.url:
        item["URL"] = work.url
    return item


def html_to_markdown(text: str) -> str:
    text = re.sub(r"</?(i|em)>", "*", text)
    text = re.sub(r"</?(b|strong)>", "**", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _suffix_year(text: str, year: int | None, suffix: str) -> str:
    """'(Smith, 2021)' -> '(Smith, 2021a)'; 'Smith, J. (2021).' -> 'Smith, J. (2021a).'"""
    token = str(year) if year else "n.d."
    replacement = f"{token}{suffix}" if year else f"{token}-{suffix}"
    return re.sub(rf"\b{re.escape(token)}(?=[).,;])", replacement, text, count=1)


def format_references(works: Sequence[Work]) -> list[FormattedReference]:
    """Build the APA reference list (sorted as APA requires) with in-text forms."""
    unique = list({w.id: w for w in works}.values())
    if not unique:
        return []
    source = CiteProcJSON([csl_item(w) for w in unique])
    bib = CitationStylesBibliography(_style(), source, formatter.html)
    citations = {}
    for w in unique:
        citations[w.id] = Citation([CitationItem(w.id)])
        bib.register(citations[w.id])
    bib.sort()

    def warn(item: Any) -> None:
        log.warning("citeproc_missing_item", key=item.key)

    entries = [html_to_markdown(str(e)) for e in bib.bibliography()]
    ordered_ids = [item.key.upper() for item in bib.items]
    by_id = {w.id: w for w in unique}
    refs: list[FormattedReference] = []
    for position, (work_id, entry) in enumerate(zip(ordered_ids, entries, strict=True), start=1):
        w = by_id[work_id]
        in_text = html_to_markdown(str(bib.cite(citations[work_id], warn)))
        refs.append(
            FormattedReference(
                work_id=w.id,
                apa_citation=entry,
                in_text_citation=in_text,
                authors=[{"family": a.family, "given": a.given} for a in w.authors],
                year=w.year,
                title=w.title,
                source=w.source,
                volume=w.volume,
                issue=w.issue,
                pages=w.pages,
                doi=w.doi,
                url=w.url,
                is_open_access=w.is_open_access,
                position=position,
            )
        )

    # Disambiguate identical author-date citations: a, b, c ... in reference-list order.
    groups: dict[str, list[FormattedReference]] = defaultdict(list)
    for ref in refs:
        groups[ref.in_text_citation].append(ref)
    for same in groups.values():
        if len(same) < 2:
            continue
        same.sort(key=lambda r: r.title.lower())
        for letter, ref in zip(string.ascii_lowercase, same, strict=False):
            ref.in_text_citation = _suffix_year(ref.in_text_citation, ref.year, letter)
            ref.apa_citation = _suffix_year(ref.apa_citation, ref.year, letter)
    refs.sort(key=lambda r: r.position)
    return refs


def _inner(citation: str) -> str:
    return citation.strip()[1:-1] if citation.startswith("(") and citation.endswith(")") else citation


def apply_citations(text: str, refs: Mapping[str, FormattedReference]) -> str:
    """Replace placeholders with APA in-text citations; multiple works are listed in reference order."""

    def replace(match: re.Match[str]) -> str:
        ids = list(dict.fromkeys(re.findall(r"W\d+", match.group(1))))
        cited = sorted((refs[i] for i in ids if i in refs), key=lambda r: r.position)
        if not cited:
            return ""
        return "(" + "; ".join(_inner(r.in_text_citation) for r in cited) + ")"

    out = PLACEHOLDER.sub(replace, text)
    out = re.sub(r"[ \t]+([.,;:!?])", r"\1", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()
