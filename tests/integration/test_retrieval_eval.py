"""P3.5 / P3.6: the retrieval eval, its gates, the swap gate and the CLI; Phase 3 exit criteria."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import CATALOG, ROOT, SOURCES, FakeEmbedder, page_html
from mf_faq.cli import app
from mf_faq.config import Settings, get_settings
from mf_faq.eval.retrieval import (
    IN_CORPUS_SETS,
    OUT_OF_CORPUS_SETS,
    Case,
    Outcome,
    evaluate,
    format_report,
    load_cases,
    retrieval_gate,
    summarize,
)
from mf_faq.ingest.build import build_corpus
from mf_faq.ingest.fetch import write_meta
from mf_faq.ingest.index import build_index, current_version, load_index
from mf_faq.pipeline.retrieve import Evidence, NotFound, Retriever, SearchResult, Status

EVAL_DIR = ROOT / "tests" / "eval"
MODEL = "BAAI/bge-small-en-v1.5"
DAY1 = "2026-10-25T06:36:23+00:00"
SETTINGS = Settings(_env_file=None)
INSTRUCTION = SETTINGS.embed_query_instruction


def _model_cached(name: str) -> bool:
    from huggingface_hub import try_to_load_from_cache

    return isinstance(try_to_load_from_cache(name, "config.json"), str)


needs_model = pytest.mark.skipif(not _model_cached(MODEL), reason="embedding model not downloaded")


def make_corpus(base: Path) -> Path:
    corpus = base / "corpus"
    (corpus / "raw").mkdir(parents=True)
    for name in ("sources.yaml", "schemes.yaml"):
        shutil.copy(ROOT / "corpus" / name, corpus / name)
    for s in SOURCES:
        (corpus / "raw" / f"{s.id}.html").write_text(page_html(s.id), encoding="utf-8")
        write_meta(corpus / "raw", s.id, {"status": "ok", "downloaded_at": DAY1})
    return corpus


def gate_for(encoder, eval_dir: Path = EVAL_DIR):
    return retrieval_gate(
        encoder,
        CATALOG,
        eval_dir,
        query_instruction=SETTINGS.embed_query_instruction,
        top_k=SETTINGS.retrieval_top_k,
        threshold=SETTINGS.retrieval_threshold,
    )


# --- query sets -------------------------------------------------------------------------------


def test_query_sets_load():
    cases = load_cases(EVAL_DIR)
    sets: dict[str, list[Case]] = {}
    for c in cases:
        sets.setdefault(c.set, []).append(c)
    assert len(sets["golden"]) == 36  # 31 seeded answers + 5 holdings cases (P3.5)
    assert len(sets["golden_not_found"]) == 11
    assert (len(sets["paraphrase"]), len(sets["heldout"]), len(sets["holdings"])) == (48, 30, 3)
    assert (len(sets["ooc_scheme"]), len(sets["ooc_general"])) == (16, 13)
    assert len({c.id for c in cases}) == len(cases)
    thread = [c for c in cases if c.thread == "T-01"]
    assert [c.id for c in thread] == ["T-01.1", "T-01.2", "T-01.3"]


def test_every_answer_case_asks_an_available_fact():
    for c in load_cases(EVAL_DIR):
        if c.expect_kind == "answer":
            assert c.fact in CATALOG.by_source(c.expect_source).available_facts, c.id


# --- gates ------------------------------------------------------------------------------------


def _search(cosine: float = 0.8, ms: float = 5.0) -> SearchResult:
    return SearchResult(hits=(), top_cosine=cosine, top_bm25=1.0, latency_ms=ms)


def _answer(case_id: str, rank: int | None, status: Status = Status.OK, **search) -> Outcome:
    case = Case("golden", case_id, "q", "answer", "groww-hdfc-mid-cap", "exit_load")
    reason = NotFound.LOW_SIMILARITY if status is Status.INSUFFICIENT else None
    return Outcome(
        case, Evidence(status, ("hdfc-mid-cap",), reason, search=_search(**search)), rank
    )


def _stopped(case_id: str, set_name: str = "ooc_scheme", stopped: bool = True) -> Outcome:
    case = Case(set_name, case_id, "q", "not_found")
    if stopped:
        return Outcome(case, Evidence(Status.INSUFFICIENT, (), NotFound.UNSUPPORTED_TOPIC))
    return Outcome(case, Evidence(Status.OK, ("hdfc-mid-cap",), search=_search()))


def test_summary_passes_and_suggests_tau():
    outcomes = [_answer(f"a{i}", 0, cosine=0.75 + i / 100) for i in range(20)]
    outcomes += [_stopped(f"n{i}") for i in range(20)]
    report = summarize(outcomes, top_k=4, threshold=0.69)
    assert report.ok, report.failures
    assert report.sets["golden"].metrics["hit@1"] == 1.0
    assert report.cosine["in_corpus"]["min"] == 0.75
    assert report.cosine["tau_range"] == [0.7, 0.72]
    assert "is OUTSIDE it" in format_report(report)
    assert "PASS" in format_report(report)


def test_summary_gates_fail_loudly():
    outcomes = [_answer(f"a{i}", 0) for i in range(18)]
    outcomes += [_answer("miss", None), _answer("nf", None, Status.INSUFFICIENT)]
    outcomes += [_answer("slow", 3, ms=900.0) for _ in range(3)]
    outcomes += [_stopped(f"n{i}") for i in range(8)] + [_stopped("leak", stopped=False)]
    report = summarize(outcomes, top_k=4, threshold=0.69)
    text = " | ".join(report.failures)
    assert "golden: hit@4" in text and "golden: hit@1" in text
    assert "1 false not-found {'low_similarity': 1}" in text
    assert "out-of-corpus: 0.889 stopped" in text
    assert "latency p95" in text
    misses = {m["id"] for m in report.sets["golden"].misses}
    assert misses == {"miss", "nf"}
    assert "FAIL" in format_report(report)


# --- real model: the Phase 3 exit criteria ----------------------------------------------------


@pytest.fixture(scope="module")
def real_index(tmp_path_factory):
    from mf_faq.ingest.index import SentenceTransformerEmbedder

    base = tmp_path_factory.mktemp("real")
    model = SentenceTransformerEmbedder(MODEL)
    result = build_corpus(make_corpus(base), base / "data", tokenizer=model)
    assert result.ok, result.failures
    built = build_index(
        base / "data",
        model,
        query_instruction=SETTINGS.embed_query_instruction,
        gates=[gate_for(model)],
    )
    assert built.ok and built.swapped, built.failures  # the eval passed as the swap gate
    return base / "data", model


@needs_model
def test_exit_criteria_with_the_real_model(real_index):
    data, model = real_index
    index = load_index(data, model_name=MODEL, query_instruction=SETTINGS.embed_query_instruction)
    assert index.meta["chunk_count"] == 86 and index.meta["max_chunk_tokens"] <= 400
    retriever = Retriever(
        index,
        model,
        CATALOG,
        query_instruction=SETTINGS.embed_query_instruction,
        threshold=SETTINGS.retrieval_threshold,
    )
    report = evaluate(retriever, CATALOG, EVAL_DIR)
    assert report.ok, format_report(report)

    sets = report.sets
    assert sets["golden"].metrics["hit@4"] >= 0.95 and sets["golden"].metrics["hit@1"] >= 0.85
    assert sets["heldout"].metrics["hit@4"] >= 0.95
    assert sets["paraphrase"].metrics["hit@4"] >= 0.95
    assert all(sets[s].metrics["false_not_found"] == 0 for s in IN_CORPUS_SETS)
    stopped = [sets[s].metrics["stopped"] for s in OUT_OF_CORPUS_SETS]
    assert min(stopped) >= 0.95
    assert report.latency_ms["p95"] < 300
    low, high = report.cosine["tau_range"]  # P3.6: τ sits 0.03-0.05 under the in-corpus min
    assert low <= SETTINGS.retrieval_threshold <= high


# --- swap gate and CLI ------------------------------------------------------------------------


@pytest.fixture
def fake_data(tmp_path, monkeypatch):
    corpus = make_corpus(tmp_path)
    assert build_corpus(corpus, tmp_path / "data").ok
    monkeypatch.setenv("MF_FAQ_CORPUS_DIR", str(corpus))
    monkeypatch.setenv("MF_FAQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MF_FAQ_EVAL_DIR", str(EVAL_DIR))
    monkeypatch.setenv("MF_FAQ_EMBED_MODEL", "fake/model")
    monkeypatch.setenv("MF_FAQ_EMBED_MODEL_REVISION", "r1")  # FakeEmbedder's revision
    monkeypatch.setattr("mf_faq.ingest.index.make_embedder", FakeEmbedder)
    get_settings.cache_clear()
    yield tmp_path / "data"
    get_settings.cache_clear()


def test_failing_eval_keeps_the_live_index(fake_data):
    """Random vectors can't find the right sections, so the gate blocks the swap."""
    live = build_index(fake_data, FakeEmbedder(), query_instruction=INSTRUCTION).version
    result = build_index(
        fake_data,
        FakeEmbedder("fake/other"),
        query_instruction=INSTRUCTION,
        gates=[gate_for(FakeEmbedder("fake/other"))],
    )
    assert not result.swapped and result.failures
    assert all(f.startswith("retrieval eval: ") for f in result.failures)
    assert current_version(fake_data) == live


def test_missing_query_sets_fail_the_gate(fake_data, tmp_path):
    result = build_index(
        fake_data,
        FakeEmbedder(),
        query_instruction=INSTRUCTION,
        gates=[gate_for(FakeEmbedder(), tmp_path / "nowhere")],
    )
    assert not result.swapped and "set MF_FAQ_EVAL_DIR" in result.failures[0]


def test_cli_index_runs_the_eval_gate(fake_data):
    result = CliRunner().invoke(app, ["index"])
    assert result.exit_code == 1
    assert "live index is unchanged" in result.output and "retrieval eval:" in result.output
    assert current_version(fake_data) is None


def test_cli_eval_retrieval_only(fake_data, tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["eval", "--retrieval-only"])
    assert result.exit_code == 1 and "run `mf-faq ingest` first" in result.output

    build_index(fake_data, FakeEmbedder(), query_instruction=INSTRUCTION)
    out = tmp_path / "report.json"
    result = runner.invoke(app, ["eval", "--retrieval-only", "--json", str(out)])
    assert result.exit_code == 1  # fake vectors fail the gates, and the exit code says so
    assert "Retrieval eval (top_k=4" in result.output and "FAIL:" in result.output
    report = json.loads(out.read_text())
    assert not report["ok"] and set(report["sets"]) >= {"golden", "heldout", "ooc_scheme"}
    # Layers A-C don't depend on the vectors: every out-of-corpus question is still stopped.
    assert report["sets"]["ooc_scheme"]["stopped"] == 1.0

    assert runner.invoke(app, ["eval"]).exit_code == 1  # full eval is Phase 9
