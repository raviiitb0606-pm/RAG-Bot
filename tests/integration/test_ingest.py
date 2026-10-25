"""P2: full ingest on the saved pages; gates trip on a bad registry or a leftover returns chunk."""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from conftest import ROOT, SOURCES, FakeEmbedder, page_html
from mf_faq.cli import app
from mf_faq.config import get_settings
from mf_faq.ingest import build as build_module
from mf_faq.ingest.build import build_corpus, load_chunks
from mf_faq.ingest.fetch import read_meta, write_meta
from mf_faq.ingest.registry import RegistryError, is_allowed_url
from mf_faq.ingest.strip_performance import find_performance_patterns

MID = "groww-hdfc-mid-cap"
DAY1 = "2026-10-25T06:36:23+00:00"
DAY2 = "2026-11-25T06:36:23+00:00"
OBJECTIVE = "investing predominantly in Mid-Cap companies."


@pytest.fixture
def corpus(tmp_path) -> Path:
    corpus = tmp_path / "corpus"
    (corpus / "raw").mkdir(parents=True)
    for name in ("sources.yaml", "schemes.yaml"):
        shutil.copy(ROOT / "corpus" / name, corpus / name)
    for s in SOURCES:
        (corpus / "raw" / f"{s.id}.html").write_text(page_html(s.id), encoding="utf-8")
        write_meta(corpus / "raw", s.id, {"status": "ok", "downloaded_at": DAY1})
    return corpus


def build(corpus: Path):
    return build_corpus(corpus, corpus.parent / "data")


def edit_page(corpus: Path, source_id: str, old: str, new: str, downloaded_at: str) -> None:
    path = corpus / "raw" / f"{source_id}.html"
    html = path.read_text(encoding="utf-8")
    assert old in html
    path.write_text(html.replace(old, new), encoding="utf-8")
    meta = read_meta(corpus / "raw", source_id)
    write_meta(corpus / "raw", source_id, meta | {"downloaded_at": downloaded_at})


def snapshot(data: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(data.iterdir())}


def sources_table(data: Path) -> dict[str, tuple]:
    with sqlite3.connect(data / "app.db") as conn:
        rows = conn.execute("SELECT id, fetched_at, content_sha256, status, url FROM sources")
        return {r[0]: r[1:] for r in rows}


def test_full_build_writes_all_outputs(corpus):
    result = build(corpus)
    assert result.ok, result.failures
    data = corpus.parent / "data"

    chunks = load_chunks(data)
    manifest = json.loads((data / "manifest.json").read_text())
    assert manifest["chunk_count"] == len(chunks) == sum(len(p.chunks) for p in result.pages)
    assert [s["id"] for s in manifest["sources"]] == [s.id for s in SOURCES]
    assert all(s["has_performance_section"] for s in manifest["sources"])
    assert manifest["corpus_version"].startswith("2026-10-25.")
    assert (manifest["chunk_format_version"], manifest["tokenizer"]) == ("2", "approx")
    assert manifest["max_chunk_tokens"] <= manifest["max_tokens"] == 400

    table = sources_table(data)
    assert set(table) == {s.id for s in SOURCES}
    assert all(
        row[0] == DAY1 and row[2] == "ok" and is_allowed_url(row[3]) for row in table.values()
    )

    # Spot check (exit criteria): every chunk from a registered page, no return figures.
    assert {c.source_id for c in chunks} == {s.id for s in SOURCES}
    assert not [c.chunk_id for c in chunks if find_performance_patterns(c.content)]
    meta = read_meta(corpus / "raw", MID)
    assert meta["fetched_at"] == DAY1 and meta["content_sha256"] == table[MID][1]


def test_unchanged_facts_keep_fetched_at(corpus):
    # FET-08: re-downloaded a month later, ads changed, facts identical -> date stays.
    assert build(corpus).ok
    edit_page(corpus, MID, "</body>", "<div>New ad banner</div></body>", DAY2)
    assert build(corpus).ok
    assert sources_table(corpus.parent / "data")[MID][0] == DAY1


def test_changed_facts_move_fetched_at(corpus):
    assert build(corpus).ok
    version = json.loads((corpus.parent / "data" / "manifest.json").read_text())["corpus_version"]
    edit_page(corpus, MID, "₹213.42", "₹220.10", DAY2)
    assert build(corpus).ok
    table = sources_table(corpus.parent / "data")
    assert table[MID][0] == DAY2
    assert table["groww-hdfc-small-cap"][0] == DAY1  # other pages keep their own date
    manifest = json.loads((corpus.parent / "data" / "manifest.json").read_text())
    assert manifest["corpus_version"] != version


def test_stale_copy_still_builds(corpus):
    # FET-10: a failed fetch keeps the last good copy, marked stale.
    write_meta(corpus / "raw", MID, read_meta(corpus / "raw", MID) | {"status": "stale"})
    assert build(corpus).ok
    assert sources_table(corpus.parent / "data")[MID][2] == "stale"


@pytest.mark.parametrize("change", ["sixth", "other_groww_page", "duplicate_scheme"])
def test_bad_registry_stops_build(corpus, change):
    path = corpus / "sources.yaml"
    entries = yaml.safe_load(path.read_text())
    other = "https://groww.in/mutual-funds/hdfc-flexi-cap-fund-direct-growth"
    if change == "sixth":
        entries.append({**entries[0], "id": "groww-hdfc-flexi-cap", "url": other})
    elif change == "other_groww_page":
        entries[0]["url"] = other
    else:
        entries[1]["scheme_id"] = entries[0]["scheme_id"]
    path.write_text(yaml.safe_dump(entries, allow_unicode=True))
    with pytest.raises(RegistryError):
        build(corpus)
    assert not (corpus.parent / "data").exists()


def test_leftover_returns_chunk_trips_gate(corpus, monkeypatch):
    # Even if stripping were broken, the gate must stop the build (P2.7).
    assert build(corpus).ok
    data = corpus.parent / "data"
    before = snapshot(data)
    monkeypatch.setattr(build_module, "strip_performance_sections", lambda sections: sections)
    edit_page(corpus, MID, OBJECTIVE, OBJECTIVE + " It delivered 25% in 3 years.", DAY2)

    result = build(corpus)
    assert not result.ok
    assert any("investment_objective: return/performance pattern" in f for f in result.failures)
    assert snapshot(data) == before  # nothing written


def test_returns_sentence_in_page_is_stripped(corpus):
    # PAR-03: with stripping on, the same page builds and the sentence is gone.
    edit_page(corpus, MID, OBJECTIVE, OBJECTIVE + " It delivered 25% in 3 years.", DAY1)
    assert build(corpus).ok
    objective = next(
        c
        for c in load_chunks(corpus.parent / "data")
        if c.chunk_id == f"{MID}:investment_objective"
    )
    assert objective.text.endswith(OBJECTIVE)


def test_block_page_fails_build_and_keeps_old_outputs(corpus):
    # FET-05 / PAR-01: one page lost its facts -> whole build fails, live data untouched.
    assert build(corpus).ok
    before = snapshot(corpus.parent / "data")
    edit_page(corpus, MID, ">Fund benchmark<", ">Benchmark<", DAY2)
    result = build(corpus)
    assert not result.ok
    assert any(f.startswith(f"{MID}: label 'Fund benchmark'") for f in result.failures)
    assert snapshot(corpus.parent / "data") == before


def test_manual_copy_without_date_fails(corpus):
    # FET-09: no recorded save date -> build fails until one is supplied.
    (corpus / "raw" / f"{MID}.meta.json").unlink()
    result = build(corpus)
    assert not result.ok
    assert any("--manual-copy" in f for f in result.failures)
    assert not (corpus.parent / "data").exists()


def test_tokenizer_splits_and_gates_the_chunks(corpus):
    """P3.0: the model's tokenizer splits long sections; a chunk over its limit fails the build."""
    model = FakeEmbedder()
    result = build_corpus(corpus, corpus.parent / "data", tokenizer=model, max_tokens=150)
    assert result.ok, result.failures
    chunks = load_chunks(corpus.parent / "data")
    assert max(model.count_tokens(c.content) for c in chunks) <= 150
    assert json.loads((corpus.parent / "data" / "manifest.json").read_text())["tokenizer"] == (
        "fake/model"
    )

    model.max_seq_length = 100  # budget above the model's limit: every long chunk is caught
    before = snapshot(corpus.parent / "data")
    result = build_corpus(corpus, corpus.parent / "data", tokenizer=model, max_tokens=150)
    assert any("over fake/model's max_seq_length 100" in f for f in result.failures)
    assert snapshot(corpus.parent / "data") == before


def test_missing_copy_fails(corpus):
    (corpus / "raw" / f"{MID}.html").unlink()
    result = build(corpus)
    assert any(f.startswith(f"{MID}: no copy") for f in result.failures)
    assert any(f == f"{MID}: produced 0 chunks" for f in result.failures)


@pytest.fixture
def cli_env(corpus, monkeypatch):
    monkeypatch.setenv("MF_FAQ_CORPUS_DIR", str(corpus))
    monkeypatch.setenv("MF_FAQ_DATA_DIR", str(corpus.parent / "data"))
    monkeypatch.setattr("mf_faq.ingest.index.make_embedder", FakeEmbedder)
    monkeypatch.setattr("mf_faq.cli._index_gates", lambda embedder: [])  # fake vectors
    get_settings.cache_clear()
    yield corpus
    get_settings.cache_clear()


def test_cli_build_dump_and_manual_copy(cli_env):
    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "--skip-fetch"])
    assert result.exit_code == 0, result.output
    assert "corpus 2026-10-25." in result.output
    chunk_count = json.loads((cli_env.parent / "data" / "manifest.json").read_text())["chunk_count"]
    assert f"{chunk_count} vectors" in result.output and "(now live)" in result.output

    result = runner.invoke(app, ["ingest", "--dump", MID])
    assert result.exit_code == 0
    assert f"## {MID}:exit_load" in result.output
    assert "Exit load of 1% if redeemed within 1 year." in result.output

    assert runner.invoke(app, ["ingest", "--dump", "groww-hdfc-flexi-cap"]).exit_code == 2
    assert runner.invoke(app, ["ingest", "--manual-copy", MID]).exit_code == 2  # needs date

    result = runner.invoke(app, ["ingest", "--manual-copy", MID, "--saved-on", "2026-10-20"])
    assert result.exit_code == 0, result.output
    assert read_meta(cli_env / "raw", MID)["downloaded_at"] == "2026-10-20T00:00:00+05:30"


def test_cli_build_failure_exits_nonzero(cli_env):
    (cli_env / "raw" / f"{MID}.meta.json").unlink()
    result = CliRunner().invoke(app, ["ingest", "--skip-fetch"])
    assert result.exit_code == 1
    assert "nothing was written" in result.output
