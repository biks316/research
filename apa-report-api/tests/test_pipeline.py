from types import SimpleNamespace

import pytest

from app.clients.llm import FakeLLMClient
from app.clients.openalex import Author, Work
from app.pipeline import apa
from app.pipeline.models import ClaimCheck, SentenceVerdict
from app.pipeline.retriever import Candidate, Retriever, passes_filters, rrf_merge
from app.pipeline.screener import is_included
from app.pipeline.verifier import Verifier, cited_ids, scrub_unknown
from app.pipeline.writer import DraftSection
from tests.conftest import make_settings
from tests.fakes import FakeOpenAlex, corpus_work


def card(work_id, claim_strength="associational"):
    return SimpleNamespace(
        work_id=work_id, finding="Use was associated with anxiety.", design="cross-sectional", sample="1,000 teens",
        effect_direction="positive", claim_strength=claim_strength, limitations=None,
    )


# --------------------------------------------------------------- retriever
def test_rrf_merge_uses_k_60():
    scores = rrf_merge([["A", "B", "C"], ["B", "A"]])
    assert scores["A"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["B"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["C"] == pytest.approx(1 / 63)


def test_strong_candidate_rules():
    w = corpus_work(1)
    assert Candidate(w, sources={"keyword", "semantic"}).strong
    assert Candidate(w, sources={"keyword"}, seed_connections=1).strong
    assert Candidate(w, sources=set(), seed_connections=2).strong
    assert not Candidate(w, sources={"semantic"}).strong


def test_expansion_results_respect_report_filters():
    filters = {"publication_year": "2015-2020", "is_oa": "true", "language": "en"}
    assert passes_filters(corpus_work(104, year=2016, is_open_access=True), filters)
    assert not passes_filters(corpus_work(104, year=2010, is_open_access=True), filters)
    assert not passes_filters(corpus_work(104, year=2016, is_open_access=False), filters)
    assert not passes_filters(corpus_work(104, year=2016, is_open_access=True, abstract=None), filters)
    assert not passes_filters(corpus_work(104, year=2016, is_open_access=True, is_retracted=True), filters)


async def test_retriever_expands_and_boosts_multi_seed_papers():
    settings = make_settings(MIN_PAPERS_PER_SUBQUESTION=3, RETRIEVAL_EXPANSION_SEEDS=3)
    fake = FakeOpenAlex()
    retriever = Retriever(fake, FakeLLMClient(settings), settings)
    candidates = await retriever.retrieve(central_question="Q?", question="Does social media use relate to anxiety?", filters={})
    assert candidates and len(candidates) <= settings.MAX_CANDIDATES_PER_SUBQUESTION
    assert any("expansion" in c.sources for c in candidates)
    assert [c.score for c in candidates] == sorted((c.score for c in candidates), reverse=True)
    assert any(name == "get_citing" for name, _ in fake.calls)
    assert sum(1 for name, _ in fake.calls if name == "search") == 1  # enough strong papers in round 1


async def test_retriever_searches_again_when_too_few_strong_candidates():
    settings = make_settings(MIN_PAPERS_PER_SUBQUESTION=500, MAX_RETRIEVAL_ROUNDS=3)
    fake = FakeOpenAlex()
    llm = FakeLLMClient(settings)
    await Retriever(fake, llm, settings).retrieve(central_question="Q?", question="Social media use and adolescent anxiety?", filters={})
    assert sum(1 for name, _ in fake.calls if name == "search") == 3
    term_calls = [c for c in llm.calls if c["task"] == "search_terms"]
    assert len(term_calls) == 3 and '"previousQueries": []' not in term_calls[-1]["user"].replace("\n", "")


def test_screening_keeps_2_3_and_1_only_for_background():
    assert is_included(3, "CORE") and is_included(2, "DISCUSSION")
    assert not is_included(1, "CORE") and is_included(1, "BACKGROUND")
    assert not is_included(0, "BACKGROUND")


# --------------------------------------------------------------- verifier
def test_unknown_w999_citation_is_removed_by_scrub():
    text = "Use rose sharply [W999]. Anxiety was linked to use [W1; W999]. Known [W2]."
    out = scrub_unknown(text, {"W1", "W2"})
    assert "W999" not in out
    assert out == "Use rose sharply. Anxiety was linked to use [W1]. Known [W2]."
    assert cited_ids(out) == ["W1", "W2"]


async def test_verifier_removes_w999_and_checks_claims():
    llm = FakeLLMClient(make_settings())
    sections = [DraftSection("body", "Findings", "sq1", "Teens use apps [W1]. A fabricated claim [W999].\n\n### Theme\n\nMore [W2].")]
    out, report = await Verifier(llm).verify(sections, known_ids={"W1", "W2"}, cards_by_work={"W1": [card("W1")], "W2": [card("W2")]})
    assert "W999" not in out[0].content
    assert report.removed_citations == {"W999": 1}
    assert "### Theme" in out[0].content
    assert report.checked == 2


async def test_verifier_rewrites_or_drops_unsupported_sentences():
    llm = FakeLLMClient(make_settings())

    def judge(system, data):
        verdicts = []
        for s in data["sentences"]:
            if "caused" in s["sentence"]:
                verdicts.append(SentenceVerdict(index=s["index"], supported=False, rewrite="Use was associated with anxiety [W1; W999]."))
            elif "cures" in s["sentence"]:
                verdicts.append(SentenceVerdict(index=s["index"], supported=False, rewrite=None))
            else:
                verdicts.append(SentenceVerdict(index=s["index"], supported=True, rewrite=None))
        return ClaimCheck(verdicts=verdicts)

    llm.set_handler("verify_claims", judge)
    text = "Social media caused anxiety [W1]. Exercise cures it [W2]. Teens were surveyed [W2]. Uncited context."
    out, report = await Verifier(llm).verify(
        [DraftSection("body", "H", None, text)], known_ids={"W1", "W2"}, cards_by_work={"W1": [card("W1")], "W2": [card("W2")]}
    )
    assert out[0].content == "Use was associated with anxiety [W1]. Teens were surveyed [W2]. Uncited context."
    assert report.rewritten == 1 and report.dropped == 1


# --------------------------------------------------------------------- APA
def _w(n, family, year, title, **kw):
    return Work(id=f"W{n}", title=title, year=year, authors=[Author(family=f, given="A.") for f in family], **kw)


def test_apa_references_sorted_and_in_text_forms():
    refs = apa.format_references(
        [
            _w(2, ["Zhang", "Wu", "Chen"], 2020, "Anxiety and screens", source="Psych Reports"),
            _w(1, ["Smith", "Lee"], 2021, "Teens online", source="Journal of Youth", volume="4", issue="2", pages="1-10", doi="https://doi.org/10.1/abc"),
            _w(3, ["Adams"], None, "Undated work", source="Blog"),
        ]
    )
    assert [r.work_id for r in refs] == ["W3", "W1", "W2"]
    by_id = {r.work_id: r for r in refs}
    assert by_id["W1"].in_text_citation == "(Smith & Lee, 2021)"
    assert by_id["W2"].in_text_citation == "(Zhang et al., 2020)"
    assert "n.d." in by_id["W3"].in_text_citation
    assert by_id["W1"].apa_citation == "Smith, A., & Lee, A. (2021). Teens online. *Journal of Youth*, *4*(2), 1–10. https://doi.org/10.1/abc"
    # Missing volume/issue/pages/DOI are omitted, not invented.
    assert by_id["W2"].apa_citation == "Zhang, A., Wu, A., & Chen, A. (2020). Anxiety and screens. *Psych Reports*."


def test_apa_disambiguates_same_author_year():
    refs = apa.format_references([_w(1, ["Smith"], 2021, "Beta study", source="J"), _w(2, ["Smith"], 2021, "Alpha study", source="J")])
    by_id = {r.work_id: r for r in refs}
    assert by_id["W2"].in_text_citation == "(Smith, 2021a)" and by_id["W1"].in_text_citation == "(Smith, 2021b)"
    assert "(2021a)" in by_id["W2"].apa_citation and "(2021b)" in by_id["W1"].apa_citation


def test_apply_citations_groups_in_reference_order():
    refs = {r.work_id: r for r in apa.format_references([_w(1, ["Zhang"], 2020, "Z", source="J"), _w(2, ["Adams"], 2019, "A", source="J")])}
    assert apa.apply_citations("Use rose [W1; W2]. Unknown [W9].", refs) == "Use rose (Adams, 2019; Zhang, 2020). Unknown."
