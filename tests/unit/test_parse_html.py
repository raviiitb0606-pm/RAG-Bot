"""P2.3: saved copies of the 5 pages -> expected fact groups and the values the page shows."""

import json
import re

import pytest

from conftest import CATALOG, page_html, source
from mf_faq.ingest.parse_html import ParseError, extract_server_data, facts_sha256, parse_page

GROUPS = [
    "category_risk",
    "nav",
    "fund_size",
    "expense_ratio",
    "minimum_investments",
    "exit_load",
    "stamp_duty",
    "tax_implication",
    "fund_management",
    "investment_objective",
    "benchmark",
    "holdings",
    "fund_house",
    "registrar",
]


def parse(source_id: str, html: str | None = None):
    return parse_page(html or page_html(source_id), source(source_id))


def text(page, group: str) -> str:
    return next(s.text for s in page.sections if s.group == group)


def with_data(source_id: str, edit) -> str:
    """The fixture page with its embedded JSON changed by `edit(data)`."""
    html = page_html(source_id)
    match = re.search(r'(<script id="__NEXT_DATA__"[^>]*>)(.*?)(</script>)', html, re.S)
    root = json.loads(match.group(2))
    edit(root["props"]["pageProps"]["mfServerSideData"])
    return html[: match.start(2)] + json.dumps(root) + html[match.end(2) :]


def test_every_fact_group_extracted(source_id):
    page = parse(source_id)
    groups = [s.group for s in page.sections]
    assert [g for g in groups if g != "custodian"] == GROUPS
    assert len(groups) == len(set(groups))
    assert page.has_performance_section


def test_sections_cover_available_facts(source_id):
    page = parse(source_id)
    found = {f for s in page.sections for f in s.facts}
    assert set(CATALOG.by_source(source_id).available_facts) <= found


def test_mid_cap_values_as_shown_on_page():
    page = parse("groww-hdfc-mid-cap")
    assert page.scheme_name == "HDFC Mid Cap Fund Direct Growth"
    assert text(page, "category_risk") == (
        "Category: Equity – Mid Cap.\nPlan: Direct – Growth.\nRisk level (riskometer): Very High."
    )
    assert text(page, "expense_ratio") == "Expense ratio: 0.74% (as of 21 Oct 2026)."
    assert text(page, "nav") == "Latest NAV: ₹213.42 (as of 23 Oct 2026)."
    assert text(page, "fund_size") == "Fund size (AUM): ₹1,01,856.06 Cr."
    assert text(page, "exit_load") == "Exit load: Exit load of 1% if redeemed within 1 year."
    assert "Minimum SIP amount: ₹100." in text(page, "minimum_investments")
    assert text(page, "benchmark") == "Fund benchmark: NIFTY Midcap 150 Total Return Index."
    assert text(page, "stamp_duty") == "Stamp duty on investment: 0.005% (from July 1st, 2020)."
    assert "returns are taxed at 20%" in text(page, "tax_implication")
    managers = text(page, "fund_management")
    assert managers.startswith(
        "Fund managers: Chirag Setalvad (since Jan 2013), Dhruv Muchhal (since Jun 2023)."
    )
    holdings = text(page, "holdings").split("\n")
    assert holdings[0] == "Portfolio holdings as of 30 Sep 2026 (81 holdings, % of fund assets):"
    assert len(holdings) == 82
    assert "The Federal Bank Ltd (Financial, Equity): 3.98%" in holdings
    assert "Custodian" not in [s.heading_path[0] for s in page.sections]  # page shows "--"


def test_custodian_only_where_shown():
    assert text(parse("groww-hdfc-large-cap"), "custodian") == "Custodian: HDFC Bank."
    assert text(parse("groww-hdfc-small-cap"), "custodian") == "Custodian: Citibank NA."


def test_silver_fof_values():
    page = parse("groww-hdfc-silver-etf-fof")
    assert "Commodities – Silver" in text(page, "category_risk")
    assert text(page, "exit_load") == "Exit load: Exit load of 1%, if redeemed within 15 days."
    assert "Income Tax slab" in text(page, "tax_implication")
    # PAR-13: the underlying ETF is kept as part of the FoF page.
    assert "HDFC Silver ETF Regular - Growth" in text(page, "holdings")
    # Only the current exit load, not the dated history shown on the page (PAR-08).
    assert "2% if redeemed within 6 months" not in "\n".join(s.text for s in page.sections)


def test_unicode_survives(source_id):
    # FET-11: ₹ and the en dash must not be garbled.
    page = parse(source_id)
    assert "₹" in text(page, "nav") and "–" in text(page, "category_risk")


def test_excluded_content_never_parsed(source_id):
    all_text = "\n".join(s.text for s in parse(source_id).sections)
    # "About" paragraph and fund house dates/AUM: the AMC's, not the scheme's.
    assert "10 Dec 1999" not in all_text
    assert "9,34,07" not in all_text
    assert "Asset Under Management" not in all_text
    # Other schemes from "Also manages these schemes" / peer table (PAR-06).
    assert "Retirement Fund" not in all_text and "Flexi Cap" not in all_text
    assert "Invesco" not in all_text and "Nippon India Growth" not in all_text
    # Ratings, returns, links (D-03, CR-2, R-2).
    assert "Rating" not in all_text and "annualised" not in all_text
    assert "http" not in all_text and "www." not in all_text and "@" not in all_text


def test_json_risk_and_stale_manager_field_ignored():
    # nfo_risk says "Moderately High"; the page shows "Very High" (P1.3).
    page = parse("groww-hdfc-large-cap")
    assert "Very High" in text(page, "category_risk")
    assert "Prashant Jain" not in text(page, "fund_management")


def test_facts_hash_stable_and_sensitive():
    page = parse("groww-hdfc-mid-cap")
    assert facts_sha256(page.sections) == facts_sha256(parse("groww-hdfc-mid-cap").sections)
    html = page_html("groww-hdfc-mid-cap").replace("₹213.42", "₹214.00")
    assert facts_sha256(parse("groww-hdfc-mid-cap", html).sections) != facts_sha256(page.sections)


def test_ads_and_scripts_dont_move_hash():
    # FET-08: markup outside the facts changes, facts hash doesn't.
    html = page_html("groww-hdfc-mid-cap")
    noisy = html.replace("</body>", "<script>var ad=1</script><div>Download the App</div></body>")
    assert facts_sha256(parse("groww-hdfc-mid-cap", noisy).sections) == facts_sha256(
        parse("groww-hdfc-mid-cap").sections
    )


def test_block_page_rejected():
    # FET-05: HTTP 200 but a bot check.
    with pytest.raises(ParseError):
        parse("groww-hdfc-mid-cap", "<html><body>Please enable JavaScript</body></html>")


def test_block_page_with_json_but_no_facts_rejected():
    html = page_html("groww-hdfc-mid-cap")
    body = html.index("<body")
    script = re.search(r'<script id="__NEXT_DATA__".*?</script>', html, re.S).group(0)
    with pytest.raises(ParseError):
        parse("groww-hdfc-mid-cap", html[:body] + f"<body><p>Captcha</p>{script}</body></html>")


def test_missing_fact_group_rejected():
    # PAR-01: layout change removes a label.
    html = page_html("groww-hdfc-mid-cap").replace(">Fund benchmark<", ">Benchmark index<")
    with pytest.raises(ParseError, match="Fund benchmark"):
        parse("groww-hdfc-mid-cap", html)


def test_wrong_scheme_rejected():
    with pytest.raises(ParseError, match="expected"):
        parse_page(page_html("groww-hdfc-mid-cap"), source("groww-hdfc-small-cap"))


def test_regular_plan_rejected():
    html = with_data("groww-hdfc-mid-cap", lambda d: d.update(plan_type="Regular"))
    with pytest.raises(ParseError, match="PAR-07"):
        parse("groww-hdfc-mid-cap", html)


def test_stale_manager_json_rejected():
    def edit(d):
        d["fund_manager_details"][0]["date_from"] = "2019-03-31T18:30:00.000Z"

    with pytest.raises(ParseError, match="Chirag Setalvad"):
        parse("groww-hdfc-mid-cap", with_data("groww-hdfc-mid-cap", edit))


def test_holdings_count_mismatch_rejected():
    html = with_data("groww-hdfc-mid-cap", lambda d: d["holdings"].pop())
    with pytest.raises(ParseError, match="holdings"):
        parse("groww-hdfc-mid-cap", html)


def test_performance_json_never_reaches_parser():
    data = extract_server_data(page_html("groww-hdfc-mid-cap"))
    assert data["return_stats"]  # present on the page...
    all_text = "\n".join(s.text for s in parse("groww-hdfc-mid-cap").sections)
    assert "15.26" not in all_text and "-3.17" not in all_text  # ...but never parsed
