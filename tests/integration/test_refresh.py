"""P7.3 / P7.5: a refresh run on restored state (fixture pages, `FakeEmbedder`, no network)."""

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from refresh_support import DAY1, INSTRUCTION, MID, Groww, make_workspace
from typer.testing import CliRunner

from conftest import SOURCES, FakeEmbedder, page_html
from mf_faq.cli import app
from mf_faq.config import get_settings
from mf_faq.ingest.fetch import read_meta
from mf_faq.ingest.index import current_version, load_index
from mf_faq.ingest.refresh import (
    FETCH_BLOCKED,
    GATE_FAILED,
    PUBLISHED,
    UNCHANGED,
    format_summary,
    run_refresh,
)
from mf_faq.ingest.state import STATUS_FILE

NOW = datetime(2026, 10, 26, 4, 30, tzinfo=UTC)  # 10:00 IST


@pytest.fixture(scope="module")
def restored(tmp_path_factory) -> Path:
    corpus, _ = make_workspace(tmp_path_factory.mktemp("restored"))
    return corpus.parent


@pytest.fixture
def ws(restored, tmp_path) -> tuple[Path, Path]:
    """A fresh copy of the restored state per test: (corpus, data)."""
    base = Path(shutil.copytree(restored, tmp_path / "ws"))
    return base / "corpus", base / "data"


def refresh(ws, groww: Groww, *, now=NOW, gates=lambda e: (), **kw):
    corpus, data = ws
    return run_refresh(
        corpus,
        data,
        embedder=FakeEmbedder(),
        query_instruction=INSTRUCTION,
        index_gates=gates,
        transport=groww.transport,
        sleep=lambda s: None,
        now=now,
        **kw,
    )


def fetched_at(corpus: Path) -> dict[str, str]:
    return {s.id: read_meta(corpus / "raw", s.id)["fetched_at"] for s in SOURCES}


def test_same_pages_are_unchanged(ws):
    corpus, data = ws
    live = current_version(data)
    corpus_version = json.loads((data / "manifest.json").read_text())["corpus_version"]

    report = refresh(ws, Groww())
    assert report.outcome == UNCHANGED and report.exit_code == 0
    assert report.old["index_version"] == report.new["index_version"] == live
    assert report.new["corpus_version"] == corpus_version
    assert all(s.status == "ok" and s.facts_changed is False for s in report.sources)
    assert fetched_at(corpus) == dict.fromkeys(fetched_at(corpus), DAY1)  # footer dates kept
    assert not report.alerts and not report.failures


def test_changed_facts_publish_and_move_only_that_date(ws):
    corpus, data = ws
    live = current_version(data)
    groww = Groww()
    groww.pages[MID] = page_html(MID).replace("₹213.42", "₹220.10")

    report = refresh(ws, groww)
    assert report.outcome == PUBLISHED and report.exit_code == 0
    assert report.new["index_version"] != live == report.old["index_version"]
    assert current_version(data) == report.new["index_version"]
    changed = {s.id for s in report.sources if s.facts_changed}
    assert changed == {MID}

    dates = fetched_at(corpus)
    assert dates[MID] != DAY1 and dates[MID] == read_meta(corpus / "raw", MID)["downloaded_at"]
    assert {d for sid, d in dates.items() if sid != MID} == {DAY1}
    # The new live index carries the new date with it (footer dates travel with the index).
    index = load_index(data, model_name="fake/model")
    assert {s["id"]: s["fetched_at"] for s in index_manifest(index)["sources"]}[MID] == dates[MID]
    index.close()


def index_manifest(index) -> dict:
    return json.loads((index.path / "manifest.json").read_text())


def test_missing_fact_label_fails_and_keeps_the_live_index(ws):
    corpus, data = ws
    live = current_version(data)
    before = (data / "manifest.json").read_bytes()
    groww = Groww()
    groww.pages[MID] = page_html(MID).replace(">Fund benchmark<", ">Benchmark<")

    report = refresh(ws, groww)
    assert report.outcome == GATE_FAILED and report.exit_code == 1
    assert any(f.startswith(f"{MID}: label 'Fund benchmark'") for f in report.failures)
    assert current_version(data) == live == report.new["index_version"]
    assert (data / "manifest.json").read_bytes() == before
    assert "### Failures" in format_summary(report)


def test_failed_index_gate_keeps_the_live_index(ws):
    _, data = ws
    live = current_version(data)
    groww = Groww()
    groww.pages[MID] = page_html(MID).replace("₹213.42", "₹220.10")
    report = refresh(ws, groww, gates=lambda e: [lambda index: ["retrieval eval: hit@4 0.50"]])
    assert report.outcome == GATE_FAILED and report.failures == ["retrieval eval: hit@4 0.50"]
    assert current_version(data) == live


def test_failed_fetch_goes_stale_and_the_third_day_alerts(ws):
    corpus, data = ws
    before = (corpus / "raw" / f"{MID}.html").read_bytes()
    groww = Groww()
    groww.fail[MID] = 503

    for day in range(3):
        report = refresh(ws, groww, now=NOW + timedelta(days=day))
        mid = next(s for s in report.sources if s.id == MID)
        assert mid.status == "stale" and mid.http_status == 503 and mid.stale_days == day + 1
        assert report.outcome == UNCHANGED  # the previous copy still builds
        assert (corpus / "raw" / f"{MID}.html").read_bytes() == before
        assert read_meta(corpus / "raw", MID)["fetched_at"] == DAY1
        if day < 2:
            assert not report.alerts
    assert len(report.alerts) == 1 and report.alerts[0].startswith(f"{MID}: stale for 3 days")
    assert "### Alerts" in format_summary(report)

    status = json.loads((data / STATUS_FILE).read_text())["sources"][MID]
    assert status["stale_since"] == "2026-10-26"
    groww.fail.clear()  # a good fetch ends the streak
    report = refresh(ws, groww, now=NOW + timedelta(days=3))
    assert not report.alerts and all(s.stale_days == 0 for s in report.sources)


def test_two_runs_on_one_day_count_as_one_stale_day(ws):
    groww = Groww()
    groww.fail[MID] = 503
    refresh(ws, groww)
    report = refresh(ws, groww, now=NOW + timedelta(hours=3))  # a manual re-run
    assert next(s for s in report.sources if s.id == MID).stale_days == 1


def test_every_fetch_failing_is_fetch_blocked(ws):
    _, data = ws
    live = current_version(data)
    groww = Groww()
    groww.fail.update(dict.fromkeys((s.id for s in SOURCES), 403))
    report = refresh(ws, groww)
    assert report.outcome == FETCH_BLOCKED and report.exit_code == 1
    assert len(report.failures) == 5 and current_version(data) == live


def test_cli_ingest_report(ws, tmp_path, monkeypatch):
    corpus, data = ws
    monkeypatch.setenv("MF_FAQ_CORPUS_DIR", str(corpus))
    monkeypatch.setenv("MF_FAQ_DATA_DIR", str(data))
    monkeypatch.setenv("MF_FAQ_EMBED_MODEL", "fake/model")  # the model the live index used
    monkeypatch.setenv("MF_FAQ_EMBED_MODEL_REVISION", "r1")
    monkeypatch.setattr("mf_faq.ingest.index.make_embedder", FakeEmbedder)
    monkeypatch.setattr("mf_faq.cli._index_gates", lambda embedder: [])  # fake vectors
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        out, summary = tmp_path / "report.json", tmp_path / "summary.md"
        args = ["ingest", "--skip-fetch", "--report", str(out), "--summary", str(summary)]
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        report = json.loads(out.read_text())
        assert report["outcome"] == UNCHANGED and report["exit_code"] == 0
        assert report["fetched"] is False and len(report["sources"]) == 5
        assert summary.read_text().startswith("## Daily refresh")

        (corpus / "raw" / f"{MID}.meta.json").unlink()  # no download date → the build fails
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert json.loads(out.read_text())["outcome"] == GATE_FAILED

        assert runner.invoke(app, [*args, "--fetch-only"]).exit_code == 2
    finally:
        get_settings.cache_clear()
