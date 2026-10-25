"""P3.1: index build -> load round trip, refusal on mismatch, atomic swap and rollback."""

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from conftest import ROOT, SOURCES, FakeEmbedder, page_html
from mf_faq.cli import app
from mf_faq.config import get_settings
from mf_faq.ingest.build import build_corpus, load_chunks
from mf_faq.ingest.fetch import write_meta
from mf_faq.ingest.index import (
    CHROMA_DIR,
    CURRENT,
    META_FILE,
    IndexBuildError,
    IndexLoadError,
    build_index,
    current_version,
    index_root,
    load_index,
    previous_version,
    rollback_index,
)

INSTRUCTION = "Represent this sentence for searching relevant passages: "
DAY1 = "2026-10-25T06:36:23+00:00"


@pytest.fixture(scope="module")
def built_data(tmp_path_factory) -> Path:
    """data/ as the Phase 2 build writes it from the saved pages (built once per module)."""
    base = tmp_path_factory.mktemp("built")
    corpus = base / "corpus"
    (corpus / "raw").mkdir(parents=True)
    for name in ("sources.yaml", "schemes.yaml"):
        shutil.copy(ROOT / "corpus" / name, corpus / name)
    for s in SOURCES:
        (corpus / "raw" / f"{s.id}.html").write_text(page_html(s.id), encoding="utf-8")
        write_meta(corpus / "raw", s.id, {"status": "ok", "downloaded_at": DAY1})
    assert build_corpus(corpus, base / "data").ok
    return base / "data"


@pytest.fixture
def data(built_data, tmp_path) -> Path:
    return Path(shutil.copytree(built_data, tmp_path / "data"))


def build(data: Path, embedder=None, **kwargs):
    return build_index(data, embedder or FakeEmbedder(), query_instruction=INSTRUCTION, **kwargs)


def load(data: Path, model: str = "fake/model", **kwargs):
    return load_index(data, model_name=model, **kwargs)


def test_build_load_round_trip(data):
    result = build(data)
    assert result.ok and result.swapped
    assert current_version(data) == result.version
    assert result.version.startswith(
        json.loads((data / "manifest.json").read_text())["corpus_version"]
    )

    index = load(data, query_instruction=INSTRUCTION)
    chunks = load_chunks(data)
    assert [c.chunk_id for c in index.chunks] == [c.chunk_id for c in chunks]
    assert index.vectors.shape == (len(chunks), 8) and index.vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(index.vectors, axis=1), 1.0, atol=1e-5)
    # Rows follow chunks.jsonl order: row i is the embedding of chunk i's content.
    assert np.allclose(index.vectors[3], FakeEmbedder().encode([chunks[3].content])[0])
    assert list(index.scheme_ids) == [c.scheme_id for c in chunks]
    assert not index.vectors.flags.writeable

    meta = index.meta
    assert meta["embed_model"] == "fake/model" and meta["embed_model_revision"] == "r1"
    assert meta["chunk_count"] == len(chunks) and meta["dim"] == 8
    assert meta["chunk_format_version"] == "2" and meta["query_instruction"] == INSTRUCTION
    assert (meta["vector_store"], meta["collection"], meta["hnsw_space"]) == (
        "chromadb",
        "chunks",
        "cosine",
    )
    assert sorted(p.name for p in index.path.iterdir()) == [
        CHROMA_DIR,
        "chunks.jsonl",
        META_FILE,
        "manifest.json",
    ]
    # The Chroma collection holds one record per chunk: id, our vector, metadata, document.
    record = index.collection.get(ids=[chunks[3].chunk_id], include=["metadatas", "documents"])
    assert record["metadatas"][0] == {
        "row": 3,
        "scheme_id": chunks[3].scheme_id,
        "source_id": chunks[3].source_id,
    }
    assert record["documents"][0] == chunks[3].content
    assert index.collection.count() == len(chunks)
    # No pickles anywhere: Chroma writes SQLite + HNSW .bin files, BM25 isn't stored at all.
    assert not [p for p in index_root(data).rglob("*") if p.suffix in {".pkl", ".pickle"}]
    index.close()


def test_same_inputs_rebuild_is_a_no_op(data):
    first = build(data)
    embedder = FakeEmbedder()
    again = build(data, embedder)
    assert again.ok and not again.swapped and again.version == first.version
    assert embedder.calls == 0
    assert previous_version(data) is None


def test_model_or_format_mismatch_refuses_to_load(data):
    build(data)
    with pytest.raises(IndexLoadError, match="embed_model"):
        load(data, model="BAAI/bge-base-en-v1.5")
    with pytest.raises(IndexLoadError, match="chunk_format_version"):
        load(data, chunk_format_version="1")
    with pytest.raises(IndexLoadError, match="query_instruction"):
        load(data, query_instruction="query: ")


def test_no_index_yet_fails_clearly(data):
    with pytest.raises(IndexLoadError, match="run `mf-faq ingest` first"):
        load(data)


def test_tampered_files_refuse_to_load(data):
    from mf_faq.ingest.index import open_client

    version = build(data).version
    client = open_client(index_root(data) / version / CHROMA_DIR)
    collection = client.get_collection("chunks", embedding_function=None)
    first = collection.get(limit=1, include=["embeddings"])
    collection.update(ids=first["ids"], embeddings=[-np.asarray(first["embeddings"][0])])
    client.close()
    with pytest.raises(IndexLoadError, match="Chroma vectors do not match"):
        load(data)


def test_missing_chroma_store_refuses_to_load(data):
    version = build(data).version
    shutil.rmtree(index_root(data) / version / CHROMA_DIR)
    with pytest.raises(IndexLoadError, match="no chroma/ folder"):
        load(data)


def test_rollback_refuses_a_version_without_chroma(data):
    old = build(data).version
    build(data, FakeEmbedder("fake/model-v2"))
    shutil.rmtree(index_root(data) / old / CHROMA_DIR)  # e.g. a pre-Chroma NumPy version
    with pytest.raises(IndexLoadError, match="no ChromaDB store"):
        rollback_index(data)


def test_failed_gate_leaves_current_unchanged(data):
    live = build(data).version
    before = sorted(p.name for p in index_root(data).iterdir())

    result = build(data, FakeEmbedder("fake/other"), gates=[lambda index: ["eval: hit@4 0.50"]])
    assert result.failures == ["eval: hit@4 0.50"] and not result.swapped
    assert current_version(data) == live
    assert sorted(p.name for p in index_root(data).iterdir()) == before  # no leftovers
    assert load(data).version == live


def test_gates_see_the_candidate_index(data):
    seen = []
    result = build(data, gates=[lambda index: seen.append(index) or []])
    assert result.swapped
    assert seen[0].version.startswith(".build-")  # judged before it is live
    assert len(seen[0].chunks) == seen[0].meta["chunk_count"]


@pytest.mark.parametrize(
    "vectors, message",
    [
        (lambda n, d: np.ones((n, d), np.float32), "not unit length"),
        (lambda n, d: np.full((n, d), np.nan, np.float32), "NaN"),
        (lambda n, d: np.ones((n - 1, d), np.float32), "shape"),
    ],
)
def test_bad_embeddings_fail_the_build(data, vectors, message):
    class Broken(FakeEmbedder):
        def encode(self, texts):
            return vectors(len(texts), self.dimension)

    live = build(data).version
    result = build(data, Broken("fake/broken"))
    assert not result.ok and message in result.failures[0]
    assert current_version(data) == live


def test_new_version_keeps_previous_and_rolls_back(data):
    old = build(data).version
    new = build(data, FakeEmbedder("fake/model-v2")).version
    assert new != old
    assert current_version(data) == new and previous_version(data) == old
    assert load(data, model="fake/model-v2").version == new
    assert load(data, version=old).version == old  # the previous version stays loadable

    assert rollback_index(data) == old
    assert current_version(data) == old and previous_version(data) == new


def test_only_current_and_previous_are_kept(data):
    versions = [build(data, FakeEmbedder(f"fake/m{i}")).version for i in range(3)]
    kept = sorted(p.name for p in index_root(data).iterdir() if p.is_dir())
    assert kept == sorted(versions[1:])


def test_chunk_change_gives_a_new_version(data):
    old = build(data).version
    lines = (data / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["text"] += " (edited)"
    lines[0] = json.dumps(first, ensure_ascii=False)
    (data / "chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert build(data).version != old


def test_stale_or_missing_inputs_stop_the_build(data):
    manifest = json.loads((data / "manifest.json").read_text())
    (data / "manifest.json").write_text(json.dumps({**manifest, "chunk_format_version": "0"}))
    with pytest.raises(IndexBuildError, match="re-run `mf-faq ingest"):
        build(data)
    (data / "chunks.jsonl").unlink()
    with pytest.raises(IndexBuildError, match="no usable chunks"):
        build(data)
    assert not (index_root(data) / CURRENT).exists()


def test_chunks_over_the_model_limit_fail_the_build(data):
    """P3.0 gate: a chunk the model would truncate never goes live (split it in ingest)."""
    live = build(data).version
    embedder = FakeEmbedder("fake/short")
    longest = max(embedder.count_tokens(c.content) for c in load_chunks(data))
    embedder.max_seq_length = longest - 1
    result = build(data, embedder)
    over = [c.chunk_id for c in load_chunks(data) if embedder.count_tokens(c.content) == longest]
    assert over and not result.ok and not result.swapped
    assert "would be truncated" in result.failures[0] and over[0] in result.failures[0]
    assert current_version(data) == live


def test_ingest_split_fits_the_model(data):
    meta = build(data).meta
    assert meta["max_chunk_tokens"] <= meta["max_seq_length"]
    assert "truncated_chunks" not in meta


def test_cli_index_and_rollback(data, monkeypatch):
    monkeypatch.setenv("MF_FAQ_DATA_DIR", str(data))
    monkeypatch.setattr("mf_faq.ingest.index.make_embedder", FakeEmbedder)
    gates = []
    # Random fake vectors can't pass the retrieval eval; check the CLI wires it as a gate.
    monkeypatch.setattr("mf_faq.cli._index_gates", lambda e: gates.append(e) or [])
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        assert runner.invoke(app, ["index", "--rollback"]).exit_code == 1  # nothing to roll back
        result = runner.invoke(app, ["index"])
        assert result.exit_code == 0 and "(now live)" in result.output
        assert "already live" in runner.invoke(app, ["index"]).output
        assert len(gates) == 2 and isinstance(gates[0], FakeEmbedder)
    finally:
        get_settings.cache_clear()


def _model_cached(name: str) -> bool:
    from huggingface_hub import try_to_load_from_cache

    return isinstance(try_to_load_from_cache(name, "config.json"), str)


@pytest.mark.skipif(
    not _model_cached("BAAI/bge-small-en-v1.5"), reason="embedding model not downloaded"
)
def test_real_model_index(data):
    from mf_faq.ingest.index import SentenceTransformerEmbedder

    embedder = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")
    result = build(data, embedder)
    assert result.ok
    index = load(data, model="BAAI/bge-small-en-v1.5")
    assert index.meta["dim"] == 384 and index.meta["max_seq_length"] == 512
    assert len(index.meta["embed_model_revision"]) == 40

    # Sanity: within the scheme mask, the exit-load question lands on the exit-load chunk.
    q = embedder.encode([INSTRUCTION + "What is the exit load? (HDFC Mid Cap Fund)"])[0]
    rows = np.flatnonzero(index.scheme_ids == "hdfc-mid-cap")
    best = rows[np.argmax(index.vectors[rows] @ q)]
    assert index.chunks[best].chunk_id == "groww-hdfc-mid-cap:exit_load"
