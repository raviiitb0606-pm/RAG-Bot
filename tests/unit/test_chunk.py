"""P2.5 / P3.0: one chunk per fact group, header + covers line, holdings split by tokenizer."""

import re

import pytest

from conftest import CATALOG, page_html, source
from mf_faq.ingest.chunk import (
    MAX_TOKENS,
    ApproxTokenizer,
    ChunkError,
    approx_tokens,
    chunk_page,
)
from mf_faq.ingest.parse_html import Section, parse_page


def chunks_for(source_id: str, tokenizer=None, max_tokens: int = MAX_TOKENS):
    src = source(source_id)
    page = parse_page(page_html(source_id), src)
    return chunk_page(
        page.sections,
        src,
        CATALOG.by_source(source_id),
        CATALOG.covers,
        tokenizer=tokenizer,
        max_tokens=max_tokens,
    )


def holdings_rows(chunks) -> list[str]:
    return [row for c in chunks if c.facts == ("holdings",) for row in c.text.split("\n")[1:]]


def test_header_covers_and_metadata():
    chunks = chunks_for("groww-hdfc-mid-cap")
    exit_load = next(c for c in chunks if c.chunk_id == "groww-hdfc-mid-cap:exit_load")
    assert exit_load.header == (
        "[Scheme: HDFC Mid Cap Fund | Source: Groww scheme page | "
        "Section: Exit load, stamp duty and tax > Exit load]"
    )
    assert exit_load.covers_line == (
        "Covers: exit load, redemption charge, penalty or fee for withdrawing or selling "
        "units early."
    )
    assert exit_load.content == "\n".join([exit_load.header, exit_load.covers_line, exit_load.text])
    assert (exit_load.source_id, exit_load.scheme_id) == ("groww-hdfc-mid-cap", "hdfc-mid-cap")
    assert (exit_load.doc_type, exit_load.publisher) == ("scheme_page", "GROWW")
    assert exit_load.facts == ("exit_load",) and exit_load.group == "exit_load"


def test_one_chunk_per_small_group_and_unique_ids(source_id):
    chunks = chunks_for(source_id)
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))
    assert all(c.header.startswith("[Scheme: ") for c in chunks)
    assert all(approx_tokens(c.content) <= MAX_TOKENS for c in chunks)
    singles = [c for c in chunks if "holdings" not in c.chunk_id]
    assert all(c.chunk_id.count(":") == 1 for c in singles)


def test_covers_lines_hold_no_values(source_id):
    """The covers line describes the section; values only ever come from the page text."""
    for chunk in chunks_for(source_id):
        assert chunk.covers == CATALOG.covers[chunk.group]
        assert not re.search(r"[\d%₹]", chunk.covers_line), chunk.chunk_id
    assert set(CATALOG.covers) >= {c.group for c in chunks_for(source_id)}


def test_group_without_covers_line_is_an_error():
    src = source("groww-hdfc-mid-cap")
    with pytest.raises(ChunkError, match="no covers line"):
        chunk_page(
            [Section("new_group", ("New",), "Text.", ())], src, CATALOG.by_source(src.id), {}
        )


def test_long_holdings_split_with_lead_repeated():
    chunks = [c for c in chunks_for("groww-hdfc-multi-cap") if c.facts == ("holdings",)]
    assert len(chunks) > 1
    assert [c.chunk_id for c in chunks] == [
        f"groww-hdfc-multi-cap:holdings:{n}" for n in range(1, len(chunks) + 1)
    ]
    lead = "Portfolio holdings as of 30 Sep 2026 (123 holdings, % of fund assets):"
    assert all(c.text.split("\n")[0] == lead for c in chunks)
    assert len({c.header for c in chunks}) == 1 and len({c.covers for c in chunks}) == 1
    rows = holdings_rows(chunks)
    assert len(rows) == 123 and len(set(rows)) == 123  # nothing lost or duplicated


def test_short_holdings_not_split():
    holdings = [c for c in chunks_for("groww-hdfc-silver-etf-fof") if c.facts == ("holdings",)]
    assert [c.chunk_id for c in holdings] == ["groww-hdfc-silver-etf-fof:holdings"]


def test_single_long_paragraph_split_on_sentences():
    src = source("groww-hdfc-mid-cap")
    text = " ".join(f"Sentence number {i} has a few words." for i in range(60))
    chunks = chunk_page(
        [Section("long", ("Long",), text, ())],
        src,
        CATALOG.by_source(src.id),
        {"long": "a long section"},
        max_tokens=120,
    )
    assert len(chunks) > 1
    assert all(approx_tokens(c.content) <= 120 for c in chunks)
    assert " ".join(c.text.replace("\n", " ") for c in chunks) == text


def test_split_counts_with_the_given_tokenizer():
    """A tokenizer that counts more tokens than the estimate gives more, smaller parts."""

    class Doubling(ApproxTokenizer):
        def count_tokens(self, text: str) -> int:
            return 2 * approx_tokens(text)

    approx = chunks_for("groww-hdfc-multi-cap")
    doubled = chunks_for("groww-hdfc-multi-cap", tokenizer=Doubling())
    assert len(holdings_rows(doubled)) == len(holdings_rows(approx)) == 123
    assert len(doubled) > len(approx)
    assert all(Doubling().count_tokens(c.content) <= MAX_TOKENS for c in doubled)


def _model_cached(name: str) -> bool:
    from huggingface_hub import try_to_load_from_cache

    return isinstance(try_to_load_from_cache(name, "config.json"), str)


@pytest.mark.skipif(
    not _model_cached("BAAI/bge-small-en-v1.5"), reason="embedding model not downloaded"
)
def test_real_tokenizer_split_fits_the_model():
    """Research §3.1: by bge's own tokenizer every chunk is ≤ 400 tokens, so none is truncated."""
    from mf_faq.ingest.index import SentenceTransformerEmbedder

    model = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")
    total = 0
    for s in CATALOG.schemes:
        chunks = chunks_for(s.source_id, tokenizer=model)
        total += len(chunks)
        assert all(model.count_tokens(c.content) <= MAX_TOKENS for c in chunks)
        assert model.max_seq_length >= MAX_TOKENS
        rows = holdings_rows(chunks)
        assert len(rows) == len(set(rows)) == len(holdings_rows(chunks_for(s.source_id)))
    assert total == 86  # 19 holdings parts (research §3.1)
