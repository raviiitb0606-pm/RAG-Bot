"""`mf-faq` command-line entry point (P0.12). The full `eval` comes in Phase 9."""

import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from mf_faq.config import get_settings
from mf_faq.logs import configure_logging
from mf_faq.redacted import RedactedText

app = typer.Typer(
    help="Facts-only FAQ assistant for 5 HDFC Mutual Fund schemes. No investment advice.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    configure_logging(get_settings().log_level)


def _not_implemented(phase: str) -> None:
    typer.echo(f"Not implemented yet ({phase}).", err=True)
    raise typer.Exit(code=1)


@app.command()
def ingest(
    source: Annotated[
        str | None, typer.Option(help="Only re-download this source id (all 5 are rebuilt).")
    ] = None,
    dump: Annotated[
        str | None, typer.Option(help="Print the chunks of this source id and exit.")
    ] = None,
    fetch_only: Annotated[
        bool, typer.Option("--fetch-only", help="Only download the pages to corpus/raw/.")
    ] = False,
    skip_fetch: Annotated[
        bool, typer.Option("--skip-fetch", help="Build from the copies already in corpus/raw/.")
    ] = False,
    manual_copy: Annotated[
        str | None,
        typer.Option(help="Register a browser-saved corpus/raw/<id>.html (needs --saved-on)."),
    ] = None,
    saved_on: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="Save date of --manual-copy.")
    ] = None,
    report: Annotated[
        Path | None,
        typer.Option(help="Scheduled-refresh mode: write the run's outcome here as JSON (P7.3)."),
    ] = None,
    summary: Annotated[
        Path | None, typer.Option(help="With --report: also write a Markdown summary here.")
    ] = None,
) -> None:
    """Fetch, parse, strip returns and chunk the 5 Groww pages, then build the search index.

    With --report, exit codes are 0 = the live index is current (new or unchanged), 1 = a gate
    failed or every fetch failed (live index untouched), 2 = registry/config error.
    """
    from mf_faq.ingest.registry import RegistryError, load_sources

    settings = get_settings()
    raw_dir = settings.corpus_dir / "raw"
    try:
        sources = load_sources(settings.corpus_dir / "sources.yaml")
        if report:
            if dump or manual_copy or fetch_only:
                raise ValueError("--report excludes --dump, --manual-copy and --fetch-only")
            _refresh(report, summary, fetch=not skip_fetch, only=source)
        elif dump:
            _dump(sources, dump)
        elif manual_copy:
            _manual_copy(sources, raw_dir, manual_copy, saved_on)
        else:
            if not skip_fetch:
                _fetch(sources, raw_dir, source, strict=fetch_only)
            if not fetch_only:
                # One model load: its tokenizer splits the chunks (P3.0), then it embeds them.
                embedder = _embedder()
                _build(embedder)
                _index(embedder)
    except (RegistryError, ValueError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=2) from e


def _refresh(report_path: Path, summary_path: Path | None, *, fetch: bool, only: str | None):
    import json

    from mf_faq.ingest.refresh import format_summary, run_refresh

    settings = get_settings()
    report = run_refresh(
        settings.corpus_dir,
        settings.data_dir,
        embedder=_embedder(),
        query_instruction=settings.embed_query_instruction,
        index_gates=_index_gates,
        fetch=fetch,
        only=only,
        max_tokens=settings.holdings_max_tokens,
        stale_alert_days=settings.stale_alert_days,
    )
    report_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n")
    text = format_summary(report)
    if summary_path:
        with summary_path.open("a", encoding="utf-8") as f:  # $GITHUB_STEP_SUMMARY appends
            f.write(text)
    typer.echo(text)
    if report.exit_code:
        raise typer.Exit(code=report.exit_code)


def _source(sources: list, source_id: str):
    match = next((s for s in sources if s.id == source_id), None)
    if match is None:
        raise ValueError(f"unknown source id: {source_id}")
    return match


def _fetch(sources: list, raw_dir: Path, only: str | None, *, strict: bool) -> None:
    from mf_faq.ingest.fetch import fetch_all

    results = fetch_all(sources, raw_dir, only=only)
    for r in results:
        detail = f"HTTP {r.http_status}, {r.attempts} attempt(s)"
        typer.echo(f"{r.status:8} {r.source_id:28} {detail}" + (f" - {r.error}" if r.error else ""))
    # A stale copy still builds (FET-10); only --fetch-only reports it as a failure.
    if strict and any(r.status != "ok" for r in results):
        raise typer.Exit(code=1)


def _embedder():
    from mf_faq.ingest.index import IndexBuildError, embedder_from

    try:
        return embedder_from(get_settings())
    except IndexBuildError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _build(embedder) -> None:
    from mf_faq.ingest.build import build_corpus

    settings = get_settings()
    result = build_corpus(
        settings.corpus_dir,
        settings.data_dir,
        tokenizer=embedder,
        max_tokens=settings.holdings_max_tokens,
    )
    if not result.ok:
        typer.echo("Build failed; nothing was written:", err=True)
        for failure in result.failures:
            typer.echo(f"  - {failure}", err=True)
        raise typer.Exit(code=1)
    for p in result.pages:
        typer.echo(
            f"{p.meta.get('status', ''):8} {p.source.id:28} {len(p.chunks):3} chunks  "
            f"fetched_at {p.meta['fetched_at'][:10]}  facts {p.content_sha256[:12]}"
        )
    manifest = result.manifest
    typer.echo(
        f"corpus {manifest['corpus_version']}: {manifest['chunk_count']} chunks "
        f"(max {manifest['max_chunk_tokens']} tokens) -> {settings.data_dir}/"
    )


def _index_gates(embedder) -> list:
    """Extra swap gates: the retrieval eval must pass before a new index goes live (P3.5)."""
    from mf_faq.eval.retrieval import retrieval_gate

    settings = get_settings()
    return [
        retrieval_gate(
            embedder,
            _catalog(),
            settings.eval_dir,
            query_instruction=settings.embed_query_instruction,
            top_k=settings.retrieval_top_k,
            threshold=settings.retrieval_threshold,
        )
    ]


def _catalog():
    from mf_faq.ingest.registry import load_schemes, load_sources

    corpus_dir = get_settings().corpus_dir
    return load_schemes(corpus_dir / "schemes.yaml", load_sources(corpus_dir / "sources.yaml"))


def _index(embedder=None) -> None:
    from mf_faq.ingest.index import IndexBuildError, build_index

    settings = get_settings()
    embedder = embedder or _embedder()
    try:
        result = build_index(
            settings.data_dir,
            embedder,
            query_instruction=settings.embed_query_instruction,
            gates=_index_gates(embedder),
        )
    except IndexBuildError as e:
        typer.echo(f"Index not built: {e}", err=True)
        raise typer.Exit(code=1) from e
    if not result.ok:
        typer.echo("Index gates failed; the live index is unchanged:", err=True)
        for failure in result.failures:
            typer.echo(f"  - {failure}", err=True)
        raise typer.Exit(code=1)
    meta = result.meta
    state = "now live" if result.swapped else "unchanged, already live"
    typer.echo(
        f"index {result.version}: {meta['chunk_count']} vectors, {meta['dim']}-d "
        f"{meta['embed_model']}@{(meta['embed_model_revision'] or '?')[:7]} ({state})"
    )


@app.command()
def index(
    rollback: Annotated[
        bool, typer.Option("--rollback", help="Make the previous index version live again.")
    ] = False,
) -> None:
    """Rebuild the search index from data/chunks.jsonl; it goes live only if the eval passes."""
    if rollback:
        from mf_faq.ingest.index import IndexLoadError, rollback_index

        try:
            version = rollback_index(get_settings().data_dir)
        except IndexLoadError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        typer.echo(f"index {version}: now live (rolled back)")
        return
    _index()


def _dump(sources: list, source_id: str) -> None:
    from mf_faq.ingest.build import process_html
    from mf_faq.ingest.chunk import ChunkError
    from mf_faq.ingest.parse_html import ParseError
    from mf_faq.ingest.registry import load_schemes

    settings = get_settings()
    src = _source(sources, source_id)
    catalog = load_schemes(settings.corpus_dir / "schemes.yaml", sources)
    path = settings.corpus_dir / "raw" / f"{src.id}.html"
    embedder = _embedder()  # split exactly as the build does
    try:
        page, chunks, digest = process_html(
            path.read_text(encoding="utf-8"),
            src,
            catalog,
            tokenizer=embedder,
            max_tokens=settings.holdings_max_tokens,
        )
    except (OSError, ParseError, ChunkError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(
        f"# {page.scheme_name} ({src.id}): {len(chunks)} chunks, facts {digest[:12]}, "
        f"has_performance_section={page.has_performance_section}"
    )
    for c in chunks:
        tokens = embedder.count_tokens(c.content)
        typer.echo(f"\n## {c.chunk_id}  ({tokens} tokens; {', '.join(c.facts)})")
        typer.echo(c.content)


def _manual_copy(sources: list, raw_dir: Path, source_id: str, saved_on: datetime | None) -> None:
    from mf_faq.ingest.fetch import FetchError, register_manual_copy

    if saved_on is None:
        raise ValueError("--manual-copy needs --saved-on YYYY-MM-DD (FET-09)")
    try:
        result = register_manual_copy(_source(sources, source_id), raw_dir, saved_on.date())
    except FetchError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"manual   {result.source_id:28} saved on {saved_on.date().isoformat()}")
    typer.echo("Now run: mf-faq ingest --skip-fetch")


state_app = typer.Typer(
    help="Pack or restore the refresh state (pages, meta, manifest, live index; P7.2).",
    no_args_is_help=True,
)
app.add_typer(state_app, name="state")


@state_app.command("export")
def state_export(
    bundle: Annotated[Path, typer.Argument(help="Bundle to write (.tar.gz; a .sha256 is added).")],
) -> None:
    """Pack corpus/raw/, data/manifest.json, data/chunks.jsonl and the live index."""
    from mf_faq.ingest.state import StateError, export_state

    settings = get_settings()
    try:
        state = export_state(settings.corpus_dir, settings.data_dir, bundle)
    except (StateError, OSError, ValueError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"exported corpus {state['corpus_version']}, index {state['index_version']}")
    typer.echo(f"  -> {bundle} (+ {bundle.name}.sha256)")


@state_app.command("import")
def state_import(
    bundle: Annotated[Path, typer.Argument(help="Bundle written by `mf-faq state export`.")],
) -> None:
    """Restore a bundle; refuses a bad checksum or an index built for another config."""
    from mf_faq.ingest.state import StateError, import_state

    settings = get_settings()
    try:
        result = import_state(
            bundle,
            settings.corpus_dir,
            settings.data_dir,
            model_name=settings.embed_model,
            query_instruction=settings.embed_query_instruction,
        )
    except StateError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    state = result.state
    typer.echo(
        f"imported corpus {state['corpus_version']}, index {result.index_version} "
        f"(created {state['created_at']})"
    )


@app.command(name="pull-index")
def pull_index(
    release: Annotated[str, typer.Option(help="Release tag to install.")] = "data-latest",
) -> None:
    """Download the newest published index, verify it and make it live (server side, P7.6).

    Needs MF_FAQ_GITHUB_REPO and a read-only MF_FAQ_GITHUB_TOKEN. A running server picks the
    new version up within MF_FAQ_INDEX_RELOAD_S, without a restart.
    """
    from mf_faq.ingest.state import StateError
    from mf_faq.ingest.state import pull_index as pull

    settings = get_settings()
    if not settings.github_repo or settings.github_token is None:
        typer.echo("Error: set MF_FAQ_GITHUB_REPO and MF_FAQ_GITHUB_TOKEN", err=True)
        raise typer.Exit(code=2)
    try:
        result = pull(
            settings.data_dir,
            repo=settings.github_repo,
            token=settings.github_token.get_secret_value(),
            model_name=settings.embed_model,
            query_instruction=settings.embed_query_instruction,
            release=release,
        )
    except StateError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    state = "now live" if result.swapped else "unchanged, already live"
    typer.echo(f"index {result.index_version} (corpus {result.state['corpus_version']}): {state}")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
) -> None:
    """Start the API and chat UI at http://HOST:PORT (needs GROQ_API_KEY and an index)."""
    import uvicorn

    from mf_faq.api.app import create_app
    from mf_faq.ingest.index import IndexLoadError
    from mf_faq.pipeline.sources import RegistryLoadError

    try:
        api = create_app()
    except (IndexLoadError, RegistryLoadError, RuntimeError, ValueError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"Serving on http://{host}:{port} (API docs at /docs)")
    # log_config=None keeps our JSON logging; access logs off (paths carry thread ids only,
    # but there's no need for a second, unstructured log stream).
    uvicorn.run(api, host=host, port=port, log_config=None, access_log=False)


@app.command()
def ask(
    questions: Annotated[list[str], typer.Argument(help="One or more questions, asked in order.")],
    thread: Annotated[
        bool, typer.Option("--thread/--separate", help="Treat the questions as one conversation.")
    ] = False,
) -> None:
    """Ask the live pipeline (needs GROQ_API_KEY and an index); used for the P5 smoke run."""
    from mf_faq.pipeline.answer import ThreadContext, build_pipeline
    from mf_faq.pipeline.generate import Turn

    pipeline = build_pipeline()
    ctx = ThreadContext()
    for question in questions:
        started = time.perf_counter()
        result = pipeline.answer_turn(ctx, question)
        elapsed = time.perf_counter() - started
        typer.echo(f"\nQ: {result.user_text}\n[{result.reply.kind.value}, {elapsed:.1f}s]")
        typer.echo(result.reply.text)
        if thread:
            history = [
                *ctx.history,
                Turn("user", result.user_text),
                Turn("assistant", RedactedText(result.reply.answer)),
            ]
            ctx = ThreadContext(history, result.active_scheme_id)


@app.command(name="eval")
def eval_(
    retrieval_only: Annotated[
        bool, typer.Option("--retrieval-only", help="Score retrieval only (hit@4, not-found).")
    ] = False,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Also write the full report as JSON here.")
    ] = None,
) -> None:
    """Run the golden-set evaluation (exit code 1 when a gate fails)."""
    if not retrieval_only:
        _not_implemented("Phase 9; use --retrieval-only")
    import json

    from mf_faq.eval.retrieval import evaluate, format_report
    from mf_faq.ingest.index import IndexLoadError
    from mf_faq.pipeline.retrieve import load_retriever

    settings = get_settings()
    catalog = _catalog()
    try:
        retriever = load_retriever(settings, catalog, _embedder())
    except IndexLoadError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    report = evaluate(retriever, catalog, settings.eval_dir)
    typer.echo(f"index {retriever.index.version}")
    typer.echo(format_report(report))
    if json_out:
        json_out.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n")
    if not report.ok:
        raise typer.Exit(code=1)
