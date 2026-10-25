"""FastAPI app and routes (P6.4-P6.9, arch §9.1).

`create_app()` loads the pipeline (index, registry, Groq client) once, before serving: it is
read-only and shared by every request (P6.5, arch §8.2). Each turn runs in the thread pool
(the pipeline is synchronous) under a per-thread lock, so two messages to the same thread can't
interleave their history, while different threads run side by side.

A turn: load the thread's last redacted turns and active scheme → `answer_turn` → store the
redacted question and the reply → save the new active scheme. Only `RedactedText` reaches the
store and the logs (P-1).

Also here: per-IP rate limits on the endpoints that write (P6.6), no CORS middleware (the UI
is served from the same origin, so cross-origin calls stay blocked), a body-size cap, security
headers, error handlers that never echo input or internals, and the retention job (P6.7).

Index reload (P7.6): every `MF_FAQ_INDEX_RELOAD_S` the app re-reads `data/index/current`
(repointed by `mf-faq pull-index`). On a change it builds a new pipeline in the background and
swaps it in: a turn takes its pipeline once at the start, so in-flight turns finish on the old
one, which is closed a tick later. If loading fails, the old index stays and
`index.reload_failed` is logged; `/api/health`, `/api/meta` and footer dates follow the swap.
"""

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from mf_faq.api.schemas import (
    ErrorOut,
    Example,
    HealthOut,
    MessageIn,
    MessageList,
    MessageOut,
    MetaOut,
    ReplyOut,
    SchemeOut,
    ThreadList,
    ThreadOut,
)
from mf_faq.config import Settings, get_settings
from mf_faq.ingest.registry import SchemeCatalog
from mf_faq.logs import log_event
from mf_faq.pipeline.answer import AnswerPipeline, ThreadContext
from mf_faq.pipeline.generate import Turn
from mf_faq.pipeline.sources import SourceRegistry
from mf_faq.redacted import RedactedText
from mf_faq.threads.store import MessageRow, ThreadNotFound, ThreadRow, ThreadStore

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

DISCLAIMER = "Facts-only. No investment advice."
WELCOME = (
    "Welcome! I answer factual questions about 5 HDFC Mutual Fund schemes, using their Groww "
    "scheme pages."
)
PII_HINT = "Don't share PAN, Aadhaar, account number or OTP."

# UI-2: one question per fact, each on a scheme whose page shows that fact (checked at startup).
EXAMPLES: tuple[tuple[str, str, str], ...] = (
    ("hdfc-mid-cap", "expense_ratio", "What is the expense ratio of {name} – Direct Growth?"),
    ("hdfc-small-cap", "exit_load", "What is the exit load of {name}?"),
    ("hdfc-multi-cap", "min_sip", "What is the minimum SIP amount for {name}?"),
)

MAX_BODY_BYTES = 8 * 1024  # a 500-character question is far below this
PURGE_INTERVAL_S = 24 * 60 * 60
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")  # Swagger UI loads its assets from a CDN


def examples(catalog: SchemeCatalog) -> list[Example]:
    """The 3 example questions; each asks for a fact its scheme's page shows (P6.4)."""
    out = []
    for scheme_id, fact, question in EXAMPLES:
        scheme = catalog.by_id(scheme_id)
        if fact not in scheme.available_facts:
            raise ValueError(f"example fact {fact!r} is not on the {scheme_id} page")
        out.append(
            Example(
                question=question.format(name=scheme.short_name),
                fact=fact,
                label=catalog.fact_labels[fact],
                scheme_id=scheme_id,
            )
        )
    return out


def index_versions(data_dir: Path, version: str | None = None) -> tuple[str | None, str | None]:
    """(live index version, its corpus version) for `/api/health` and `/api/meta`."""
    from mf_faq.ingest.index import current_version

    version = version or current_version(data_dir)
    if version is None:
        return None, None
    try:
        manifest = json.loads((data_dir / "index" / version / "manifest.json").read_text())
    except (OSError, ValueError):
        return version, None
    return version, manifest.get("corpus_version")


def build_meta(pipeline: AnswerPipeline, settings: Settings, corpus_version: str | None) -> MetaOut:
    return MetaOut(
        welcome=WELCOME,
        disclaimer=DISCLAIMER,
        pii_hint=PII_HINT,
        examples=examples(pipeline.catalog),
        schemes=[
            SchemeOut(
                scheme_id=s.scheme_id,
                name=s.name,
                short_name=s.short_name,
                category=s.category,
                plan=s.plan,
                source_url=pipeline.registry.get(s.source_id).url,
            )
            for s in pipeline.catalog.schemes
        ],
        corpus_version=corpus_version,
        thread_ttl_days=settings.thread_ttl_days,
    )


# Builds the pipeline for one index version (the reload, P7.6).
PipelineLoader = Callable[[str], AnswerPipeline]


@dataclass
class Service:
    pipeline: AnswerPipeline
    store: ThreadStore
    settings: Settings
    index_version: str | None = None
    corpus_version: str | None = None
    loader: PipelineLoader | None = None
    meta: MetaOut | None = None
    _retired: list[AnswerPipeline] = field(default_factory=list)
    _reload_guard: threading.Lock = field(default_factory=threading.Lock)
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _locks_guard: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.meta = self.meta or build_meta(self.pipeline, self.settings, self.corpus_version)

    def reload_if_changed(self) -> bool:
        """One reload tick: swap in the index `current` points at, if it changed (P7.6)."""
        from mf_faq.ingest.index import current_version

        with self._reload_guard:
            for old in self._retired:  # swapped out a tick ago: its turns have finished
                old.close()
            self._retired.clear()
            version = current_version(self.settings.data_dir)
            if self.loader is None or version is None or version == self.index_version:
                return False
            try:
                pipeline = self.loader(version)
                _, corpus_version = index_versions(self.settings.data_dir, version)
                meta = build_meta(pipeline, self.settings, corpus_version)
            except Exception as e:  # keep serving the old index
                log_event(
                    logger,
                    "index.reload_failed",
                    logging.ERROR,
                    version=version,
                    serving=self.index_version,
                    exc_type=type(e).__name__,
                    error=str(e)[:300],
                )
                return False  # retried next tick (a fixed pull repoints `current` again)
            old, old_version = self.pipeline, self.index_version
            self.pipeline, self.meta = pipeline, meta
            self.index_version, self.corpus_version = version, corpus_version
            self._retired.append(old)
        log_event(
            logger,
            "index.reloaded",
            index_version=version,
            corpus_version=corpus_version,
            previous=old_version,
        )
        return True

    def lock(self, thread_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(thread_id, threading.Lock())

    def forget(self, thread_id: str) -> None:
        with self._locks_guard:
            self._locks.pop(thread_id, None)

    def purge(self) -> int:
        removed = self.store.purge_older_than(self.settings.thread_ttl_days)
        log_event(logger, "threads.purged", removed=removed, ttl_days=self.settings.thread_ttl_days)
        return removed


def _thread_out(row: ThreadRow) -> ThreadOut:
    return ThreadOut(
        id=row.id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
        message_count=row.message_count,
    )


def _message_out(row: MessageRow, registry: SourceRegistry) -> MessageOut:
    url = title = None
    if row.source_id and row.source_id in registry:
        source = registry.get(row.source_id)
        url, title = source.url, source.title
    return MessageOut(
        id=row.id,
        role=row.role,
        kind=row.kind,
        text=row.content,
        created_at=row.created_at,
        source_url=url,
        source_title=title,
        last_updated=row.last_updated,
    )


def _parse_ids(raw: str) -> list[str]:
    ids = []
    for part in raw.split(","):
        try:
            ids.append(str(UUID(part.strip())))
        except ValueError:
            continue  # ids the browser kept from elsewhere are ignored, not an error
    return ids


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


def create_app(
    settings: Settings | None = None,
    *,
    pipeline: AnswerPipeline | None = None,
    store: ThreadStore | None = None,
    index_version: str | None = None,
    corpus_version: str | None = None,
    web_dir: Path | None = WEB_DIR,
    loader: PipelineLoader | None = None,
) -> FastAPI:
    """Build the app. Without a `pipeline`, the live one is loaded now (fails fast, P5.6).

    `loader` builds the pipeline for a new index version on reload; by default (live pipeline
    only) it reuses the running query encoder and Groq client.
    """
    settings = settings or get_settings()
    if pipeline is None:
        from mf_faq.ingest.index import current_version
        from mf_faq.pipeline.answer import build_pipeline

        version = current_version(settings.data_dir)
        pipeline = build_pipeline(settings, version=version)
        index_version, corpus_version = index_versions(settings.data_dir, version)
        if loader is None:
            live = pipeline

            def loader(v: str) -> AnswerPipeline:
                return build_pipeline(
                    settings,
                    generator=live.generator,
                    encoder=live.retriever.encoder,
                    version=v,
                )

    svc = Service(
        pipeline=pipeline,
        store=store or ThreadStore(settings.db_path),
        settings=settings,
        index_version=index_version,
        corpus_version=corpus_version,
        loader=loader,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(svc.purge)  # P6.7: on startup…

        async def daily() -> None:  # …and once a day while running
            while True:
                await asyncio.sleep(PURGE_INTERVAL_S)
                try:
                    await asyncio.to_thread(svc.purge)
                except Exception as e:  # keep the job alive; the next run retries
                    log_event(
                        logger, "threads.purge_failed", logging.ERROR, exc_type=type(e).__name__
                    )

        async def watch_index() -> None:  # P7.6
            while True:
                await asyncio.sleep(settings.index_reload_s)
                try:
                    await asyncio.to_thread(svc.reload_if_changed)
                except Exception as e:  # keep watching; the next tick retries
                    log_event(
                        logger, "index.reload_failed", logging.ERROR, exc_type=type(e).__name__
                    )

        tasks = [asyncio.create_task(daily())]
        if settings.index_reload_s and svc.loader is not None:
            tasks.append(asyncio.create_task(watch_index()))
        log_event(
            logger,
            "api.started",
            index_version=svc.index_version,
            corpus_version=svc.corpus_version,
        )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

    app = FastAPI(
        title="Mutual Fund FAQ Assistant",
        description="Facts-only answers about 5 HDFC Mutual Fund schemes. No investment advice.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.service = svc
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    write_limit = limiter.limit(settings.api_rate_limit)
    errors: dict[int | str, dict] = {404: {"model": ErrorOut}, 429: {"model": ErrorOut}}

    # -- middleware and error handlers -------------------------------------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next) -> Response:
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
            return _error(413, "Request too large.")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith(DOCS_PATHS):
            response.headers["Content-Security-Policy"] = CSP
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ThreadNotFound)
    async def not_found(request: Request, exc: ThreadNotFound) -> JSONResponse:
        return _error(404, "Thread not found.")

    @app.exception_handler(RateLimitExceeded)
    async def rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        log_event(logger, "api.rate_limited", path=request.url.path)
        return _error(429, "Too many requests. Please try again in a minute.")

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field, message and type only: the default body echoes the input, which may hold PII.
        detail = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    @app.exception_handler(Exception)
    async def crashed(request: Request, exc: Exception) -> JSONResponse:
        log_event(
            logger, "api.error", logging.ERROR, path=request.url.path, exc_type=type(exc).__name__
        )
        return _error(500, "Something went wrong on our side. Please try again.")

    # -- routes ------------------------------------------------------------------------------

    @app.get("/api/meta", response_model=MetaOut, tags=["meta"])
    def get_meta() -> MetaOut:
        """Welcome text, 3 example questions, disclaimer and the 5 supported schemes."""
        return svc.meta

    @app.get("/api/health", response_model=HealthOut, tags=["meta"])
    def health() -> HealthOut:
        """Liveness, plus which index and corpus version are being served."""
        return HealthOut(
            status="ok",
            index_loaded=True,  # the app doesn't start without one
            index_version=svc.index_version,
            corpus_version=svc.corpus_version,
        )

    @app.post("/api/threads", response_model=ThreadOut, status_code=201, tags=["threads"])
    @write_limit
    def create_thread(request: Request) -> ThreadOut:
        """Start a new, empty conversation."""
        return _thread_out(svc.store.create_thread())

    @app.get("/api/threads", response_model=ThreadList, tags=["threads"])
    def list_threads(ids: str = "") -> ThreadList:
        """The threads with these comma-separated ids that still exist, most recent first."""
        return ThreadList(threads=[_thread_out(t) for t in svc.store.list_threads(_parse_ids(ids))])

    @app.get(
        "/api/threads/{thread_id}/messages",
        response_model=MessageList,
        responses=errors,
        tags=["threads"],
    )
    def get_messages(thread_id: UUID) -> MessageList:
        """The thread's history (questions as stored: redacted)."""
        rows = svc.store.get_messages(str(thread_id))
        registry = svc.pipeline.registry
        return MessageList(messages=[_message_out(r, registry) for r in rows])

    @app.post(
        "/api/threads/{thread_id}/messages",
        response_model=ReplyOut,
        responses=errors,
        tags=["threads"],
    )
    @write_limit
    def post_message(request: Request, thread_id: UUID, body: MessageIn) -> ReplyOut:
        """Ask a question in this thread and get the assistant's reply."""
        tid = str(thread_id)
        pipeline = svc.pipeline  # the whole turn uses one index, even if a reload swaps it
        with svc.lock(tid):
            thread = svc.store.get_thread(tid)
            history = _history(svc.store.get_messages(tid, limit=svc.settings.history_turns))
            ctx = ThreadContext(
                history=history,
                active_scheme_id=thread.active_scheme_id,
                thread_id=tid,
                turn=thread.message_count // 2 + 1,
            )
            result = pipeline.answer_turn(ctx, body.text)
            reply = result.reply
            user = svc.store.append_message(tid, "user", result.user_text)
            # The body is fixed copy or a validated answer from public page text: the model only
            # ever saw the redacted question, so there is no user PII in it to redact.
            assistant = svc.store.append_message(
                tid,
                "assistant",
                RedactedText(reply.answer),
                kind=reply.kind.value,
                source_id=reply.source_id,
                last_updated=reply.last_updated,
            )
            svc.store.set_active_scheme(tid, result.active_scheme_id)
            thread = svc.store.get_thread(tid)
        return ReplyOut(
            kind=reply.kind.value,
            answer=reply.answer,
            source_url=reply.source_url,
            source_title=reply.source_title,
            last_updated=reply.last_updated,
            text=reply.text,
            created_at=assistant.created_at,
            user_text=user.content,
            thread=_thread_out(thread),
        )

    @app.delete("/api/threads/{thread_id}", status_code=204, responses=errors, tags=["threads"])
    @write_limit
    def delete_thread(request: Request, thread_id: UUID) -> Response:
        """Delete the thread and all its messages."""
        tid = str(thread_id)
        with svc.lock(tid):
            if not svc.store.delete_thread(tid):
                raise ThreadNotFound(tid)
        svc.forget(tid)
        return Response(status_code=204)

    if web_dir is not None and web_dir.is_dir():  # last, so /api and /docs win (P6.9)
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


def _history(rows: Sequence[MessageRow]) -> list[Turn]:
    return [Turn(r.role, RedactedText(r.content)) for r in rows]
