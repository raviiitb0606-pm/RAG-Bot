"""P2.4: return figures are removed or caught; exit load, expense ratio and tax rule survive."""

import pytest

from conftest import page_html, source
from mf_faq.ingest.parse_html import Section, extract_server_data, parse_page
from mf_faq.ingest.strip_performance import (
    PERFORMANCE_JSON_KEYS,
    find_performance_patterns,
    strip_performance_json,
    strip_performance_sections,
)

RETURN_TEXT = [
    "+15.26% 3Y annualised",
    "3Y annualised",
    "The fund has a 5-year CAGR of 16.5%.",
    "It delivered 25% in 3 years.",  # PAR-03
    "with three year return of 15.26",  # the page's meta description
    "Fund returns +15.3% +16.5%",
    "Category average (Equity Mid Cap) +13.6%",
    "Rank (Equity Mid Cap) 12",
    "Monthly investment ₹5,000 would've become ₹15,20,182",
    "Its Groww rating is 4.",
    "Returns of 21.4% over the last 7 years",
    "XIRR 14%",
    "Absolute returns",
]

FACT_TEXT = [
    "Exit load: Exit load of 1% if redeemed within 1 year.",  # PAR-04
    "Exit load: Exit load of 1%, if redeemed within 15 days.",
    "Exit load of 2% if redeemed within 6 months and 1% if redeemed after 6 months",
    "Expense ratio: 0.74% (as of 21 Oct 2026).",
    # The page's tax rule (Phase 1 carry-over): "returns … 20%" must pass.
    "Tax implication: If you redeem within one year, returns are taxed at 20%. If you redeem "
    "after one year, returns exceeding Rs 1.25 lakh in a financial year are taxed at 12.5%.",
    "Tax implication: If you redeem within two years, returns are taxed as per your Income Tax "
    "slab. If you redeem after two years, returns are taxed at 12.5%.",
    "Stamp duty on investment: 0.005% (from July 1st, 2020).",
    "Net Payables (Unspecified, Net Payables): -0.08%",
    "3M India Ltd (Industrials, Equity): 0.45%",
    "Fund benchmark: NIFTY Midcap 150 Total Return Index.",
    "Fund benchmark: Nifty 500 Multicap 50:25:25 Total Return Index.",
    "Latest NAV: ₹213.42 (as of 23 Oct 2026).",
    "HDFC Bank Ltd (Financial, Equity): 7.36%",
]


@pytest.mark.parametrize("text", RETURN_TEXT)
def test_return_patterns_found(text):
    assert find_performance_patterns(text), text


@pytest.mark.parametrize("text", FACT_TEXT)
def test_facts_not_flagged(text):
    assert find_performance_patterns(text) == [], text


def test_json_performance_keys_removed():
    data = extract_server_data(page_html("groww-hdfc-mid-cap"))
    stripped = strip_performance_json(data)
    assert not PERFORMANCE_JSON_KEYS & stripped.keys()
    for key in ("stats", "return_stats", "sip_return", "peerComparison", "analysis"):
        assert key in data and key not in stripped
    assert stripped["exit_load"] == data["exit_load"]


def test_sentence_with_return_figure_dropped_rest_kept():
    section = Section(
        "investment_objective",
        ("Investment objective",),
        "Investment objective: Long-term capital appreciation. It delivered 25% in 3 years.",
        ("investment_objective",),
    )
    [kept] = strip_performance_sections([section])
    assert kept.text == "Investment objective: Long-term capital appreciation."


def test_section_left_empty_is_dropped():
    sections = [
        Section("x", ("X",), "Fund returns +15.3% +16.5%", ()),
        Section("exit_load", ("Exit load",), "Exit load: Exit load of 1% if redeemed.", ()),
    ]
    assert [s.group for s in strip_performance_sections(sections)] == ["exit_load"]


def test_real_pages_have_nothing_to_strip(source_id):
    page = parse_page(page_html(source_id), source(source_id))
    assert strip_performance_sections(page.sections) == page.sections
