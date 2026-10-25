"""P3.3 / P3.4: hybrid retrieval, RRF, the scheme mask, and the not-found layers A–D."""

import uuid
from pathlib import Path

import chromadb
import numpy as np
import pytest

from conftest import CATALOG, SOURCES, FakeEmbedder, page_html
from mf_faq.eval.retrieval import load_cases
from mf_faq.ingest.build import process_html
from mf_faq.ingest.chunk import Chunk
from mf_faq.ingest.index import IndexLoadError, LoadedIndex, chroma_settings, write_collection
from mf_faq.pipeline.retrieve import (
    RRF_K,
    NotFound,
    Retriever,
    Status,
    bm25_document,
    bm25_tokens,
)
from mf_faq.pipeline.schemes import Reason, SchemeResolution, SchemeResolver
from mf_faq.pipeline.scope import unsupported_topic

ROOT = Path(__file__).resolve().parents[2]
INSTRUCTION = "Represent this sentence for searching relevant passages: "
MC, SC = "hdfc-mid-cap", "hdfc-small-cap"
CASES = load_cases(ROOT / "tests" / "eval")
IN_CORPUS = [c for c in CASES if c.expect_kind == "answer"]
RESOLVER = SchemeResolver(CATALOG)


def make_index(chunks: list[Chunk], encoder, vectors=None) -> LoadedIndex:
    """An in-memory Chroma collection with the same records the index build writes."""
    vectors = encoder.encode([c.content for c in chunks]) if vectors is None else vectors
    vectors = np.asarray(vectors, np.float32)
    client = chromadb.EphemeralClient(settings=chroma_settings())
    collection = write_collection(client, chunks, vectors, name=f"test-{uuid.uuid4().hex}")
    meta = {
        "embed_model": encoder.model_name,
        "embed_model_revision": encoder.revision,
        "dim": encoder.dimension,
        "query_instruction": INSTRUCTION,
    }
    scheme_ids = np.array([c.scheme_id for c in chunks])
    return LoadedIndex("test", Path("."), meta, chunks, vectors, scheme_ids, collection)


@pytest.fixture(scope="module")
def corpus_chunks() -> list[Chunk]:
    return [c for s in SOURCES for c in process_html(page_html(s.id), s, CATALOG)[1]]


@pytest.fixture
def encoder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def retriever(corpus_chunks, encoder) -> Retriever:
    return Retriever(
        make_index(corpus_chunks, encoder), encoder, CATALOG, query_instruction=INSTRUCTION
    )


def matched(*scheme_ids: str) -> SchemeResolution:
    return SchemeResolution(tuple(scheme_ids), Reason.MATCHED)


# --- BM25 tokeniser ---------------------------------------------------------------------------


def test_bm25_tokens_drop_scheme_words_and_stopwords_and_stem():
    assert bm25_tokens("Who manages the HDFC Mid Cap Fund – Direct Growth?") == ["manag"]
    assert bm25_tokens("fund managers") == ["manag"]  # "managers" and "manages" meet
    assert bm25_tokens("Exit load of 1.5% within 365 days") == [
        "exit",
        "load",
        "1.5",
        "within",
        "365",
        "days",
    ]
    assert bm25_tokens("₹1,01,856.06 Cr") == ["1,01,856.06", "cr"]


def test_bm25_document_has_no_header(corpus_chunks):
    chunk = corpus_chunks[0]
    doc = bm25_document(chunk)
    assert chunk.header not in doc
    assert doc == f"{chunk.heading_path}\n{chunk.covers_line}\n{chunk.text}"


# --- RRF and the mask -------------------------------------------------------------------------


def _chunk(group: str, heading: str, text: str, scheme_id: str = MC) -> Chunk:
    source_id = CATALOG.by_id(scheme_id).source_id
    return Chunk(
        chunk_id=f"{source_id}:{group}",
        source_id=source_id,
        scheme_id=scheme_id,
        doc_type="scheme_page",
        publisher="GROWW",
        heading_path=heading,
        facts=(group,),
        header=f"[Scheme: x | Section: {heading}]",
        covers="section",
        text=text,
    )


class FixedEncoder(FakeEmbedder):
    """Every query embeds to the first basis vector."""

    def __init__(self):
        super().__init__(dimension=2)

    def encode(self, texts):
        self.calls += 1
        return np.array([[1.0, 0.0]] * len(texts), np.float32)


def test_rrf_maths():
    chunks = [
        _chunk("exit_load", "exit", "Exit load of 1%."),
        _chunk("expense_ratio", "expense", "Expense ratio: 0.74%."),
        _chunk("benchmark", "benchmark", "Benchmark: NIFTY Midcap 150."),
    ]
    vectors = [[1.0, 0.0], [0.6, 0.8], [0.0, 1.0]]  # cosines 1.0, 0.6, 0.0
    encoder = FixedEncoder()
    retriever = Retriever(
        make_index(chunks, encoder, vectors), encoder, CATALOG, query_instruction=INSTRUCTION
    )
    result = retriever.search("What is the expense ratio?", MC)
    # Dense order: exit, expense, benchmark. BM25: only expense scores (> 0).
    assert [h.chunk.group for h in result.hits] == ["expense_ratio", "exit_load", "benchmark"]
    expense, exit_load, benchmark = result.hits
    assert expense.rrf == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    assert exit_load.rrf == pytest.approx(1 / (RRF_K + 1))
    assert benchmark.rrf == pytest.approx(1 / (RRF_K + 3))
    assert (expense.dense_rank, expense.bm25_rank) == (1, 0)
    assert (exit_load.bm25_rank, exit_load.bm25) == (None, 0.0)
    assert result.top_cosine == pytest.approx(1.0) and result.top_bm25 > 0


def test_top_k_limits_the_hits(corpus_chunks, encoder):
    index = make_index(corpus_chunks, encoder)
    for k in (1, 4, 6):
        retriever = Retriever(index, encoder, CATALOG, query_instruction=INSTRUCTION, top_k=k)
        assert len(retriever.search("exit load", MC).hits) == k


def test_mask_never_returns_another_scheme(retriever):
    """Even a question naming another scheme only sees the resolved scheme's chunks."""
    for scheme in CATALOG.schemes:
        for text in ("What is the exit load of HDFC Small Cap Fund?", "holdings", "ICICI Bank"):
            result = retriever.search(text, scheme.scheme_id)
            assert result.hits
            assert {h.chunk.scheme_id for h in result.hits} == {scheme.scheme_id}


def test_query_gets_the_scheme_name_once(retriever):
    assert (
        retriever.query_text("And its exit load?", MC) == "And its exit load? (HDFC Mid Cap Fund)"
    )
    assert retriever.query_text("Exit load of hdfc mid cap fund", MC) == (
        "Exit load of hdfc mid cap fund"
    )


def test_query_is_embedded_with_the_instruction(corpus_chunks):
    seen = []

    class Recording(FakeEmbedder):
        def encode(self, texts):
            seen.extend(texts)
            return super().encode(texts)

    encoder = Recording()
    index = make_index(corpus_chunks, FakeEmbedder())
    Retriever(index, encoder, CATALOG, query_instruction=INSTRUCTION).search("exit load?", SC)
    assert seen == [INSTRUCTION + "exit load? (HDFC Small Cap Fund)"]


def test_unknown_scheme_is_an_error(retriever):
    with pytest.raises(ValueError, match="no chunks"):
        retriever.search("exit load", "hdfc-flexi-cap")


@pytest.mark.parametrize(
    "change, message",
    [
        ({"model_name": "other/model"}, "embed_model"),
        ({"revision": "r2"}, "embed_model_revision"),
        ({"dimension": 16}, "dim"),
    ],
)
def test_refuses_an_encoder_the_index_was_not_built_with(corpus_chunks, change, message):
    index = make_index(corpus_chunks, FakeEmbedder())
    encoder = FakeEmbedder()
    for key, value in change.items():
        setattr(encoder, key, value)
    with pytest.raises(IndexLoadError, match=message):
        Retriever(index, encoder, CATALOG, query_instruction=INSTRUCTION)
    with pytest.raises(IndexLoadError, match="query_instruction"):
        Retriever(index, FakeEmbedder(), CATALOG, query_instruction="query: ")


# --- not-found layers A–D ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, active, status, reason",
    [
        ("What is the exit load of SBI Small Cap Fund?", None, Status.INSUFFICIENT, "out_of_scope"),
        ("Expense ratio of HDFC Flexi Cap Fund?", None, Status.INSUFFICIENT, "out_of_scope"),
        ("What is the lock-in period for ELSS funds?", MC, Status.INSUFFICIENT, "no_scheme"),
        ("What is a mutual fund?", None, Status.INSUFFICIENT, "no_scheme"),
        ("Does HDFC Mid Cap Fund have a lock-in?", None, Status.INSUFFICIENT, "unsupported_topic"),
        ("What is its Sharpe ratio?", MC, Status.INSUFFICIENT, "unsupported_topic"),
        ("What is the lock-in?", None, Status.INSUFFICIENT, "no_scheme"),
        ("What is its lock-in?", None, Status.INSUFFICIENT, "unsupported_topic"),
        ("What is the exit load?", None, Status.NEEDS_SCHEME, None),
        ("Exit load of HDFC Mid Cap and HDFC Small Cap?", None, Status.MULTI_SCHEME, None),
    ],
)
def test_layers_stop_before_retrieval(corpus_chunks, text, active, status, reason):
    encoder = FakeEmbedder()
    retriever = Retriever(
        make_index(corpus_chunks, encoder), encoder, CATALOG, query_instruction=INSTRUCTION
    )
    encoder.calls = 0
    evidence = retriever.evidence(text, RESOLVER.resolve(text, active))
    assert evidence.status is status
    assert (evidence.reason.value if evidence.reason else None) == reason
    assert evidence.search is None and evidence.hits == ()
    assert encoder.calls == 0  # nothing embedded: no retrieval at all


def test_layer_details_name_what_decided(retriever):
    text = "What is the exit load of SBI Small Cap Fund?"
    assert retriever.evidence(text, RESOLVER.resolve(text)).detail == ("SBI",)
    text = "What is the Sharpe ratio of HDFC Mid Cap Fund?"
    evidence = retriever.evidence(text, RESOLVER.resolve(text))
    assert evidence.detail == ("risk_ratio",) and evidence.scheme_ids == (MC,)


def test_layer_d_dense_floor(corpus_chunks, encoder):
    index = make_index(corpus_chunks, encoder)
    text = "What is the exit load of HDFC Mid Cap Fund?"
    strict = Retriever(index, encoder, CATALOG, query_instruction=INSTRUCTION, threshold=1.01)
    evidence = strict.evidence(text, matched(MC))
    assert (evidence.status, evidence.reason) == (Status.INSUFFICIENT, NotFound.LOW_SIMILARITY)
    assert evidence.search is not None and evidence.hits == ()  # scores kept for logs, no chunks
    assert evidence.detail == (f"{evidence.search.top_cosine:.3f}",)

    lenient = Retriever(index, encoder, CATALOG, query_instruction=INSTRUCTION, threshold=-1.0)
    evidence = lenient.evidence(text, matched(MC))
    assert evidence.status is Status.OK and evidence.reason is None
    assert len(evidence.hits) == 4 and evidence.scheme_id == MC


def test_carry_over_retrieves_from_the_thread_scheme(retriever):
    text = "And its exit load?"
    evidence = retriever.evidence(text, RESOLVER.resolve(text, SC))
    assert evidence.status in (Status.OK, Status.INSUFFICIENT)
    assert evidence.search and {h.chunk.scheme_id for h in evidence.search.hits} == {SC}


# --- lexicon (layer C) ------------------------------------------------------------------------


@pytest.mark.parametrize("case", IN_CORPUS, ids=lambda c: c.id)
def test_lexicon_passes_every_in_corpus_question(case):
    assert unsupported_topic(case.q) is None


def test_lexicon_passes_every_chunk_line_and_holding(corpus_chunks):
    """Holdings names ("Alpha…", "Beta…", "Rating…") must not look like unsupported topics."""
    lines = [line for c in corpus_chunks for line in c.text.split("\n")]
    assert len(lines) > 300
    assert [line for line in lines if unsupported_topic(line)] == []


def test_lexicon_catches_every_scheme_scoped_out_of_corpus_question():
    cases = [c for c in CASES if c.set == "ooc_scheme"]
    assert len(cases) >= 16
    assert [c.q for c in cases if unsupported_topic(c.q) is None] == []


@pytest.mark.parametrize(
    "text, topic",
    [
        ("Does it have a lock in?", "lock_in"),
        ("When was HDFC Mid Cap Fund launched?", "launch_date"),
        ("Groww rating of HDFC Multi Cap Fund", "rating"),
        ("What is the beta of HDFC Small Cap Fund?", "risk_ratio"),
        ("Does HDFC Large Cap pay dividends?", "idcw"),
        ("Expense ratio of the regular plan of HDFC Mid Cap Fund?", "regular_plan"),
        ("How do I switch from regular to direct?", "how_to"),
        ("How can I redeem HDFC Small Cap Fund?", "how_to"),
        ("Can I claim 80C for HDFC Mid Cap Fund?", "elss"),
    ],
)
def test_lexicon_topics(text, topic):
    assert unsupported_topic(text) == topic
