"""Shared test helpers: saved copies of the 5 Groww pages (fetched 25 Oct 2026)."""

import gzip
from functools import cache
from pathlib import Path

import pytest

from mf_faq.ingest.registry import load_schemes, load_sources

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "groww"
SOURCES = load_sources(ROOT / "corpus" / "sources.yaml")
CATALOG = load_schemes(ROOT / "corpus" / "schemes.yaml", SOURCES)


@cache
def page_html(source_id: str) -> str:
    return gzip.decompress((FIXTURES / f"{source_id}.html.gz").read_bytes()).decode("utf-8")


def source(source_id: str):
    return next(s for s in SOURCES if s.id == source_id)


@pytest.fixture(params=[s.id for s in SOURCES])
def source_id(request) -> str:
    return request.param


class FakeEmbedder:
    """Deterministic stand-in for the sentence-transformers model (no download, no torch)."""

    def __init__(self, model_name: str = "fake/model", *, dimension: int = 8, revision="r1"):
        self.model_name = model_name
        self.revision = revision
        self.dimension = dimension
        self.max_seq_length = 512
        self.calls = 0

    def encode(self, texts: list[str]):
        import hashlib

        import numpy as np

        self.calls += 1
        rows = []
        for text in texts:
            seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
            rows.append(np.random.default_rng(seed).normal(size=self.dimension))
        vectors = np.array(rows, dtype=np.float32)
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    def count_tokens(self, text: str) -> int:
        return len(text.split())
