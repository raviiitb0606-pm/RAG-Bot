"""Shared helpers for the Phase 7 tests: a built corpus + live index, and a mock Groww.

`make_workspace()` lays out `corpus/` (the saved pages, downloaded on DAY1) and builds `data/`
with the `FakeEmbedder`, as a previous refresh run would have left them. `Groww` serves the
saved pages over `httpx.MockTransport`, with per-page overrides.
"""

import shutil
from pathlib import Path

import httpx

from conftest import ROOT, SOURCES, FakeEmbedder, page_html
from mf_faq.ingest.build import build_corpus
from mf_faq.ingest.fetch import write_meta
from mf_faq.ingest.index import build_index

INSTRUCTION = "Represent this sentence for searching relevant passages: "
DAY1 = "2026-10-25T06:36:23+00:00"
MID = "groww-hdfc-mid-cap"
ROBOTS_OK = "User-agent: *\nDisallow: /user/\n"
PATHS = {s.id: "/" + s.url.split("/", 3)[3] for s in SOURCES}


def make_workspace(base: Path) -> tuple[Path, Path]:
    corpus, data = base / "corpus", base / "data"
    (corpus / "raw").mkdir(parents=True)
    for name in ("sources.yaml", "schemes.yaml"):
        shutil.copy(ROOT / "corpus" / name, corpus / name)
    for s in SOURCES:
        (corpus / "raw" / f"{s.id}.html").write_text(page_html(s.id), encoding="utf-8")
        write_meta(corpus / "raw", s.id, {"status": "ok", "downloaded_at": DAY1})
    embedder = FakeEmbedder()
    assert build_corpus(corpus, data, tokenizer=embedder).ok
    assert build_index(data, embedder, query_instruction=INSTRUCTION).swapped
    return corpus, data


class Groww:
    """robots.txt + the 5 saved pages; `pages[id]` replaces a page, `fail[id]` an HTTP status."""

    def __init__(self) -> None:
        self.pages: dict[str, str] = {}
        self.fail: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OK)
        source_id = next(i for i, p in PATHS.items() if p == request.url.path)
        if source_id in self.fail:
            return httpx.Response(self.fail[source_id])
        return httpx.Response(200, text=self.pages.get(source_id) or page_html(source_id))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)
