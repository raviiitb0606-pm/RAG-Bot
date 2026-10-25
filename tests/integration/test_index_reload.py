"""P7.6: a running API swaps to the index `current` points at, without a restart."""

import json
import logging
import time
from pathlib import Path

import pytest
from api_support import make_pipeline
from fastapi.testclient import TestClient

from conftest import SOURCES
from mf_faq.api.app import create_app
from mf_faq.config import Settings
from mf_faq.ingest.index import CURRENT, index_root
from mf_faq.pipeline.sources import SourceRegistry
from mf_faq.threads.store import ThreadStore

QUESTION = {"text": "What is the exit load of HDFC Mid Cap Fund?"}
VERSIONS = {  # index version -> (corpus version, fetched_at of every page)
    "2026-10-25.aaaa-11111111": ("2026-10-25.aaaa", "2026-10-25T06:36:23+00:00"),
    "2026-10-27.bbbb-22222222": ("2026-10-27.bbbb", "2026-10-27T04:31:00+00:00"),
}
A, B = VERSIONS


def write_version(data: Path, version: str) -> None:
    corpus_version, fetched_at = VERSIONS[version]
    folder = index_root(data) / version
    folder.mkdir(parents=True)
    manifest = {
        "corpus_version": corpus_version,
        "sources": [
            {
                "id": s.id,
                "url": s.url,
                "title": s.title,
                "scheme_id": s.scheme_id,
                "fetched_at": fetched_at,
                "status": "ok",
            }
            for s in SOURCES
        ],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest))


def point(data: Path, version: str) -> None:
    (index_root(data) / CURRENT).write_text(version + "\n")


class Loader:
    """Builds the test pipeline for a version from that version's own manifest."""

    def __init__(self, data: Path) -> None:
        self.data = data
        self.loaded: list[str] = []
        self.closed: list[str] = []

    def __call__(self, version: str):
        registry = SourceRegistry.from_manifest(index_root(self.data) / version / "manifest.json")
        pipeline = make_pipeline(registry=registry)
        closed = self.closed

        class Index:  # stands in for LoadedIndex: records when the old pipeline is released
            def close(self) -> None:
                closed.append(version)

        pipeline.retriever.index = Index()
        self.loaded.append(version)
        return pipeline


@pytest.fixture
def setup(tmp_path):
    data = tmp_path / "data"
    for version in VERSIONS:
        write_version(data, version)
    point(data, A)
    return data, Loader(data)


def make(data: Path, loader: Loader, reload_s: float = 0) -> TestClient:
    settings = Settings(
        _env_file=None, data_dir=data, api_rate_limit="1000/minute", index_reload_s=reload_s
    )
    app = create_app(
        settings,
        pipeline=loader(A),
        store=ThreadStore(settings.db_path),
        index_version=A,
        corpus_version=VERSIONS[A][0],
        web_dir=None,
        loader=loader,
    )
    return TestClient(app)


def ask(api: TestClient) -> dict:
    tid = api.post("/api/threads").json()["id"]
    return api.post(f"/api/threads/{tid}/messages", json=QUESTION).json()


def test_reload_tick_swaps_to_the_new_version(setup):
    data, loader = setup
    with make(data, loader) as api:
        svc = api.app.state.service
        assert api.get("/api/health").json()["index_version"] == A
        assert ask(api)["last_updated"] == "25 Oct 2026"

        assert not svc.reload_if_changed()  # nothing changed: no reload
        point(data, B)
        assert svc.reload_if_changed()
        health = api.get("/api/health").json()
        assert (health["index_version"], health["corpus_version"]) == (B, VERSIONS[B][0])
        assert api.get("/api/meta").json()["corpus_version"] == VERSIONS[B][0]
        assert ask(api)["last_updated"] == "27 Oct 2026"  # footer dates came with the index

        assert loader.closed == []  # in-flight turns may still hold A…
        svc.reload_if_changed()
        assert loader.closed == [A]  # …so it is released a tick later


def test_broken_version_keeps_serving_the_old_one(setup, caplog):
    data, loader = setup
    caplog.set_level(logging.ERROR)
    (index_root(data) / "broken-33333333").mkdir()  # no manifest: unloadable
    with make(data, loader) as api:
        point(data, "broken-33333333")
        assert not api.app.state.service.reload_if_changed()
        assert api.get("/api/health").json()["index_version"] == A
        assert ask(api)["last_updated"] == "25 Oct 2026"
    failed = [r for r in caplog.records if r.getMessage() == "index.reload_failed"]
    assert failed and failed[0].fields["version"] == "broken-33333333"
    assert failed[0].fields["serving"] == A


def test_background_watcher_picks_up_the_new_pointer(setup):
    data, loader = setup
    with make(data, loader, reload_s=0.05) as api:
        point(data, B)
        deadline = time.monotonic() + 5
        while api.get("/api/health").json()["index_version"] != B:
            assert time.monotonic() < deadline, "reload did not happen"
            time.sleep(0.05)
    assert loader.loaded == [A, B]


def test_default_loader_reloads_a_real_index(tmp_path, monkeypatch):
    """The live path: real ChromaDB versions, the query encoder reused (no second model load)."""
    from refresh_support import INSTRUCTION, make_workspace

    from conftest import FakeEmbedder
    from mf_faq.ingest.index import build_index, current_version

    corpus, data = make_workspace(tmp_path)
    old = current_version(data)
    made: list[FakeEmbedder] = []

    def make_embedder(name: str, revision: str | None = None) -> FakeEmbedder:
        made.append(FakeEmbedder(name, revision=revision))
        return made[-1]

    monkeypatch.setattr("mf_faq.ingest.index.make_embedder", make_embedder)
    settings = Settings(
        _env_file=None,
        groq_api_key="gsk_test",  # the Groq client is built, never called
        corpus_dir=corpus,
        data_dir=data,
        embed_model="fake/model",
        embed_model_revision="r1",
        index_reload_s=0,
    )
    with TestClient(create_app(settings, web_dir=None)) as api:
        svc = api.app.state.service
        assert api.get("/api/health").json()["index_version"] == old

        lines = (data / "chunks.jsonl").read_text().splitlines()
        row = json.loads(lines[0])
        row["text"] += " (changed)"
        (data / "chunks.jsonl").write_text("\n".join([json.dumps(row), *lines[1:]]) + "\n")
        new = build_index(data, FakeEmbedder(), query_instruction=INSTRUCTION).version
        assert new != old and svc.reload_if_changed()
        assert api.get("/api/health").json()["index_version"] == new
        assert svc.pipeline.retriever.index.version == new
        assert len(made) == 1  # one encoder, shared by both pipelines
