"""Download the 5 Groww pages politely and save them to corpus/raw/ (P2.2, arch §5.2).

Owner decision (P0.6): automated fetch. Each run makes one robots.txt request plus at most
5 page requests (more only on retries), with a delay between pages.

Per source this writes `<id>.html` (only after the page passes `check_page`) and
`<id>.meta.json`. The meta file records `downloaded_at` (last good download),
`last_checked_at`, `http_status` and `raw_sha256`. The footer date (`fetched_at`) and the
extracted-facts hash (`content_sha256`) are added to the same file by the build
(`ingest/build.py`), so ad or script changes don't move the date; fetching keeps them.

Manual fallback (P2.2, FET-09): a browser-saved page put at `corpus/raw/<id>.html` is
registered with `register_manual_copy()` (`mf-faq ingest --manual-copy <id> --saved-on DATE`),
which runs the same page checks and records the save date as `downloaded_at`.
"""

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from urllib import robotparser

import httpx

from mf_faq.ingest.registry import Source, is_allowed_url
from mf_faq.logs import log_event

logger = logging.getLogger(__name__)

USER_AGENT = "mf-faq/0.1 (non-commercial facts-only mutual fund FAQ assistant)"
ROBOTS_URL = "https://groww.in/robots.txt"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
RETRIES = 3
BACKOFF_S = 2.0
DELAY_BETWEEN_S = 3.0
# Written by the build, not by fetching; kept when the meta file is rewritten.
BUILD_META_KEYS = ("fetched_at", "content_sha256")
IST = timezone(timedelta(hours=5, minutes=30), "IST")

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class FetchError(Exception):
    """The response is not a usable copy of the expected scheme page."""


@dataclass
class FetchResult:
    source_id: str
    url: str
    status: str  # ok | stale (previous copy kept) | missing (no usable copy) | manual
    http_status: int | None
    attempts: int
    last_checked_at: str
    downloaded_at: str | None
    raw_sha256: str | None
    error: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def check_page(html: str, source: Source) -> None:
    """Reject block pages, error pages and the wrong scheme before anything is saved."""
    match = _NEXT_DATA_RE.search(html)
    if not match:
        raise FetchError("no __NEXT_DATA__ block (layout change or block page?)")
    try:
        data = json.loads(match.group(1))["props"]["pageProps"]["mfServerSideData"]
    except (ValueError, KeyError, TypeError) as e:
        raise FetchError("embedded JSON has no mfServerSideData") from e
    if data.get("search_id") != source.slug:
        raise FetchError(f"page is for {data.get('search_id')!r}, expected {source.slug!r}")


def robots_allows(client: httpx.Client, urls: list[str]) -> dict[str, bool]:
    """Check robots.txt. 404 means everything is allowed; any other failure means nothing is."""
    try:
        resp = client.get(ROBOTS_URL)
    except httpx.HTTPError:
        return dict.fromkeys(urls, False)
    if resp.status_code == 404:
        return dict.fromkeys(urls, True)
    if resp.status_code != 200:
        return dict.fromkeys(urls, False)
    parser = robotparser.RobotFileParser()
    parser.parse(resp.text.splitlines())
    return {u: parser.can_fetch(USER_AGENT, u) for u in urls}


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _read_meta(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_meta(raw_dir: Path, source_id: str) -> dict:
    return _read_meta(raw_dir / f"{source_id}.meta.json")


def write_meta(raw_dir: Path, source_id: str, meta: dict) -> None:
    path = raw_dir / f"{source_id}.meta.json"
    _write_atomic(path, json.dumps(meta, indent=2, ensure_ascii=False).encode())


def _save_result(meta_path: Path, result: FetchResult) -> None:
    previous = _read_meta(meta_path)
    meta = asdict(result) | {k: previous[k] for k in BUILD_META_KEYS if k in previous}
    _write_atomic(meta_path, json.dumps(meta, indent=2, ensure_ascii=False).encode())


def fetch_source(
    client: httpx.Client,
    source: Source,
    raw_dir: Path,
    *,
    retries: int = RETRIES,
    backoff_s: float = BACKOFF_S,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchResult:
    html_path = raw_dir / f"{source.id}.html"
    meta_path = raw_dir / f"{source.id}.meta.json"

    http_status: int | None = None
    error: str | None = None
    attempt = 0
    for attempt in range(1, retries + 2):
        try:
            resp = client.get(source.url)
        except httpx.HTTPError as e:
            http_status, error = None, type(e).__name__
        else:
            http_status = resp.status_code
            if resp.is_redirect:
                # Edge case FET-04: never follow; a human must update the registry.
                location = resp.headers.get("location", "")
                note = "" if is_allowed_url(location) else " (outside ALLOWED_URLS)"
                error = f"redirected to {location}{note}"
                break
            if http_status == 200:
                try:
                    check_page(resp.text, source)
                except FetchError as e:
                    error = str(e)
                else:
                    _write_atomic(html_path, resp.content)
                    now = _now()
                    result = FetchResult(
                        source_id=source.id,
                        url=source.url,
                        status="ok",
                        http_status=200,
                        attempts=attempt,
                        last_checked_at=now,
                        downloaded_at=now,
                        raw_sha256=hashlib.sha256(resp.content).hexdigest(),
                    )
                    _save_result(meta_path, result)
                    return result
            else:
                error = f"HTTP {http_status}"
        if attempt <= retries:
            sleep(backoff_s * 2 ** (attempt - 1))

    return _record_failure(source, raw_dir, http_status=http_status, attempts=attempt, error=error)


def _record_failure(
    source: Source, raw_dir: Path, *, http_status: int | None, attempts: int, error: str | None
) -> FetchResult:
    """Keep the last good copy (status `stale`) or report that there is none (`missing`)."""
    meta_path = raw_dir / f"{source.id}.meta.json"
    previous = _read_meta(meta_path)
    has_copy = (raw_dir / f"{source.id}.html").exists() and bool(previous.get("downloaded_at"))
    result = FetchResult(
        source_id=source.id,
        url=source.url,
        status="stale" if has_copy else "missing",
        http_status=http_status,
        attempts=attempts,
        last_checked_at=_now(),
        downloaded_at=previous.get("downloaded_at") if has_copy else None,
        raw_sha256=previous.get("raw_sha256") if has_copy else None,
        error=error,
    )
    _save_result(meta_path, result)
    return result


def register_manual_copy(source: Source, raw_dir: Path, saved_on: date) -> FetchResult:
    """Accept a browser-saved copy of the page; its save date becomes `downloaded_at`."""
    html_path = raw_dir / f"{source.id}.html"
    try:
        content = html_path.read_bytes()
    except OSError as e:
        raise FetchError(f"no saved page at {html_path}") from e
    check_page(content.decode("utf-8", errors="replace"), source)
    result = FetchResult(
        source_id=source.id,
        url=source.url,
        status="manual",
        http_status=None,
        attempts=0,
        last_checked_at=_now(),
        downloaded_at=datetime.combine(saved_on, dtime(), IST).isoformat(timespec="seconds"),
        raw_sha256=hashlib.sha256(content).hexdigest(),
    )
    _save_result(raw_dir / f"{source.id}.meta.json", result)
    log_event(logger, "fetch.manual_copy", source_id=source.id, saved_on=saved_on.isoformat())
    return result


def fetch_all(
    sources: list[Source],
    raw_dir: Path,
    *,
    only: str | None = None,
    transport: httpx.BaseTransport | None = None,
    delay_s: float = DELAY_BETWEEN_S,
    retries: int = RETRIES,
    backoff_s: float = BACKOFF_S,
    sleep: Callable[[float], None] = time.sleep,
) -> list[FetchResult]:
    selected = [s for s in sources if only is None or s.id == only]
    if not selected:
        raise ValueError(f"unknown source id: {only}")
    raw_dir.mkdir(parents=True, exist_ok=True)

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Language": "en-IN,en"}
    results: list[FetchResult] = []
    with httpx.Client(
        headers=headers, timeout=TIMEOUT, follow_redirects=False, transport=transport
    ) as client:
        allowed = robots_allows(client, [s.url for s in selected])
        for i, source in enumerate(selected):
            if not allowed[source.url]:
                result = _record_failure(
                    source,
                    raw_dir,
                    http_status=None,
                    attempts=0,
                    error="robots.txt disallows this URL",
                )
            else:
                if i:
                    sleep(delay_s)
                result = fetch_source(
                    client, source, raw_dir, retries=retries, backoff_s=backoff_s, sleep=sleep
                )
            log_event(
                logger,
                "fetch.result",
                level=logging.INFO if result.status == "ok" else logging.WARNING,
                source_id=result.source_id,
                status=result.status,
                http_status=result.http_status,
                attempts=result.attempts,
                error=result.error,
            )
            results.append(result)
    return results
