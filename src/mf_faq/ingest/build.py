"""Ingest build: raw pages -> sections -> strip -> chunks -> gates -> outputs (P2.2, P2.6).

Always rebuilds all 5 pages from `corpus/raw/` (parsing is cheap), so the gates see the whole
corpus. Every gate runs **before** anything is written: a failed build leaves the previous
outputs untouched.

Outputs:
- `data/chunks.jsonl`: one chunk per line; `ingest/index.py` embeds it (P3.1).
- `data/manifest.json`: corpus version, the 5 sources with dates, hashes and chunk counts.
- `data/app.db`, table `sources` (arch §8.1): the registry snapshot the composer reads.
- `corpus/raw/<id>.meta.json`: `fetched_at` and `content_sha256` added.

Footer-date rule (arch §5.6, FET-08): `fetched_at` moves to the copy's `downloaded_at` only
when the hash of the extracted facts changes; otherwise the previous `fetched_at` is kept.
"""

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mf_faq.ingest.chunk import (
    CHUNK_FORMAT_VERSION,
    MAX_TOKENS,
    ApproxTokenizer,
    Chunk,
    ChunkError,
    Tokenizer,
    chunk_page,
)
from mf_faq.ingest.fetch import read_meta, write_meta
from mf_faq.ingest.gates import check_gates
from mf_faq.ingest.parse_html import (
    IST,
    PARSER_VERSION,
    ParsedPage,
    ParseError,
    facts_sha256,
    parse_page,
)
from mf_faq.ingest.registry import SchemeCatalog, Source, load_schemes, load_sources
from mf_faq.ingest.strip_performance import strip_performance_sections
from mf_faq.logs import log_event

logger = logging.getLogger(__name__)

CHUNKS_FILE = "chunks.jsonl"
MANIFEST_FILE = "manifest.json"
DB_FILE = "app.db"

SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, url TEXT, title TEXT, publisher TEXT, doc_type TEXT,
  scheme_id TEXT, fetched_at TEXT, content_sha256 TEXT, status TEXT
)
"""


@dataclass
class PageBuild:
    source: Source
    meta: dict
    page: ParsedPage | None = None
    chunks: list[Chunk] = field(default_factory=list)
    content_sha256: str | None = None


@dataclass
class BuildResult:
    failures: list[str]
    pages: list[PageBuild]
    manifest: dict | None = None

    @property
    def ok(self) -> bool:
        return not self.failures


def process_html(
    html: str,
    source: Source,
    catalog: SchemeCatalog,
    *,
    tokenizer: Tokenizer | None = None,
    max_tokens: int = MAX_TOKENS,
) -> tuple[ParsedPage, list[Chunk], str]:
    """Parse, strip and chunk one page. Returns the page, its chunks and its facts hash.

    Pass the embedding model as `tokenizer` (any `Embedder`) so long sections are split by the
    tokens the model actually sees (P3.0); without it the regex estimate is used.
    """
    page = parse_page(html, source)
    sections = strip_performance_sections(page.sections)
    chunks = chunk_page(
        sections,
        source,
        catalog.by_source(source.id),
        catalog.covers,
        tokenizer=tokenizer,
        max_tokens=max_tokens,
    )
    return page, chunks, facts_sha256(sections)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _corpus_version(pages: list[PageBuild]) -> str:
    """`<latest fetched_at date>.<hash of all facts hashes>`: same content, same version."""
    latest = max(datetime.fromisoformat(p.meta["fetched_at"]) for p in pages)
    digest = hashlib.sha256(
        "\n".join(f"{p.source.id}:{p.content_sha256}" for p in pages).encode()
    ).hexdigest()
    return f"{latest.astimezone(IST).date().isoformat()}.{digest[:8]}"


def write_sources_table(db_path: Path, pages: list[PageBuild]) -> None:
    rows = [
        (
            p.source.id,
            p.source.url,
            p.source.title,
            p.source.publisher,
            p.source.doc_type,
            p.source.scheme_id,
            p.meta["fetched_at"],
            p.content_sha256,
            p.meta.get("status"),
        )
        for p in pages
    ]
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(SOURCES_DDL)
        conn.execute("DELETE FROM sources")
        conn.executemany("INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def build_corpus(
    corpus_dir: Path,
    data_dir: Path,
    *,
    tokenizer: Tokenizer | None = None,
    max_tokens: int = MAX_TOKENS,
    now: datetime | None = None,
) -> BuildResult:
    tokenizer = tokenizer or ApproxTokenizer()
    sources = load_sources(corpus_dir / "sources.yaml")  # RegistryError stops the build
    catalog = load_schemes(corpus_dir / "schemes.yaml", sources)
    raw_dir = corpus_dir / "raw"

    failures: list[str] = []
    pages: list[PageBuild] = []
    for source in sources:
        build = PageBuild(source, read_meta(raw_dir, source.id))
        pages.append(build)
        html_path = raw_dir / f"{source.id}.html"
        if not html_path.exists():
            failures.append(f"{source.id}: no copy at {html_path} (fetch it, or save it by hand)")
            continue
        if not build.meta.get("downloaded_at"):  # FET-09: the footer needs a date
            failures.append(
                f"{source.id}: no recorded download or save date; run "
                f"`mf-faq ingest --manual-copy {source.id} --saved-on YYYY-MM-DD`"
            )
            continue
        try:
            build.page, build.chunks, build.content_sha256 = process_html(
                html_path.read_text(encoding="utf-8"),
                source,
                catalog,
                tokenizer=tokenizer,
                max_tokens=max_tokens,
            )
        except (ParseError, ChunkError) as e:  # PAR-01 layout change, FET-05 block page
            failures.append(f"{source.id}: {e}")

    failures += check_gates(
        sources, catalog, {p.source.id: p.chunks for p in pages}, tokenizer=tokenizer
    )
    if failures:
        for failure in failures:
            log_event(logger, "ingest.gate_failed", level=logging.ERROR, failure=failure)
        return BuildResult(failures, pages)

    for p in pages:
        unchanged = p.meta.get("content_sha256") == p.content_sha256 and p.meta.get("fetched_at")
        fetched_at = p.meta["fetched_at"] if unchanged else p.meta["downloaded_at"]
        p.meta = {**p.meta, "fetched_at": fetched_at, "content_sha256": p.content_sha256}

    all_chunks = [c for p in pages for c in p.chunks]
    manifest = {
        "corpus_version": _corpus_version(pages),
        "built_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "parser_version": PARSER_VERSION,
        "chunk_format_version": CHUNK_FORMAT_VERSION,
        "chunk_count": len(all_chunks),
        "tokenizer": tokenizer.model_name,  # what split the long sections (P3.0)
        "max_tokens": max_tokens,
        "max_chunk_tokens": max(tokenizer.count_tokens(c.content) for c in all_chunks),
        "sources": [
            {
                "id": p.source.id,
                "url": p.source.url,
                "scheme_id": p.source.scheme_id,
                "title": p.source.title,
                "status": p.meta.get("status"),
                "fetched_at": p.meta["fetched_at"],
                "downloaded_at": p.meta["downloaded_at"],
                "last_checked_at": p.meta.get("last_checked_at"),
                "http_status": p.meta.get("http_status"),
                "content_sha256": p.content_sha256,
                "raw_sha256": p.meta.get("raw_sha256"),
                "chunk_count": len(p.chunks),
                "has_performance_section": p.page.has_performance_section if p.page else False,
            }
            for p in pages
        ],
    }

    data_dir.mkdir(parents=True, exist_ok=True)
    _write_atomic(
        data_dir / CHUNKS_FILE,
        "".join(json.dumps(c.to_dict(), ensure_ascii=False) + "\n" for c in all_chunks),
    )
    _write_atomic(data_dir / MANIFEST_FILE, json.dumps(manifest, indent=2, ensure_ascii=False))
    write_sources_table(data_dir / DB_FILE, pages)
    for p in pages:
        write_meta(raw_dir, p.source.id, p.meta)

    log_event(
        logger,
        "ingest.built",
        corpus_version=manifest["corpus_version"],
        chunks=len(all_chunks),
        sources=len(pages),
    )
    return BuildResult([], pages, manifest)


def load_chunks(data_dir: Path) -> list[Chunk]:
    """Read `data/chunks.jsonl` back (Phase 3 indexing, tests)."""
    lines = (data_dir / CHUNKS_FILE).read_text(encoding="utf-8").splitlines()
    return [Chunk(**{**d, "facts": tuple(d["facts"])}) for d in map(json.loads, lines)]
