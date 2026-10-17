"""Deterministic handlers for FakeLLMClient (LLM_PROVIDER=fake).

They build outputs only from the <input> payload, so an offline run still cites only the
works it was given. Useful for local development without an API key and for tests.
"""

from __future__ import annotations

import re
from typing import Any

from app.models.enums import SubQuestionRole
from app.pipeline.models import (
    ClaimCheck,
    EvidenceBatch,
    EvidenceCardOut,
    FrontMatter,
    PlanDraft,
    PlannedSubQuestion,
    ScreeningBatch,
    ScreeningScore,
    SearchTerms,
    SectionDraft,
    SentenceVerdict,
)

_STOP = {
    "the", "and", "for", "with", "what", "does", "how", "are", "is", "of", "in", "on", "to", "a", "an",
    "do", "can", "be", "by", "that", "this", "which", "from", "their", "it", "as", "or", "about", "between",
    "evidence", "research", "show", "known", "there", "these", "those", "has", "have", "was", "were",
}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z\-]+", (text or "").lower()) if w not in _STOP and len(w) > 2]


def plan(system: str, data: dict[str, Any]) -> PlanDraft:
    topic = data["topic"].strip().rstrip(".")
    description = data.get("description", "")
    causal = bool(re.search(r"\b(cause[sd]?|causing|leads? to|results? in)\b", description, re.I))
    subs = [
        PlannedSubQuestion(
            question=f"What background and definitions frame research on {topic.lower()}?",
            rationale="Gives the introduction its context and key terms.",
            role=SubQuestionRole.BACKGROUND,
        ),
        PlannedSubQuestion(
            question=f"What associations have empirical studies found regarding {topic.lower()}?",
            rationale="Summarizes the main body of empirical evidence.",
            role=SubQuestionRole.CORE,
        ),
        PlannedSubQuestion(
            question=f"Which factors moderate or explain the findings on {topic.lower()}?",
            rationale="Explains differences between studies.",
            role=SubQuestionRole.CORE,
        ),
    ]
    assumptions = []
    if causal:
        assumptions.append("The wording assumes a causal effect.")
        subs.append(
            PlannedSubQuestion(
                question=f"Is the relationship in {topic.lower()} causal, or explained by confounding or reverse causation?",
                rationale="Tests the causal assumption in the request instead of accepting it.",
                role=SubQuestionRole.CORE,
            )
        )
    subs.append(
        PlannedSubQuestion(
            question=f"What practical implications and interventions follow from research on {topic.lower()}?",
            rationale="Supports the discussion of what can be done.",
            role=SubQuestionRole.DISCUSSION,
        )
    )
    return PlanDraft(
        central_question=f"What does the research evidence show about {topic.lower()}?",
        interpretation_note=f"The topic was read as a request for a review of empirical research on {topic.lower()}."
        + (" The plan tests the causal claim rather than assuming it." if causal else ""),
        hidden_assumptions=assumptions,
        sub_questions=subs,
    )


def search_terms(system: str, data: dict[str, Any]) -> SearchTerms:
    words = _words(data["subQuestion"])[:4] or ["research"]
    rounds = len(data.get("previousQueries") or [])
    terms = words[: max(1, len(words) - rounds)]
    return SearchTerms(boolean_query=" AND ".join(terms), semantic_query=data["subQuestion"])


def screen(system: str, data: dict[str, Any]) -> ScreeningBatch:
    question = set(_words(data["subQuestion"]))
    results = []
    for paper in data["papers"]:
        overlap = len(question & set(_words(f"{paper.get('title')} {paper.get('abstract')}")))
        score = 3 if overlap >= 3 else 2 if overlap >= 1 else 1 if paper.get("abstract") else 0
        results.append(ScreeningScore(work_id=paper["work_id"], score=score, reason=f"{overlap} key terms match the sub-question"))
    return ScreeningBatch(results=results)


def extract(system: str, data: dict[str, Any]) -> EvidenceBatch:
    cards = []
    for paper in data["papers"]:
        abstract = (paper.get("abstract") or paper.get("title") or "").strip()
        first = re.split(r"(?<=[.!?])\s+", abstract)[0][:400] if abstract else paper.get("title") or "No finding stated"
        cards.append(
            EvidenceCardOut(
                work_id=paper["work_id"], finding=first, design=None, sample=None,
                effect_direction=None, claim_strength=None, limitations=None,
            )
        )
    return EvidenceBatch(cards=cards)


def write_section(system: str, data: dict[str, Any]) -> SectionDraft:
    kind = data["section"]
    cards = data.get("evidenceCards") or []
    headings = {"introduction": "Introduction", "discussion": "Discussion", "limitations": "Limitations", "conclusion": "Conclusion"}
    heading = headings.get(kind) or re.sub(r"[?]$", "", data.get("subQuestion", "Findings"))[:120]
    sentences = []
    if kind == "introduction":
        sentences.append(f"This review addresses the question: {data['centralQuestion']}")
    for card in cards[:6]:
        finding = card["finding"].rstrip(". ")
        sentences.append(f"One study reported that {finding[0].lower() + finding[1:] if finding else 'results were mixed'} {card['cite_as']}.")
    if kind == "limitations":
        sentences.append("This review searched a single database and relied on abstracts, so some evidence may be missing.")
    if kind == "conclusion":
        sentences.append("Overall, the available studies provide a starting point, but stronger designs are needed.")
    if not sentences:
        sentences.append("The retrieved studies did not address this question directly.")
    return SectionDraft(heading=heading, content=" ".join(sentences))


def front_matter(system: str, data: dict[str, Any]) -> FrontMatter:
    central = data["centralQuestion"].rstrip("?")
    title = central.replace("What does the research evidence show about", "Research Evidence on").strip()
    title = title[:1].upper() + title[1:]
    words = list(dict.fromkeys(_words(central)))[:5]
    while len(words) < 3:
        words.append(["review", "evidence", "research"][len(words)])
    abstract = (
        f"This literature review examines {central.lower()}. Studies were identified in OpenAlex, screened for "
        "relevance to each sub-question, and summarized from their abstracts. The review describes the main "
        "findings, how they differ across studies, and the limitations of the available evidence, and it "
        "closes with implications for practice and further research."
    )
    return FrontMatter(title=title[:240] or "Literature Review", abstract=abstract, keywords=words)


def verify_claims(system: str, data: dict[str, Any]) -> ClaimCheck:
    return ClaimCheck(verdicts=[SentenceVerdict(index=s["index"], supported=True, rewrite=None) for s in data["sentences"]])


DEFAULT_HANDLERS = {
    "plan": plan,
    "search_terms": search_terms,
    "screen": screen,
    "extract": extract,
    "write_section": write_section,
    "front_matter": front_matter,
    "verify_claims": verify_claims,
}
