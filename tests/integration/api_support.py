"""Shared helpers for the API tests: the real pipeline with a stub retriever and generator.

`SchemeRetriever` returns one exit-load/expense-ratio chunk for whichever scheme was resolved,
so a reply's link shows which scheme a turn ended up on. `ScriptedGenerator` answers from the
first chunk in the prompt and records every prompt (thread-safe), so tests can check what the
model saw.
"""

import re
import threading
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import CATALOG, SOURCES
from mf_faq.api.app import WEB_DIR, create_app
from mf_faq.config import Settings
from mf_faq.ingest.chunk import Chunk
from mf_faq.ingest.parse_html import IST
from mf_faq.pipeline.answer import AnswerPipeline
from mf_faq.pipeline.compose import Composer
from mf_faq.pipeline.draft import AnswerDraft
from mf_faq.pipeline.generate import Generation
from mf_faq.pipeline.retrieve import Evidence, Hit, NotFound, SearchResult, Status
from mf_faq.pipeline.schemes import Reason, SchemeResolver
from mf_faq.pipeline.scope import unsupported_topic
from mf_faq.pipeline.sources import SourceEntry, SourceRegistry
from mf_faq.threads.store import ThreadStore

URL_BY_SCHEME = {s.scheme_id: s.url for s in SOURCES}
ANSWER = "The exit load is 1% if redeemed within 1 year, and the expense ratio is 0.77%."
_CHUNK_ID_RE = re.compile(r'<chunk id="([^"]+)"')


def chunk_for(scheme_id: str) -> Chunk:
    source = next(s for s in SOURCES if s.scheme_id == scheme_id)
    return Chunk(
        chunk_id=f"{source.id}:exit_load",
        source_id=source.id,
        scheme_id=scheme_id,
        doc_type="scheme_page",
        publisher="GROWW",
        heading_path="Exit load",
        facts=("exit_load", "expense_ratio"),
        header=f"[Scheme: {scheme_id} | Source: Groww scheme page | Section: Exit load]",
        covers="exit load",
        text="Exit load of 1% if redeemed within 1 year. Expense ratio: 0.77%.",
    )


class SchemeRetriever:
    """The A/B/clarify layers of `Retriever.evidence`; retrieval finds the scheme's chunk."""

    def evidence(self, text, resolution):
        if resolution.out_of_scope:
            return Evidence(Status.INSUFFICIENT, (), NotFound.OUT_OF_SCOPE)
        if resolution.reason is Reason.NONE:
            return Evidence(Status.INSUFFICIENT, (), NotFound.NO_SCHEME)
        if topic := unsupported_topic(text):
            return Evidence(
                Status.INSUFFICIENT, resolution.scheme_ids, NotFound.UNSUPPORTED_TOPIC, (topic,)
            )
        if resolution.reason is Reason.NEEDS_SCHEME:
            return Evidence(Status.NEEDS_SCHEME)
        ids = resolution.scheme_ids
        if len(ids) > 1:
            return Evidence(Status.MULTI_SCHEME, ids)
        hit = Hit(chunk_for(ids[0]), 0.03, 0.8, 5.0, 0, 0)
        return Evidence(Status.OK, ids, search=SearchResult((hit,), 0.8, 5.0, 1.0))


class ScriptedGenerator:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self._lock = threading.Lock()

    def generate(self, messages):
        with self._lock:
            self.calls.append(messages)
        cited = _CHUNK_ID_RE.search(messages[-1]["content"]).group(1)
        return Generation(AnswerDraft(status="answered", answer=ANSWER, cited_chunk_id=cited))


def make_pipeline(generator=None, registry: SourceRegistry | None = None) -> AnswerPipeline:
    when = datetime(2026, 10, 25, tzinfo=IST)
    registry = registry or SourceRegistry(
        [SourceEntry(s.id, s.url, s.title, s.scheme_id, when) for s in SOURCES]
    )
    return AnswerPipeline(
        retriever=SchemeRetriever(),
        resolver=SchemeResolver(CATALOG),
        generator=generator or ScriptedGenerator(),
        composer=Composer(registry, CATALOG, "groww-hdfc-large-cap"),
        catalog=CATALOG,
    )


def make_app(tmp_path: Path, generator=None, *, web_dir=WEB_DIR, **overrides):
    settings = Settings(
        _env_file=None, data_dir=tmp_path, **{"api_rate_limit": "1000/minute", **overrides}
    )
    return create_app(
        settings,
        pipeline=make_pipeline(generator),
        store=ThreadStore(settings.db_path),
        index_version="test-index",
        corpus_version="2026-10-25.test",
        web_dir=web_dir,
    )


def client(tmp_path: Path, generator=None, **overrides) -> TestClient:
    return TestClient(make_app(tmp_path, generator, **overrides))
