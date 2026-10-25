"""Phase 1 exit criteria: sources.yaml, schemes.yaml and the golden seed agree with each other."""

from collections import Counter
from pathlib import Path

import pytest
import yaml

from conftest import CATALOG, SOURCES, page_html
from mf_faq.ingest.build import process_html
from mf_faq.ingest.registry import ALLOWED_URLS, is_allowed_url

ROOT = Path(__file__).resolve().parents[2]
KINDS = {"answer", "refusal", "performance", "pii", "not_found", "multi_scheme", "help"}


def _load(rel: str):
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sources():
    return _load("corpus/sources.yaml")


@pytest.fixture(scope="module")
def schemes():
    return _load("corpus/schemes.yaml")


@pytest.fixture(scope="module")
def golden():
    return _load("tests/eval/golden.yaml")


def _cases(golden):
    """Flatten thread turns into single cases."""
    for case in golden:
        yield from case.get("turns", [case])


# --- sources.yaml ---------------------------------------------------------------------------


def test_sources_exactly_five_one_per_scheme(sources):
    assert len(sources) == 5
    for key in ("id", "url", "scheme_id"):
        assert len({s[key] for s in sources}) == 5, f"duplicate {key}"
    assert {s["url"] for s in sources} == ALLOWED_URLS


def test_sources_fields(sources):
    for s in sources:
        assert is_allowed_url(s["url"])
        assert s["publisher"] == "GROWW"
        assert s["doc_type"] == "scheme_page"
        assert s["id"] == f"groww-{s['scheme_id']}"
        assert s["title"].endswith("(Groww)")
        assert s["notes"].startswith("rendering:"), "P1.3 rendering method must be recorded"


# --- schemes.yaml ---------------------------------------------------------------------------


def test_schemes_match_sources(sources, schemes):
    by_scheme = {s["scheme_id"]: s["id"] for s in sources}
    assert {s["scheme_id"]: s["source_id"] for s in schemes["schemes"]} == by_scheme


def test_schemes_fields(schemes):
    labels = schemes["fact_labels"]
    for s in schemes["schemes"]:
        assert s["plan"] == "Direct – Growth"
        assert s["name"].endswith("Direct Growth")
        assert s["category"] and s["short_name"] and s["aliases"]
        assert s["available_facts"], f"{s['scheme_id']}: P1.4 available_facts missing"
        unknown = set(s["available_facts"]) - set(labels)
        assert not unknown, f"{s['scheme_id']}: facts without a label: {unknown}"
        assert len(set(s["available_facts"])) == len(s["available_facts"])


def test_aliases_unique_across_schemes(schemes):
    counts = Counter(a.lower() for s in schemes["schemes"] for a in s["aliases"])
    assert not [a for a, n in counts.items() if n > 1]


def test_not_in_scope_never_names_a_supported_scheme(schemes):
    supported = {s["short_name"].lower() for s in schemes["schemes"]}
    supported |= {a.lower() for s in schemes["schemes"] for a in s["aliases"]}
    deny = [n.lower() for n in schemes["not_in_scope"]]
    assert len(deny) == len(set(deny))
    assert not supported & set(deny)


# --- golden.yaml seed (P1.6) ----------------------------------------------------------------


def test_golden_ids_unique(golden):
    ids = [c["id"] for c in golden]
    assert len(ids) == len(set(ids))


def test_golden_kinds_and_sources_valid(golden, sources):
    source_ids = {s["id"] for s in sources}
    for c in _cases(golden):
        assert c["expect_kind"] in KINDS, c
        if "expect_source" in c:
            assert c["expect_source"] in source_ids, c


def test_golden_answer_cases_ask_only_available_facts(golden, schemes):
    facts = {s["source_id"]: set(s["available_facts"]) for s in schemes["schemes"]}
    for c in _cases(golden):
        if c["expect_kind"] == "answer":
            assert c["expect_source"] and c["fact"], c
            assert c["fact"] in facts[c["expect_source"]], c


def test_golden_three_to_five_factual_per_scheme(golden, sources):
    """3-5 per scheme (P1.6), plus exactly one holdings case each (P3.5)."""
    cases = [c for c in golden if "q" in c and c["expect_kind"] == "answer"]
    per_source = Counter(c["expect_source"] for c in cases if c["fact"] != "holdings")
    holdings = Counter(c["expect_source"] for c in cases if c["fact"] == "holdings")
    for s in sources:
        assert 3 <= per_source[s["id"]] <= 5, (s["id"], per_source[s["id"]])
        assert holdings[s["id"]] == 1, s["id"]


def test_golden_covers_every_required_class(golden):
    kinds = Counter(c["expect_kind"] for c in _cases(golden))
    for kind in ("refusal", "performance", "pii", "multi_scheme", "not_found"):
        assert kinds[kind] >= 2, kind
    assert any("turns" in c for c in golden), "follow-up cases missing"
    questions = " ".join(c["q"].lower() for c in _cases(golden))
    for topic in ("elss", "statement", "flexi cap", "sbi"):  # out-of-corpus musts (P1.6)
        assert topic in questions, topic


# --- golden.yaml expected values (P2.9) -----------------------------------------------------


def test_golden_answer_values_are_on_the_page(golden):
    """Every `must_contain` value is in a chunk of the expected page that holds that fact."""
    chunks = {s.id: process_html(page_html(s.id), s, CATALOG)[1] for s in SOURCES}
    for c in _cases(golden):
        if c["expect_kind"] != "answer":
            continue
        assert c.get("must_contain"), f"{c['q']!r}: must_contain missing (P2.9)"
        texts = [ch.content for ch in chunks[c["expect_source"]] if c["fact"] in ch.facts]
        for value in c["must_contain"]:
            assert any(value in t for t in texts), (c["q"], value)
