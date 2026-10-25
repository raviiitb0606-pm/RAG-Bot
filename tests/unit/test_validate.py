"""P4.6: validator checks V1-V7 (arch §6.7)."""

from dataclasses import replace

import pytest

from conftest import SOURCES
from mf_faq.ingest.chunk import Chunk
from mf_faq.pipeline.draft import AnswerDraft
from mf_faq.pipeline.validate import (
    check_advice_language,
    check_citation,
    check_consistency,
    check_no_links,
    check_number_grounding,
    check_performance,
    check_sentence_count,
    validate,
)

URLS = {s.id: s.url for s in SOURCES}
SRC = SOURCES[0].id

CHUNK = Chunk(
    chunk_id=f"{SRC}:exit_load",
    source_id=SRC,
    scheme_id=SOURCES[0].scheme_id,
    doc_type="scheme_page",
    publisher="GROWW",
    heading_path="Exit load",
    facts=("exit_load",),
    header="[Scheme: X | Source: Groww scheme page | Section: Exit load]",
    covers="exit load",
    text="Exit load of 1% if redeemed within 1 year. Expense ratio is 0.95%. "
    "Returns are taxed at 20%. Suitable for investors seeking long term wealth.",
)
CHUNKS = [CHUNK]


def draft(answer="The exit load is 1%.", status="answered", cited=CHUNK.chunk_id, also=()):
    return AnswerDraft(
        status=status, answer=answer, cited_chunk_id=cited, also_cited_chunk_ids=also
    )


# Same page, another fact group: a two-fact question ("exit load and fund size?").
SIZE = replace(
    CHUNK,
    chunk_id=f"{SRC}:fund_size",
    heading_path="Fund size",
    facts=("fund_size",),
    text="Fund size (AUM): ₹38,420.15 Cr.",
)
OTHER_PAGE = replace(SIZE, chunk_id=f"{SOURCES[1].id}:fund_size", source_id=SOURCES[1].id)
TWO_FACTS = "The exit load is 1% within 1 year, and the fund size is ₹38,420.15 Cr."


def codes(d, chunks=CHUNKS, urls=URLS):
    return validate(d, chunks, urls).codes


def test_good_answer_passes():
    result = validate(draft("The exit load is 1% within 1 year."), CHUNKS, URLS)
    assert result.ok and result.codes == ()


def test_v1_citation():
    assert check_citation(draft(), CHUNKS, URLS) == []
    assert check_citation(draft(cited="nope"), CHUNKS, URLS)[0].code == "V1"
    assert check_citation(draft(), CHUNKS, {})[0].code == "V1"
    assert check_citation(draft(), CHUNKS, {SRC: "https://example.com/x"})[0].code == "V1"
    assert "V1" in codes(draft(cited="nope"))


def test_v2_sentence_count():
    assert check_sentence_count("One. Two. Three.") == []
    assert check_sentence_count("One. Two. Three. Four.")[0].code == "V2"
    assert check_sentence_count("- a\n- b\n- c\n- d")[0].code == "V2"
    assert check_sentence_count("It is 0.95% p.a. approx. e.g. fine.") == []


@pytest.mark.parametrize(
    "text",
    [
        "See https://groww.in/x",
        "Visit www.hdfcfund.com",
        "See [here](http://a.b)",
        "Check groww.in for more",
        "Go to groww dot in",
        "Source: the page",
    ],
)
def test_v3_links(text):
    assert check_no_links(text)[0].code == "V3"


def test_v3_no_false_positive():
    assert check_no_links("The expense ratio is 1.05% and NAV is 12.34.") == []


def test_v4_number_grounding():
    assert check_number_grounding("Exit load is 1%.", CHUNK) == []
    assert check_number_grounding("Exit load is 2%.", CHUNK)[0].code == "V4"
    assert "V4" in codes(draft("The exit load is 3%."))


@pytest.mark.parametrize(
    "text",
    ["You should invest.", "I recommend it.", "It is a good choice.", "Better than others."],
)
def test_v5_advice(text):
    assert check_advice_language(text, CHUNK)[0].code == "V5"


def test_v5_quoted_label_is_allowed():
    label = "Ideal for investors seeking long term wealth."
    quoted = replace(CHUNK, text=label)
    assert check_advice_language(label, quoted) == []
    assert check_advice_language(label)[0].code == "V5"  # no chunk to quote from


@pytest.mark.parametrize(
    "text",
    ["The fund returned 15% last year.", "Its 3Y CAGR is high.", "Returns are 12%."],
)
def test_v6_performance(text):
    assert check_performance(text)[0].code == "V6"


def test_v6_tax_and_plain_facts_pass():
    assert check_performance("Returns are taxed at 20%.") == []
    assert check_performance("The exit load is 1% within 1 year.") == []


def test_v7_consistency():
    assert check_consistency(draft()) == []
    assert check_consistency(draft(answer=" "))[0].code == "V7"
    assert check_consistency(draft(cited=None))[0].code == "V7"
    assert check_consistency(draft(status="insufficient", answer="text", cited=None))
    assert check_consistency(draft(status="insufficient", answer="", cited=None)) == []


def test_non_answered_drafts_skip_other_checks():
    d = draft(status="advisory", answer="", cited=None)
    assert validate(d, CHUNKS, URLS).ok


def test_inconsistent_draft_short_circuits():
    assert codes(draft(answer="", cited=None)) == ("V7",)


def test_two_facts_from_two_chunks_of_one_page_pass():
    chunks = [CHUNK, SIZE]
    assert validate(draft(TWO_FACTS, also=(SIZE.chunk_id,)), chunks, URLS).ok


def test_two_facts_need_both_chunks_cited():
    result = validate(draft(TWO_FACTS), [CHUNK, SIZE], URLS)  # fund size not cited
    assert result.codes == ("V4",)


def test_extra_citation_must_be_sent_and_on_the_same_page():
    assert codes(draft(also=("nope",)), [CHUNK, SIZE]) == ("V1",)
    chunks = [CHUNK, OTHER_PAGE]
    assert "V1" in codes(draft(TWO_FACTS, also=(OTHER_PAGE.chunk_id,)), chunks)


def test_non_answered_draft_with_citations_is_inconsistent():
    assert codes(draft("", status="insufficient", cited=None, also=(SIZE.chunk_id,))) == ("V7",)
