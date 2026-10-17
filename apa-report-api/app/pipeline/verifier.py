"""Verification: drop citations to works that were not retrieved, then check that every cited
sentence is supported by the cited evidence cards (rewrite or drop it if not)."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.clients.llm import LLMClient, embed_input
from app.pipeline.models import ClaimCheck
from app.pipeline.writer import PLACEHOLDER, DraftSection

log = structlog.get_logger(__name__)

CHECK_BATCH = 20
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[*])")
_WORK_ID = re.compile(r"W\d+")

SYSTEM = """TASK: verify_claims
You fact-check sentences in a literature review against evidence cards extracted from the cited
papers. For each numbered sentence:
- supported = true only if every claim in it is backed by the cards of the papers it cites, and its
  wording is no stronger than their claim_strength (e.g. no causal verbs for associational evidence).
- If not supported, give `rewrite`: a corrected sentence that says only what the cited cards
  support, keeping placeholder citations like [W123] for the cards that back it. If nothing in the
  sentence can be supported, set rewrite to null (the sentence will be removed).
- If supported, set rewrite to null.
Return one verdict per sentence, using its index. Return only the JSON object."""


@dataclass
class VerificationReport:
    removed_citations: dict[str, int] = field(default_factory=dict)
    rewritten: int = 0
    dropped: int = 0
    checked: int = 0


def cited_ids(text: str) -> list[str]:
    ids: list[str] = []
    for group in PLACEHOLDER.findall(text):
        ids.extend(_WORK_ID.findall(group))
    return list(dict.fromkeys(ids))


def scrub_unknown(text: str, known: Collection[str], report: VerificationReport | None = None) -> str:
    """Remove [W…] ids that are not in the retrieved set; drop brackets left empty."""

    def replace(match: re.Match[str]) -> str:
        ids = _WORK_ID.findall(match.group(1))
        keep = [i for i in dict.fromkeys(ids) if i in known]
        for i in ids:
            if i not in known and report is not None:
                report.removed_citations[i] = report.removed_citations.get(i, 0) + 1
        return f"[{'; '.join(keep)}]" if keep else ""

    cleaned = PLACEHOLDER.sub(replace, text)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def split_sentences(paragraph: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(paragraph.strip()) if s]


class Verifier:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def verify(
        self,
        sections: Sequence[DraftSection],
        *,
        known_ids: Collection[str],
        cards_by_work: Mapping[str, Sequence[Any]],
    ) -> tuple[list[DraftSection], VerificationReport]:
        report = VerificationReport()
        known = set(known_ids)
        out: list[DraftSection] = []
        for section in sections:
            content = scrub_unknown(section.content, known, report)
            content = await self._check_section(content, known, cards_by_work, report)
            out.append(DraftSection(section.kind, section.heading, section.sub_question_id, content))
        if report.removed_citations:
            log.warning("verifier_removed_unknown_citations", ids=sorted(report.removed_citations))
        log.info(
            "verification_done", checked=report.checked, rewritten=report.rewritten, dropped=report.dropped
        )
        return out, report

    async def _check_section(
        self,
        content: str,
        known: set[str],
        cards_by_work: Mapping[str, Sequence[Any]],
        report: VerificationReport,
    ) -> str:
        # Keep the block structure (paragraphs, sub-headings); check sentences with citations.
        blocks = re.split(r"(\n\s*\n)", content)
        sentences: list[tuple[int, int, str]] = []  # (block index, sentence index, text)
        split_blocks: dict[int, list[str]] = {}
        for bi, block in enumerate(blocks):
            if not block.strip() or block.lstrip().startswith("#"):
                continue
            parts = split_sentences(block)
            split_blocks[bi] = parts
            for si, sentence in enumerate(parts):
                if cited_ids(sentence):
                    sentences.append((bi, si, sentence))

        verdicts: dict[int, Any] = {}
        for start in range(0, len(sentences), CHECK_BATCH):
            batch = sentences[start : start + CHECK_BATCH]
            payload = []
            for offset, (_, _, sentence) in enumerate(batch):
                ids = cited_ids(sentence)
                payload.append(
                    {
                        "index": start + offset,
                        "sentence": sentence,
                        "cards": [_card_view(c) for i in ids for c in cards_by_work.get(i, [])],
                    }
                )
            result = await self.llm.complete_json(SYSTEM, "Check these sentences.\n" + embed_input({"sentences": payload}), ClaimCheck)
            for v in result.verdicts:
                verdicts[v.index] = v
        report.checked += len(sentences)

        for idx, (bi, si, _) in enumerate(sentences):
            verdict = verdicts.get(idx)
            if verdict is None or verdict.supported:
                continue
            if verdict.rewrite and verdict.rewrite.strip():
                split_blocks[bi][si] = scrub_unknown(verdict.rewrite.strip(), known, report)
                report.rewritten += 1
            else:
                split_blocks[bi][si] = ""
                report.dropped += 1

        for bi, parts in split_blocks.items():
            blocks[bi] = " ".join(p for p in parts if p)
        text = "".join(blocks)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def _card_view(card: Any) -> dict[str, Any]:
    return {
        "work_id": card.work_id,
        "finding": card.finding,
        "design": card.design,
        "sample": card.sample,
        "effect_direction": card.effect_direction,
        "claim_strength": card.claim_strength,
        "limitations": card.limitations,
    }
