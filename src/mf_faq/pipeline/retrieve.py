"""Hybrid dense + BM25 retrieval with RRF, and the not-found layers A–D (P3.3, P3.4, arch §6.5).

**Retrieval runs only when exactly one scheme is resolved**, and always inside that scheme's
chunks: unfiltered search loses 13–26 points of hit@1, and other fund houses' questions score
like real ones (research §3.5).

`Retriever.search(text, scheme_id)`:
1. Query = text + " (<short name>)" when the name isn't already in it ("And its exit load?").
2. Dense: the query, with the bge instruction, embedded here and sent to the version's ChromaDB
   collection as a cosine query with `where={"scheme_id": …}`; top 10. (Checked against exact
   search: same order on 500 random queries, research note in P3.1.)
3. BM25 over `heading_path + covers line + text` (no header), rebuilt in memory at load (~1 ms,
   nothing unpickled). Scheme words and stopwords are dropped and words lightly stemmed: the
   scheme name repeats in every chunk after the filter, and left in it made BM25 favour the
   shortest chunks (research §3.3). Top 10 with a score above 0.
4. Reciprocal Rank Fusion (k = 60) → top 4.

`Retriever.evidence(text, resolution)` adds the not-found layers, all before the LLM; each
returns `insufficient` with a named reason (for logs and the eval):
- **A** `out_of_scope`: another fund house or a `not_in_scope` HDFC scheme is named (P3.2).
- **B** `no_scheme`: no scheme and no follow-up carry-over (ELSS, KYC, "what is a mutual fund").
- **C** `unsupported_topic`: the lexicon in `pipeline/scope.py` (lock-in, launch date, Sharpe…).
- **D** `low_similarity`: the best cosine is under τ. A safety floor, not the main gate.
A scheme-specific question with no known scheme is `needs_scheme` (D-09 clarify), and several
schemes are `multi_scheme` (one citation per answer, R-2); neither retrieves. Near-misses
("last year's expense ratio") get through and are left to the LLM's `insufficient` status.
"""

import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import numpy as np
from rank_bm25 import BM25Okapi

from mf_faq.config import Settings
from mf_faq.ingest.chunk import Chunk
from mf_faq.ingest.index import IndexLoadError, LoadedIndex
from mf_faq.ingest.registry import SchemeCatalog
from mf_faq.logs import log_event
from mf_faq.pipeline.schemes import Reason, SchemeResolution
from mf_faq.pipeline.scope import unsupported_topic

logger = logging.getLogger(__name__)

RRF_K = 60
CANDIDATES = 10  # per ranker, before fusion

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)*")


def _wordset(text: str) -> frozenset[str]:
    return frozenset(text.split())


STOPWORDS = _wordset(
    "a an the of in on for to is are was do does did i my me what which who how much many "
    "and or it its this that be with by from at as can will any there"
)
# Every chunk of the filtered scheme carries these, so they only add length bias (research §3.3).
SCHEME_WORDS = _wordset(
    "hdfc large mid midcap small multi cap silver etf fof fund direct growth scheme"
)
_SUFFIXES = ("ing", "ers", "er", "es", "ed", "s")


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def bm25_tokens(text: str) -> list[str]:
    """The one tokeniser for BM25 documents and queries."""
    return [
        _stem(w)
        for w in _TOKEN_RE.findall(text.lower())
        if w not in STOPWORDS and w not in SCHEME_WORDS
    ]


def bm25_document(chunk: Chunk) -> str:
    """What BM25 indexes: no context header (the scheme name is noise after the filter)."""
    return f"{chunk.heading_path}\n{chunk.covers_line}\n{chunk.text}"


class QueryEncoder(Protocol):
    model_name: str
    revision: str | None
    dimension: int

    def encode(self, texts: list[str]) -> np.ndarray: ...


class Status(StrEnum):
    OK = "ok"  # chunks retrieved: go on to generation
    INSUFFICIENT = "insufficient"  # not-found reply, no LLM call; `reason` names the layer
    NEEDS_SCHEME = "needs_scheme"  # ask which scheme (D-09)
    MULTI_SCHEME = "multi_scheme"  # "one scheme at a time" reply (R-2)


class NotFound(StrEnum):
    OUT_OF_SCOPE = "out_of_scope"  # layer A
    NO_SCHEME = "no_scheme"  # layer B
    UNSUPPORTED_TOPIC = "unsupported_topic"  # layer C
    LOW_SIMILARITY = "low_similarity"  # layer D


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    rrf: float
    cosine: float | None  # None when the chunk came from BM25 only (not in Chroma's top 10)
    bm25: float
    dense_rank: int | None  # 0-based, among the scheme's chunks; None = not in the top 10
    bm25_rank: int | None


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[Hit, ...]  # fused, best first, at most top_k
    top_cosine: float  # best dense cosine in the scheme (layer D, P3.6)
    top_bm25: float
    latency_ms: float

    @property
    def chunk_ids(self) -> tuple[str, ...]:
        return tuple(h.chunk.chunk_id for h in self.hits)


@dataclass(frozen=True)
class Evidence:
    status: Status
    scheme_ids: tuple[str, ...] = ()
    reason: NotFound | None = None  # set when status is INSUFFICIENT
    detail: tuple[str, ...] = ()  # what decided: names matched, the topic, the cosine
    search: SearchResult | None = None  # set when retrieval ran (OK or LOW_SIMILARITY)

    @property
    def hits(self) -> tuple[Hit, ...]:
        return self.search.hits if self.search and self.status is Status.OK else ()

    @property
    def scheme_id(self) -> str | None:
        return self.scheme_ids[0] if len(self.scheme_ids) == 1 else None


class Retriever:
    """Search over one loaded index. Read-only after construction, so it is safe to share."""

    def __init__(
        self,
        index: LoadedIndex,
        encoder: QueryEncoder,
        catalog: SchemeCatalog,
        *,
        query_instruction: str,
        top_k: int = 4,
        threshold: float = 0.69,
    ) -> None:
        meta = index.meta
        expected = {
            "embed_model": encoder.model_name,
            "embed_model_revision": encoder.revision,
            "dim": encoder.dimension,
            "query_instruction": query_instruction,
        }
        for key, value in expected.items():
            if meta.get(key) != value:
                raise IndexLoadError(
                    f"index {index.version} has {key}={meta.get(key)!r}, but the query encoder "
                    f"has {value!r}; rebuild it with `mf-faq index`"
                )
        self.index = index
        self.encoder = encoder
        self._instruction = query_instruction
        self.top_k = top_k
        self.threshold = threshold
        self._short_names = {s.scheme_id: s.short_name for s in catalog.schemes}
        self._bm25 = BM25Okapi([bm25_tokens(bm25_document(c)) for c in index.chunks])
        self._rows = {sid: np.flatnonzero(index.scheme_ids == sid) for sid in self._short_names}
        self._row_of = {c.chunk_id: i for i, c in enumerate(index.chunks)}

    def query_text(self, text: str, scheme_id: str) -> str:
        name = self._short_names[scheme_id]
        return text if name.lower() in text.lower() else f"{text} ({name})"

    def search(self, text: str, scheme_id: str) -> SearchResult:
        """Hybrid search inside one scheme's chunks. Never looks at another scheme's rows."""
        rows = self._rows.get(scheme_id)
        if rows is None or not rows.size:
            raise ValueError(f"no chunks for scheme {scheme_id!r}")
        started = time.perf_counter()
        query = self.query_text(text, scheme_id)

        q = self.encoder.encode([self._instruction + query])[0]
        dense_order, cosine = self._dense(q, scheme_id, min(CANDIDATES, rows.size))
        sparse_all = self._bm25.get_scores(bm25_tokens(query))
        sparse_order = [
            int(r) for r in rows[np.argsort(-sparse_all[rows], kind="stable")[:CANDIDATES]]
        ]
        sparse_order = [r for r in sparse_order if sparse_all[r] > 0]

        fused: dict[int, float] = {}
        for ranking in (dense_order, sparse_order):
            for rank, r in enumerate(ranking):
                fused[r] = fused.get(r, 0.0) + 1.0 / (RRF_K + rank + 1)
        dense_rank = {r: i for i, r in enumerate(dense_order)}
        sparse_rank = {r: i for i, r in enumerate(sparse_order)}
        best = sorted(fused, key=lambda r: -fused[r])[: self.top_k]  # stable: dense breaks ties

        hits = tuple(
            Hit(
                chunk=self.index.chunks[r],
                rrf=fused[r],
                cosine=cosine.get(r),
                bm25=float(sparse_all[r]),
                dense_rank=dense_rank.get(r),
                bm25_rank=sparse_rank.get(r),
            )
            for r in best
        )
        result = SearchResult(
            hits=hits,
            top_cosine=cosine[dense_order[0]] if dense_order else 0.0,
            top_bm25=float(sparse_all[rows].max()),
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        log_event(
            logger,
            "retrieve.searched",
            level=logging.DEBUG,
            scheme_id=scheme_id,
            chunk_ids=list(result.chunk_ids),
            top_cosine=round(result.top_cosine, 4),
            top_bm25=round(result.top_bm25, 3),
            latency_ms=round(result.latency_ms, 1),
        )
        return result

    def _dense(self, q: np.ndarray, scheme_id: str, n: int) -> tuple[list[int], dict[int, float]]:
        """Chroma query inside one scheme: (rows best first, cosine by row)."""
        result = self.index.collection.query(
            query_embeddings=[q],
            n_results=n,
            where={"scheme_id": scheme_id},
            include=["distances"],
        )
        order: list[int] = []
        cosine: dict[int, float] = {}
        for chunk_id, distance in zip(result["ids"][0], result["distances"][0], strict=True):
            row = self._row_of[chunk_id]
            if self.index.chunks[row].scheme_id != scheme_id:  # the filter is the contract
                continue
            order.append(row)
            cosine[row] = 1.0 - float(distance)  # cosine space: distance = 1 - cosine
        return order, cosine

    def evidence(self, text: str, resolution: SchemeResolution) -> Evidence:
        """The not-found layers A–D, then retrieval (see the module docstring)."""
        ids = resolution.scheme_ids
        if resolution.out_of_scope:  # A
            return Evidence(Status.INSUFFICIENT, (), NotFound.OUT_OF_SCOPE, resolution.matched)
        if resolution.reason is Reason.NONE:  # B
            return Evidence(Status.INSUFFICIENT, (), NotFound.NO_SCHEME, resolution.matched)
        if topic := unsupported_topic(text):  # C
            return Evidence(Status.INSUFFICIENT, ids, NotFound.UNSUPPORTED_TOPIC, (topic,))
        if resolution.reason is Reason.NEEDS_SCHEME or not ids:
            return Evidence(Status.NEEDS_SCHEME)
        if len(ids) > 1:
            return Evidence(Status.MULTI_SCHEME, ids)

        result = self.search(text, ids[0])
        if result.top_cosine < self.threshold:  # D
            return Evidence(
                Status.INSUFFICIENT,
                ids,
                NotFound.LOW_SIMILARITY,
                (f"{result.top_cosine:.3f}",),
                result,
            )
        return Evidence(Status.OK, ids, search=result)


def load_retriever(
    settings: Settings,
    catalog: SchemeCatalog,
    encoder: QueryEncoder | None = None,
    *,
    version: str | None = None,
) -> Retriever:
    """The live index (or `version`) plus the query encoder; refuses any model/format mismatch."""
    from mf_faq.ingest.index import embedder_from, load_index

    index = load_index(
        settings.data_dir,
        model_name=settings.embed_model,
        query_instruction=settings.embed_query_instruction,
        version=version,
    )
    return Retriever(
        index,
        encoder or embedder_from(settings),
        catalog,
        query_instruction=settings.embed_query_instruction,
        top_k=settings.retrieval_top_k,
        threshold=settings.retrieval_threshold,
    )
