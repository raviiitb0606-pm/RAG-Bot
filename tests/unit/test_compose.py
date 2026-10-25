"""P4.3 / P4.7: templates, link rule, composer and source registry."""

import json
import re
from datetime import datetime

import pytest
from test_validate import CHUNK

from conftest import CATALOG, SOURCES
from mf_faq.ingest.parse_html import IST
from mf_faq.pipeline.compose import FOOTER_RE, URL_RE, ComposeError, Composer, Reply, _check
from mf_faq.pipeline.retrieve import NotFound, Status
from mf_faq.pipeline.router import Intent
from mf_faq.pipeline.sources import RegistryLoadError, SourceEntry, SourceRegistry
from mf_faq.pipeline.templates import (
    OUT_OF_SCOPE,
    TRANSACTION,
    Kind,
    Templates,
    join_names,
    kind_for_evidence,
    kind_for_intent,
    pick_source,
)

WHEN = datetime(2026, 10, 25, 1, 0, tzinfo=IST)


def entries(**over):
    return [SourceEntry(s.id, s.url, s.title, s.scheme_id, WHEN, over.get(s.id)) for s in SOURCES]


REGISTRY = SourceRegistry(entries())
DEFAULT = SOURCES[0].id
COMPOSER = Composer(REGISTRY, CATALOG, DEFAULT)
ALL_IDS = [s.scheme_id for s in SOURCES]

TEMPLATE_KINDS = [
    (Kind.REFUSAL, None),
    (Kind.REFUSAL, TRANSACTION),
    (Kind.PERFORMANCE, None),
    (Kind.PII, None),
    (Kind.NOT_FOUND, None),
    (Kind.NOT_FOUND, OUT_OF_SCOPE),
    (Kind.MULTI_SCHEME, None),
    (Kind.CLARIFY, None),
    (Kind.HELP, None),
    (Kind.ERROR, None),
    (Kind.ERROR, "busy"),
]


@pytest.mark.parametrize("kind,variant", TEMPLATE_KINDS)
@pytest.mark.parametrize("scheme", [None, ALL_IDS[1]])
def test_every_template_meets_r1_r3(kind, variant, scheme):
    reply = COMPOSER.template(
        kind, variant=variant, scheme_ids=[scheme] if scheme else [], active_scheme_id=None
    )
    assert URL_RE.findall(reply.text) == [reply.source_url]
    assert reply.source_url in {s.url for s in SOURCES}
    assert FOOTER_RE.match(reply.text.splitlines()[-1])
    assert reply.text.splitlines()[-1] == "Last updated from sources: 25 Oct 2026"
    assert reply.to_dict()["kind"] == kind.value


def test_link_follows_scheme_then_active_then_default():
    by_scheme = {s.scheme_id: s for s in SOURCES}
    r = COMPOSER.template(Kind.REFUSAL, scheme_ids=[ALL_IDS[2]], active_scheme_id=ALL_IDS[3])
    assert r.source_url == by_scheme[ALL_IDS[2]].url
    r = COMPOSER.template(Kind.REFUSAL, active_scheme_id=ALL_IDS[3])
    assert r.source_url == by_scheme[ALL_IDS[3]].url
    assert COMPOSER.template(Kind.REFUSAL).source_id == DEFAULT
    assert COMPOSER.template(Kind.REFUSAL, scheme_ids=["bogus"]).source_id == DEFAULT
    r = COMPOSER.template(Kind.NOT_FOUND, best_source_id=SOURCES[4].id)
    assert r.source_id == SOURCES[4].id


def test_pick_source_ignores_unknown_best():
    s = pick_source(REGISTRY, default_source_id=DEFAULT, best_source_id="nope")
    assert s.id == DEFAULT


def test_multi_scheme_offers_named_schemes():
    body = COMPOSER.template(Kind.MULTI_SCHEME, scheme_ids=ALL_IDS[:2]).answer
    names = {s.scheme_id: s.short_name for s in CATALOG.schemes}
    assert names[ALL_IDS[0]] in body and names[ALL_IDS[1]] in body
    assert names[ALL_IDS[4]] not in body


def test_answer_reply_links_cited_page():
    reply = COMPOSER.answer(" The exit load is 1%. ", CHUNK)
    assert reply.kind is Kind.ANSWER and reply.source_id == CHUNK.source_id
    assert reply.text.startswith("The exit load is 1%.\n\nSource: ")


def test_answer_body_is_not_a_template():
    with pytest.raises(ValueError):
        Templates(CATALOG).body(Kind.ANSWER)
    with pytest.raises(ValueError):
        Templates(CATALOG).body(Kind.PII, variant="x")


def test_composer_checks_its_output():
    with pytest.raises(ComposeError):
        COMPOSER.answer("One. Two. Three. Four.", CHUNK)
    with pytest.raises(ComposeError):
        COMPOSER.answer("   ", CHUNK)
    good = COMPOSER.answer("Fine.", CHUNK)
    for bad in (
        Reply(
            Kind.ANSWER,
            "x",
            "i",
            "https://evil.example/x",
            "t",
            "25 Oct 2026",
            "x\n\nSource: https://evil.example/x\nLast updated from sources: 25 Oct 2026",
        ),
        Reply(
            Kind.ANSWER,
            "x",
            "i",
            good.source_url,
            "t",
            "d",
            f"x\n\nSource: {good.source_url}\nupdated",
        ),
    ):
        with pytest.raises(ComposeError):
            _check(bad)


def test_default_source_must_exist():
    with pytest.raises(ValueError):
        Composer(REGISTRY, CATALOG, "nope")


def test_kind_mappings():
    assert kind_for_intent(Intent.FACTUAL) is None
    assert kind_for_intent(Intent.ADVISORY) == (Kind.REFUSAL, None)
    assert kind_for_intent(Intent.TRANSACTION) == (Kind.REFUSAL, TRANSACTION)
    assert kind_for_intent(Intent.GREETING) == (Kind.HELP, None)
    assert kind_for_evidence(Status.OK, None) is None
    assert kind_for_evidence(Status.NEEDS_SCHEME, None) == (Kind.CLARIFY, None)
    assert kind_for_evidence(Status.MULTI_SCHEME, None) == (Kind.MULTI_SCHEME, None)
    assert kind_for_evidence(Status.INSUFFICIENT, NotFound.OUT_OF_SCOPE) == (
        Kind.NOT_FOUND,
        OUT_OF_SCOPE,
    )
    assert kind_for_evidence(Status.INSUFFICIENT, NotFound.NO_SCHEME) == (Kind.NOT_FOUND, None)


def test_join_names():
    assert join_names([]) == "" and join_names(["a"]) == "a"
    assert join_names(["a", "b"]) == "a and b"
    assert join_names(["a", "b", "c"]) == "a, b and c"


# --- source registry ------------------------------------------------------------------------


def test_last_updated_is_ist_date():
    utc = datetime.fromisoformat("2026-10-24T20:00:00+00:00")  # 25 Oct 01:30 IST
    e = SourceEntry("i", "u", "t", "s", utc)
    assert e.last_updated == "25 Oct 2026"


def test_registry_lookup():
    assert DEFAULT in REGISTRY and "x" not in REGISTRY
    assert len(REGISTRY.urls) == 5
    assert REGISTRY.for_scheme(ALL_IDS[0]).id == SOURCES[0].id


def test_registry_rejects_bad_sets():
    with pytest.raises(RegistryLoadError):
        SourceRegistry(entries()[:4])
    bad = entries()
    bad[0] = SourceEntry("a", "https://groww.in/other", "t", "s", WHEN)
    with pytest.raises(RegistryLoadError):
        SourceRegistry(bad)


def test_registry_from_manifest(tmp_path):
    def row(s):
        return {
            "id": s.id,
            "url": s.url,
            "title": s.title,
            "scheme_id": s.scheme_id,
            "fetched_at": "2026-10-25T00:00:00+05:30",
            "status": "stale",
        }

    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"sources": [row(s) for s in SOURCES]}))
    reg = SourceRegistry.from_manifest(p)
    assert reg.get(DEFAULT).status == "stale"

    naive = [row(s) | {"fetched_at": "2026-10-25T00:00:00"} for s in SOURCES]
    p.write_text(json.dumps({"sources": naive}))
    with pytest.raises(RegistryLoadError, match="time zone"):
        SourceRegistry.from_manifest(p)

    p.write_text("not json")
    with pytest.raises(RegistryLoadError):
        SourceRegistry.from_manifest(p)
    with pytest.raises(RegistryLoadError):
        SourceRegistry.from_manifest(tmp_path / "missing.json")


def test_footer_regex():
    assert re.match(FOOTER_RE, "Last updated from sources: 05 Jan 2026")
