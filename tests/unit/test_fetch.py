import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from mf_faq.ingest.fetch import (
    USER_AGENT,
    FetchError,
    check_page,
    fetch_all,
    read_meta,
    register_manual_copy,
    write_meta,
)
from mf_faq.ingest.registry import load_sources

ROOT = Path(__file__).resolve().parents[2]
SOURCES = load_sources(ROOT / "corpus" / "sources.yaml")
MID = next(s for s in SOURCES if s.id == "groww-hdfc-mid-cap")
ROBOTS_OK = "User-agent: *\nDisallow: /mutual-funds/compare/*\nDisallow: /user/\n"


def page(slug: str) -> str:
    data = {"props": {"pageProps": {"mfServerSideData": {"search_id": slug}}}}
    script = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
    return f"<html>{script}</html>"


class Groww:
    """Mock transport: robots.txt plus scripted responses per page path."""

    def __init__(self, robots=(200, ROBOTS_OK), script=None):
        self.robots = robots
        self.script = script or {}  # path -> list of responses, last one repeats
        self.page_requests: list[str] = []
        self.user_agents: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.user_agents.add(request.headers["user-agent"])
        if request.url.path == "/robots.txt":
            return httpx.Response(self.robots[0], text=self.robots[1])
        self.page_requests.append(request.url.path)
        slug = request.url.path.rsplit("/", 1)[-1]
        queue = self.script.get(request.url.path)
        if queue:
            r = queue.pop(0) if len(queue) > 1 else queue[0]
            return httpx.Response(r.status_code, headers=r.headers, content=r.content)  # fresh copy
        return httpx.Response(200, text=page(slug))


def run(tmp_path, groww, **kw):
    sleeps: list[float] = []
    results = fetch_all(
        SOURCES, tmp_path, transport=httpx.MockTransport(groww), sleep=sleeps.append, **kw
    )
    return results, sleeps


MID_PATH = "/mutual-funds/hdfc-mid-cap-fund-direct-growth"


def test_all_five_fetched_and_saved(tmp_path):
    groww = Groww()
    results, sleeps = run(tmp_path, groww)
    assert [r.status for r in results] == ["ok"] * 5
    assert len(groww.page_requests) == 5
    assert groww.user_agents == {USER_AGENT}
    assert sleeps == [3.0] * 4  # polite delay between pages only
    for s in SOURCES:
        assert (tmp_path / f"{s.id}.html").read_text().startswith("<html>")
        meta = json.loads((tmp_path / f"{s.id}.meta.json").read_text())
        assert meta["status"] == "ok" and meta["http_status"] == 200 and meta["raw_sha256"]


def test_only_one_source(tmp_path):
    groww = Groww()
    results, _ = run(tmp_path, groww, only=MID.id)
    assert [r.source_id for r in results] == [MID.id]
    assert groww.page_requests == [MID_PATH]


def test_unknown_source_rejected(tmp_path):
    with pytest.raises(ValueError):
        run(tmp_path, Groww(), only="groww-hdfc-flexi-cap")


def test_transient_404_is_retried(tmp_path):
    groww = Groww(
        script={MID_PATH: [httpx.Response(404), httpx.Response(200, text=page(MID.slug))]}
    )
    results, sleeps = run(tmp_path, groww, only=MID.id)
    assert results[0].status == "ok" and results[0].attempts == 2
    assert sleeps == [2.0]  # backoff before the retry


def test_persistent_failure_keeps_previous_copy_as_stale(tmp_path):
    run(tmp_path, Groww(), only=MID.id)
    before = (tmp_path / f"{MID.id}.html").read_bytes()
    first = json.loads((tmp_path / f"{MID.id}.meta.json").read_text())

    groww = Groww(script={MID_PATH: [httpx.Response(503)]})
    results, sleeps = run(tmp_path, groww, only=MID.id)
    r = results[0]
    assert r.status == "stale" and r.http_status == 503 and r.attempts == 4  # 1 + 3 retries
    assert sleeps == [2.0, 4.0, 8.0]
    assert r.downloaded_at == first["downloaded_at"]
    assert (tmp_path / f"{MID.id}.html").read_bytes() == before


def test_failure_without_copy_is_missing(tmp_path):
    results, _ = run(tmp_path, Groww(script={MID_PATH: [httpx.Response(500)]}), only=MID.id)
    assert results[0].status == "missing"
    assert not (tmp_path / f"{MID.id}.html").exists()


def test_network_error_is_retried(tmp_path):
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OK)
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text=page(MID.slug))

    results = fetch_all(
        SOURCES, tmp_path, only=MID.id, transport=httpx.MockTransport(handler), sleep=lambda s: None
    )
    assert results[0].status == "ok" and results[0].attempts == 2


def test_redirect_not_followed_or_retried(tmp_path):
    target = "https://groww.in/mutual-funds/hdfc-mid-cap-fund-renamed-direct-growth"
    groww = Groww(script={MID_PATH: [httpx.Response(301, headers={"location": target})]})
    results, _ = run(tmp_path, groww, only=MID.id)
    r = results[0]
    assert r.status == "missing" and r.attempts == 1
    assert "redirected" in r.error and "outside ALLOWED_URLS" in r.error
    assert groww.page_requests == [MID_PATH]


def test_wrong_scheme_page_not_saved(tmp_path):
    wrong = httpx.Response(200, text=page("hdfc-flexi-cap-fund-direct-growth"))
    results, _ = run(tmp_path, Groww(script={MID_PATH: [wrong]}), only=MID.id)
    assert results[0].status == "missing" and "expected" in results[0].error
    assert not (tmp_path / f"{MID.id}.html").exists()


def test_robots_disallow_means_no_page_request(tmp_path):
    groww = Groww(robots=(200, "User-agent: *\nDisallow: /mutual-funds/\n"))
    results, _ = run(tmp_path, groww)
    assert {r.status for r in results} == {"missing"}
    assert groww.page_requests == []


def test_robots_unavailable_means_no_page_request(tmp_path):
    groww = Groww(robots=(503, ""))
    results, _ = run(tmp_path, groww)
    assert {r.status for r in results} == {"missing"}
    assert groww.page_requests == []


@pytest.mark.parametrize(
    "html",
    [
        "<html>Access denied</html>",
        '<script id="__NEXT_DATA__">{"props": {}}</script>',
        '<script id="__NEXT_DATA__">not json</script>',
    ],
)
def test_check_page_rejects_non_scheme_pages(html):
    with pytest.raises(FetchError):
        check_page(html, MID)


def test_refetch_keeps_build_fields(tmp_path):
    # fetched_at / content_sha256 belong to the build (P2.2); fetching must not drop them.
    run(tmp_path, Groww(), only=MID.id)
    write_meta(
        tmp_path, MID.id, read_meta(tmp_path, MID.id) | {"fetched_at": "X", "content_sha256": "H"}
    )
    run(tmp_path, Groww(), only=MID.id)
    run(tmp_path, Groww(script={MID_PATH: [httpx.Response(503)]}), only=MID.id)
    meta = read_meta(tmp_path, MID.id)
    assert meta["status"] == "stale"
    assert (meta["fetched_at"], meta["content_sha256"]) == ("X", "H")


def test_manual_copy_records_save_date(tmp_path):
    (tmp_path / f"{MID.id}.html").write_text(page(MID.slug))
    result = register_manual_copy(MID, tmp_path, date(2026, 10, 20))
    assert result.status == "manual" and result.http_status is None
    meta = read_meta(tmp_path, MID.id)
    assert meta["downloaded_at"] == "2026-10-20T00:00:00+05:30"
    assert meta["raw_sha256"]


def test_manual_copy_must_be_the_right_page(tmp_path):
    with pytest.raises(FetchError, match="no saved page"):
        register_manual_copy(MID, tmp_path, date(2026, 10, 20))
    (tmp_path / f"{MID.id}.html").write_text(page("hdfc-flexi-cap-fund-direct-growth"))
    with pytest.raises(FetchError, match="expected"):
        register_manual_copy(MID, tmp_path, date(2026, 10, 20))
    assert read_meta(tmp_path, MID.id) == {}
