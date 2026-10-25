"""One scheduled refresh run: fetch → build → index, with a machine-readable report (P7.3, P7.5).

`mf-faq ingest --report report.json` runs this. The outcome tells the workflow what to do:

    published      a new index version passed every gate and is now live → publish the state
    unchanged      the gates passed and the index is the one already live → publish nothing
    gate_failed    a build or index gate failed → the live index is untouched; alert
    fetch_blocked  no page could be downloaded (every fetch failed, or a page has no copy at
                   all) → nothing was built; alert

Exit codes: 0 = the live index is current (published or unchanged), 1 = gate failed or fetch
blocked (live index untouched); registry/config errors raise and the CLI exits 2.

Stale streak (P7.5): `data/refresh_status.json` records, per source, the IST date its current
run of failed fetches began. It travels with the state bundle and is also uploaded on its own
after every run (an `unchanged` run publishes no bundle), so the count survives quiet days. A
source `stale` for `stale_alert_days` days in a row (Groww blocking the runner's IPs looks
exactly like this) is listed under `alerts`; the workflow fails on any alert even when the build
itself passed, so a silently frozen corpus can't go unnoticed.
"""

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from mf_faq.ingest.build import MANIFEST_FILE, build_corpus
from mf_faq.ingest.chunk import MAX_TOKENS
from mf_faq.ingest.fetch import IST, FetchResult, fetch_all, read_meta
from mf_faq.ingest.index import (
    Embedder,
    IndexBuildError,
    IndexGate,
    build_index,
    current_version,
    index_root,
)
from mf_faq.ingest.registry import load_sources
from mf_faq.ingest.state import STATUS_FILE
from mf_faq.logs import log_event

logger = logging.getLogger(__name__)

PUBLISHED, UNCHANGED, GATE_FAILED, FETCH_BLOCKED = (
    "published",
    "unchanged",
    "gate_failed",
    "fetch_blocked",
)
EXIT_CODES = {PUBLISHED: 0, UNCHANGED: 0, GATE_FAILED: 1, FETCH_BLOCKED: 1}


@dataclass
class SourceReport:
    id: str
    status: str | None  # ok | stale | missing | manual (from the fetch, or the meta file)
    http_status: int | None = None
    error: str | None = None
    fetched_at: str | None = None  # the footer date after this run
    facts_changed: bool | None = None  # vs the live index; None when nothing was built
    chunk_count: int | None = None
    stale_days: int = 0


@dataclass
class RefreshReport:
    outcome: str
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    fetched: bool = True
    old: dict = field(default_factory=dict)  # corpus_version, index_version
    new: dict = field(default_factory=dict)
    sources: list[SourceReport] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.outcome]

    def to_dict(self) -> dict:
        return asdict(self) | {"exit_code": self.exit_code}


def _live_manifest(data_dir: Path) -> dict:
    version = current_version(data_dir)
    if version is None:
        return {}
    try:
        return json.loads((index_root(data_dir) / version / MANIFEST_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _read_status(data_dir: Path) -> dict:
    try:
        return json.loads((data_dir / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_stale_status(
    data_dir: Path, results: Sequence[FetchResult], now: datetime, alert_days: int
) -> tuple[dict[str, int], list[str]]:
    """Advance each source's stale streak; returns days stale per source, and the alerts."""
    status = _read_status(data_dir)
    sources = status.get("sources", {})
    today = now.astimezone(IST).date()
    days: dict[str, int] = {}
    alerts: list[str] = []
    for r in results:
        entry = dict(sources.get(r.source_id, {}))
        if r.status in ("ok", "manual"):
            entry |= {"stale_since": None, "last_ok_at": r.downloaded_at}
            days[r.source_id] = 0
        else:
            since = entry.get("stale_since") or today.isoformat()
            entry["stale_since"] = since
            n = (today - date.fromisoformat(since)).days + 1
            days[r.source_id] = n
            if n >= alert_days:
                alerts.append(
                    f"{r.source_id}: {r.status} for {n} days in a row (since {since}; last good "
                    f"download {entry.get('last_ok_at') or r.downloaded_at or 'never'}): "
                    f"{r.error or 'fetch failed'}"
                )
        entry["last_status"] = r.status
        sources[r.source_id] = entry
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": now.isoformat(timespec="seconds"), "sources": sources}
    (data_dir / STATUS_FILE).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return days, alerts


def run_refresh(
    corpus_dir: Path,
    data_dir: Path,
    *,
    embedder: Embedder,
    query_instruction: str,
    index_gates: Callable[[Embedder], Sequence[IndexGate]] = lambda e: (),
    fetch: bool = True,
    only: str | None = None,
    max_tokens: int = MAX_TOKENS,
    stale_alert_days: int = 3,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> RefreshReport:
    clock = time.perf_counter()
    started = now or datetime.now(UTC)
    sources = load_sources(corpus_dir / "sources.yaml")  # RegistryError → exit 2
    raw_dir = corpus_dir / "raw"
    live = _live_manifest(data_dir)
    old_facts = {s["id"]: s.get("content_sha256") for s in live.get("sources", [])}
    report = RefreshReport(
        outcome=UNCHANGED,
        started_at=started.isoformat(timespec="seconds"),
        fetched=fetch,
        old={"corpus_version": live.get("corpus_version"), "index_version": None},
    )
    report.old["index_version"] = current_version(data_dir)

    by_id: dict[str, SourceReport] = {}
    results: list[FetchResult] = []
    if fetch:
        results = fetch_all(sources, raw_dir, only=only, transport=transport, sleep=sleep)
        days, report.alerts = update_stale_status(data_dir, results, started, stale_alert_days)
        for r in results:
            by_id[r.source_id] = SourceReport(
                r.source_id, r.status, r.http_status, r.error, stale_days=days[r.source_id]
            )
    for s in sources:  # sources not fetched this run: their status as recorded
        by_id.setdefault(s.id, SourceReport(s.id, read_meta(raw_dir, s.id).get("status")))
    report.sources = [by_id[s.id] for s in sources]

    missing = any(r.status == "missing" for r in report.sources)
    if missing or (results and not any(r.status == "ok" for r in results)):
        report.outcome = FETCH_BLOCKED
        report.failures = [
            f"{r.id}: {r.status} ({r.error or 'no usable copy'})"
            for r in report.sources
            if r.status not in ("ok", "manual")
        ]
        return _finish(report, data_dir, clock)

    build = build_corpus(
        corpus_dir, data_dir, tokenizer=embedder, max_tokens=max_tokens, now=started
    )
    if not build.ok:
        report.outcome, report.failures = GATE_FAILED, build.failures
        return _finish(report, data_dir, clock)
    for p in build.pages:
        r = by_id[p.source.id]
        r.fetched_at = p.meta["fetched_at"]
        r.facts_changed = old_facts.get(p.source.id) != p.content_sha256
        r.chunk_count = len(p.chunks)

    try:
        result = build_index(
            data_dir,
            embedder,
            query_instruction=query_instruction,
            gates=index_gates(embedder),
            now=started,
        )
    except IndexBuildError as e:
        report.outcome, report.failures = GATE_FAILED, [str(e)]
        return _finish(report, data_dir, clock)
    if not result.ok:
        report.outcome, report.failures = GATE_FAILED, result.failures
        return _finish(report, data_dir, clock)
    report.outcome = PUBLISHED if result.version != report.old["index_version"] else UNCHANGED
    return _finish(report, data_dir, clock)


def _finish(report: RefreshReport, data_dir: Path, clock: float) -> RefreshReport:
    live = _live_manifest(data_dir)
    report.new = {
        "corpus_version": live.get("corpus_version"),
        "index_version": current_version(data_dir),
    }
    report.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    report.duration_s = round(time.perf_counter() - clock, 1)
    level = logging.INFO if report.exit_code == 0 and not report.alerts else logging.ERROR
    log_event(
        logger,
        "refresh.completed",
        level=level,
        outcome=report.outcome,
        old_index=report.old.get("index_version"),
        new_index=report.new.get("index_version"),
        failures=len(report.failures),
        alerts=len(report.alerts),
    )
    return report


def format_summary(report: RefreshReport) -> str:
    """Markdown for the job summary and the `refresh-failed` issue body."""
    icon = {PUBLISHED: "✅", UNCHANGED: "➖", GATE_FAILED: "❌", FETCH_BLOCKED: "⛔"}
    lines = [
        f"## Daily refresh: {icon[report.outcome]} `{report.outcome}`",
        "",
        f"- Started {report.started_at}, took {report.duration_s} s"
        + ("" if report.fetched else " (no fetch: built from the saved copies)"),
        f"- Corpus: `{report.old.get('corpus_version')}` → `{report.new.get('corpus_version')}`",
        f"- Index: `{report.old.get('index_version')}` → `{report.new.get('index_version')}`",
        "",
        "| Source | Fetch | Facts changed | Footer date (fetched_at) | Chunks | Days stale |",
        "|---|---|---|---|---|---|",
    ]
    for s in report.sources:
        changed = {True: "yes", False: "no", None: "–"}[s.facts_changed]
        fetch = s.status or "–"
        if s.error:
            fetch += f" ({s.error})"
        lines.append(
            f"| `{s.id}` | {fetch} | {changed} | {(s.fetched_at or '–')[:10]} | "
            f"{s.chunk_count if s.chunk_count is not None else '–'} | {s.stale_days} |"
        )
    for title, items in (("Failures", report.failures), ("Alerts", report.alerts)):
        if items:
            lines += ["", f"### {title}", "", *(f"- {item}" for item in items)]
    return "\n".join(lines) + "\n"
