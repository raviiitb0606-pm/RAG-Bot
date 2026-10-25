"""One conversation turn, end to end (P5.7-P5.9, arch §6.1).

    PII guard → router → scheme resolution → retrieval (+ not-found layers) → generation
    → validator (one retry) → composer

`AnswerPipeline.answer_turn(ctx, text)` returns a `TurnResult`: the `Reply` to show, the thread's
next `active_scheme_id`, and the redacted user text (the only form that may be stored, P-1).
The generator is called only for a `factual` message that has passed the PII guard and the
not-found layers, so a message with PII, an advisory or performance question, or an unknown
scheme never reaches the model.

Failure policy: a model/provider failure → `error` template; an answer that fails validation
twice → `not_found` (never an unvalidated answer).
"""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from mf_faq.ingest.registry import SchemeCatalog
from mf_faq.logs import log_event
from mf_faq.pipeline.compose import Composer, Reply
from mf_faq.pipeline.generate import (
    Generation,
    GenerationError,
    Generator,
    Turn,
    Usage,
    build_messages,
)
from mf_faq.pipeline.pii import redact
from mf_faq.pipeline.retrieve import Evidence, Retriever
from mf_faq.pipeline.router import route
from mf_faq.pipeline.schemes import Reason, SchemeResolver
from mf_faq.pipeline.sources import SourceRegistry
from mf_faq.pipeline.templates import BUSY, Kind, kind_for_evidence, kind_for_intent
from mf_faq.pipeline.validate import Failure, validate
from mf_faq.redacted import RedactedText

logger = logging.getLogger(__name__)

# Limits spent (ours or Groq's): "try again in a minute" rather than "something went wrong".
BUSY_REASONS = frozenset({"rate_limited", "per_minute_budget_spent", "daily_budget_spent"})


@dataclass(frozen=True)
class ThreadContext:
    """What a turn needs from its thread: redacted history and the active scheme (arch §6.4).

    `thread_id` and `turn` only label the turn's log record (arch §13).
    """

    history: Sequence[Turn] = ()
    active_scheme_id: str | None = None
    thread_id: str | None = None
    turn: int | None = None


@dataclass(frozen=True)
class TurnResult:
    reply: Reply
    user_text: RedactedText
    active_scheme_id: str | None
    pii_types: tuple[str, ...] = ()
    trace: dict = field(default_factory=dict)  # ids, scores, codes: no text (for logs/eval)


class AnswerPipeline:
    def __init__(
        self,
        *,
        retriever: Retriever,
        resolver: SchemeResolver,
        generator: Generator,
        composer: Composer,
        catalog: SchemeCatalog,
        history_turns: int = 6,
        today: date | None = None,
    ) -> None:
        self._retriever = retriever
        self._resolver = resolver
        self._generator = generator
        self._composer = composer
        self._catalog = catalog
        self._history_turns = history_turns
        self._today = today
        self._urls = {e.id: e.url for e in composer.registry}

    # -- public ------------------------------------------------------------------------------

    @property
    def catalog(self) -> SchemeCatalog:
        return self._catalog

    @property
    def registry(self) -> SourceRegistry:
        return self._composer.registry

    @property
    def retriever(self) -> Retriever:
        return self._retriever

    @property
    def generator(self) -> Generator:
        return self._generator

    def close(self) -> None:
        """Release the index's Chroma client once no turn uses this pipeline (P7.6 reload)."""
        index = getattr(self._retriever, "index", None)
        if index is not None:
            index.close()

    def answer_turn(self, ctx: ThreadContext, text: str) -> TurnResult:
        started = time.perf_counter()
        redacted, pii_types = redact(text)
        try:
            result = self._run(ctx, redacted, pii_types)
        except Exception as e:  # a bug must not leak: answer with the error template (P5.6)
            log_event(logger, "turn.failed", level=logging.ERROR, exc_type=type(e).__name__)
            reply = self._composer.template(Kind.ERROR, active_scheme_id=ctx.active_scheme_id)
            result = TurnResult(
                reply, redacted, ctx.active_scheme_id, pii_types, {"outcome": "exception"}
            )
        labels = {"thread_id": ctx.thread_id, "turn": ctx.turn} if ctx.thread_id else {}
        log_event(
            logger,
            "turn.completed",
            **labels,
            kind=result.reply.kind.value,
            scheme_id=result.active_scheme_id,
            source_id=result.reply.source_id,
            pii_types=list(pii_types),
            latency_ms=round((time.perf_counter() - started) * 1000),
            **result.trace,
        )
        return result

    # -- stages ------------------------------------------------------------------------------

    def _run(self, ctx: ThreadContext, question: RedactedText, pii: tuple[str, ...]) -> TurnResult:
        active = ctx.active_scheme_id

        def done(reply: Reply, active_id: str | None, **trace) -> TurnResult:
            return TurnResult(reply, question, active_id, pii, trace)

        if pii:  # block entirely, even when a valid question comes with it (D-04)
            reply = self._composer.template(Kind.PII, active_scheme_id=active)
            return done(reply, active, outcome="pii")

        routed = route(question, today=self._today)
        resolution = self._resolver.resolve(question, active)
        followed = resolution.reason in (Reason.MATCHED, Reason.CARRY_OVER)
        next_active = resolution.scheme_id if followed and resolution.scheme_id else active
        link_ids = () if resolution.out_of_scope else resolution.scheme_ids
        base = {"intent": routed.intent.value, "rule": routed.rule}

        if (mapped := kind_for_intent(routed.intent)) is not None:
            kind, variant = mapped
            reply = self._composer.template(
                kind, variant=variant, scheme_ids=link_ids, active_scheme_id=active
            )
            return done(reply, next_active, outcome="router", **base)

        evidence = self._retriever.evidence(question, resolution)
        base |= {"evidence": evidence.status.value, "reason": evidence.reason}
        if (mapped := kind_for_evidence(evidence.status, evidence.reason)) is not None:
            kind, variant = mapped
            reply = self._composer.template(
                kind,
                variant=variant,
                scheme_ids=link_ids,
                active_scheme_id=active,
                best_source_id=self._best_source(evidence),
            )
            return done(reply, next_active, outcome="evidence", **base)

        reply, trace = self._generate(ctx, question, evidence)
        return done(reply, next_active, **base, **trace)

    @staticmethod
    def _best_source(evidence: Evidence) -> str | None:
        """After retrieval, a not-found reply links the best-ranked page (arch §7)."""
        if evidence.search and evidence.search.hits:
            return evidence.search.hits[0].chunk.source_id
        return None

    def _generate(self, ctx: ThreadContext, question: RedactedText, evidence: Evidence):
        chunks = [h.chunk for h in evidence.hits]
        active = evidence.scheme_id
        trace: dict = {"chunk_ids": [c.chunk_id for c in chunks], "attempts": 0}
        usage = Usage()
        failures: Sequence[Failure] = ()

        for attempt in (1, 2):  # one retry with the failed checks listed (P5.7)
            trace["attempts"] = attempt
            messages = build_messages(
                question, chunks, ctx.history, max_turns=self._history_turns, failures=failures
            )
            try:
                generation: Generation = self._generator.generate(messages)
            except GenerationError as e:
                trace["llm_error"] = e.reason
                if e.retryable and attempt == 1:
                    failures = (Failure("V7", "output", e.reason),)
                    continue
                kind = Kind.NOT_FOUND if e.retryable else Kind.ERROR
                variant = BUSY if e.reason in BUSY_REASONS else None
                reply = self._fallback(kind, active, chunks, ctx, variant)
                return reply, trace | {"outcome": "llm_error"}
            usage = Usage(
                usage.prompt_tokens + generation.usage.prompt_tokens,
                usage.completion_tokens + generation.usage.completion_tokens,
                usage.cached_tokens + generation.usage.cached_tokens,
            )
            trace |= {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "cached_tokens": usage.cached_tokens,
            }
            draft = generation.draft
            trace["status"] = draft.status

            # P5.8: the model can flag intent the rules missed.
            if draft.status == "advisory":
                return self._fallback(Kind.REFUSAL, active, chunks, ctx), trace | {
                    "outcome": "llm_advisory"
                }
            if draft.status == "performance":
                return self._fallback(Kind.PERFORMANCE, active, chunks, ctx), trace | {
                    "outcome": "llm_performance"
                }
            if draft.status == "insufficient":
                return self._fallback(Kind.NOT_FOUND, active, chunks, ctx), trace | {
                    "outcome": "llm_insufficient"
                }

            result = validate(draft, chunks, self._urls)
            if result.ok:
                cited = next(c for c in chunks if c.chunk_id == draft.cited_chunk_id)
                return self._composer.answer(draft.answer, cited), trace | {"outcome": "answered"}
            failures = result.failures
            trace["validation"] = list(result.codes)

        return self._fallback(Kind.NOT_FOUND, active, chunks, ctx), trace | {
            "outcome": "validation_failed"
        }

    def _fallback(
        self, kind: Kind, scheme_id: str | None, chunks, ctx: ThreadContext, variant=None
    ) -> Reply:
        return self._composer.template(
            kind,
            variant=variant,
            scheme_ids=(scheme_id,) if scheme_id else (),
            active_scheme_id=ctx.active_scheme_id,
            best_source_id=chunks[0].source_id if chunks else None,
        )


def build_pipeline(
    settings=None,
    *,
    generator: Generator | None = None,
    encoder=None,
    version: str | None = None,
) -> AnswerPipeline:
    """Wire everything from the settings and the live index (used by the CLI and the API).

    Raises `IndexLoadError` when there is no usable index and `RuntimeError` without a Groq key
    (fail fast at startup, P5.6). The index and the source registry come from the same version
    (`version`, or `current` read once), so a swap mid-load can't mix two versions. The API's
    reload (P7.6) passes the running `generator` and `encoder` to avoid reloading the model.
    """
    from mf_faq.config import get_settings
    from mf_faq.ingest.index import IndexLoadError, current_version
    from mf_faq.ingest.registry import load_schemes, load_sources
    from mf_faq.pipeline.generate import GroqGenerator
    from mf_faq.pipeline.retrieve import load_retriever
    from mf_faq.pipeline.sources import SourceRegistry

    settings = settings or get_settings()
    catalog = load_schemes(
        settings.corpus_dir / "schemes.yaml", load_sources(settings.corpus_dir / "sources.yaml")
    )
    generator = generator or GroqGenerator(settings)
    version = version or current_version(settings.data_dir)
    if version is None:
        raise IndexLoadError(f"no index at {settings.data_dir}/index; run `mf-faq ingest` first")
    retriever = load_retriever(settings, catalog, encoder, version=version)
    registry = SourceRegistry.from_manifest(settings.data_dir / "index" / version / "manifest.json")
    return AnswerPipeline(
        retriever=retriever,
        resolver=SchemeResolver(catalog),
        generator=generator,
        composer=Composer(registry, catalog, settings.default_source_id),
        catalog=catalog,
        history_turns=settings.history_turns,
    )
