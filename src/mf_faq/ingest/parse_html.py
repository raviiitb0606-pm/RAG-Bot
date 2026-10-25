"""Groww scheme page -> one `Section(heading_path, text)` per fact group (P2.3, arch §5.3).

The **visible page is the source of truth** (Phase 1 finding: some embedded-JSON fields are
stale or wrong). Scalar facts are read from the visible text by their on-page label, and each
value must have the expected shape, so the glossary ("Expense ratio — A fee payable …") or the
dated exit-load history can never be picked up by mistake.

The embedded `__NEXT_DATA__` JSON is used only for the two list-like groups that are tedious to
read from text, and only after its performance keys are removed (P2.4):

- fund managers: `fund_manager_details` (not the stale top-level `fund_manager`); every name
  and "since" month must match the visible "Fund management" block;
- holdings: `holdings`; the row count must match the visible "Holdings (N)".

Never parsed: the auto-generated "About" paragraph (it gives the AMC's 1999 date and total AUM
as the scheme's), "Also manages these schemes" (other schemes must not enter the corpus,
PAR-06), the fund house's rank, total AUM, incorporation and launch dates (the AMC's, easily
mistaken for the scheme's), and websites / e-mails (replies carry exactly one link, R-2, and
the e-mails are obfuscated on the visible page).

Any missing label or mismatch raises `ParseError`: a layout change (PAR-01) or a block page
(FET-05) fails the build instead of producing empty or wrong data.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from bs4 import BeautifulSoup

from mf_faq.ingest.registry import Source
from mf_faq.ingest.strip_performance import has_performance_section, strip_performance_json

PARSER_VERSION = "1"
IST = timezone(timedelta(hours=5, minutes=30), "IST")
RISK_LEVELS = ("Low", "Low to Moderate", "Moderate", "Moderately High", "High", "Very High")

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_RUPEES = r"₹[\d,]+(?:\.\d+)?"
_PERCENT = r"\d+(?:\.\d+)?%"
_NAV_DATE_RE = re.compile(r"NAV: (\d{2} [A-Z][a-z]{2} '\d{2})")
_DASH = " – "  # en dash, as the page and schemes.yaml write categories


class ParseError(Exception):
    """The page doesn't have the expected structure (layout change, block page, wrong plan)."""


@dataclass(frozen=True)
class Section:
    group: str  # stable key, e.g. "exit_load"; becomes part of the chunk id
    heading_path: tuple[str, ...]
    text: str
    facts: tuple[str, ...]  # available_facts keys (schemes.yaml) this section answers


@dataclass(frozen=True)
class ParsedPage:
    source_id: str
    scheme_name: str
    sections: list[Section]
    has_performance_section: bool


def facts_sha256(sections: list[Section]) -> str:
    """Hash of the extracted facts; ads, scripts or markup changes don't move it (FET-08)."""
    payload = json.dumps([asdict(s) for s in sections], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extract_server_data(html: str) -> dict[str, Any]:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        raise ParseError("no __NEXT_DATA__ block (layout change or block page?)")
    try:
        data = json.loads(match.group(1))["props"]["pageProps"]["mfServerSideData"]
    except (ValueError, KeyError, TypeError) as e:
        raise ParseError("embedded JSON has no mfServerSideData") from e
    if not isinstance(data, dict):
        raise ParseError("mfServerSideData is not an object")
    return data


def visible_lines(html: str) -> list[str]:
    """The page's visible text, one whitespace-normalised line per text node."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    text = (soup.body or soup).get_text("\n")
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


class _Page:
    """Label lookups over the visible lines. Every miss is a ParseError."""

    def __init__(self, lines: list[str]):
        self.lines = lines

    def find(self, label: str, start: int = 0, end: int | None = None) -> int:
        try:
            return self.lines.index(label, start, len(self.lines) if end is None else end)
        except ValueError:
            raise ParseError(f"label {label!r} not found") from None

    def value(self, label: str, start: int = 0, pattern: str = r".+", end: int | None = None):
        """(index, value) of the first `label` after `start` whose next line matches `pattern`."""
        i = start
        while True:
            i = self.find(label, i, end)
            if i + 1 < len(self.lines) and re.fullmatch(pattern, self.lines[i + 1]):
                return i, self.lines[i + 1]
            i += 1


def _ist(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return (dt if dt.tzinfo else dt.replace(tzinfo=IST)).astimezone(IST)


def _day(dt: datetime) -> str:
    return dt.strftime("%d %b %Y")


def _sentence(text: str) -> str:
    text = text.strip().rstrip(" ,")
    return text if text.endswith((".", "!", "?")) else text + "."


def parse_page(html: str, source: Source) -> ParsedPage:
    raw = extract_server_data(html)
    data = strip_performance_json(raw)
    lines = visible_lines(html)
    page = _Page(lines)

    name = data.get("scheme_name")
    if data.get("search_id") != source.slug:
        raise ParseError(f"page is for {data.get('search_id')!r}, expected {source.slug!r}")
    if (data.get("plan_type"), data.get("scheme_type")) != ("Direct", "Growth"):
        raise ParseError("not the Direct – Growth plan (PAR-07)")
    if not isinstance(name, str):
        raise ParseError("scheme_name missing")
    h1 = page.find(name)  # FET-05: a block page has no scheme heading

    sections = [
        _category_risk(page, h1),
        *_summary_strip(page, h1, data),
        _minimum_investments(page, h1),
        *_exit_load_tax(page, h1),
        _fund_management(page, h1, data),
        *_objective_benchmark(page, h1),
        _holdings(page, h1, data),
        *_fund_house_registrar(page, h1),
    ]
    return ParsedPage(
        source_id=source.id,
        scheme_name=name,
        sections=sections,
        has_performance_section=has_performance_section(raw, lines),
    )


def _category_risk(page: _Page, h1: int) -> Section:
    # Header right under the H1: category, sub-category, then "<level> Risk".
    parts = []
    for line in page.lines[h1 + 1 : h1 + 6]:
        if line.endswith(" Risk"):
            risk = line.removesuffix(" Risk")
            break
        parts.append(line)
    else:
        raise ParseError("risk level not found under the scheme heading")
    if risk not in RISK_LEVELS or not parts:
        raise ParseError(f"unexpected header: {parts!r} / {risk!r}")
    text = (
        f"Category: {_DASH.join(parts)}.\nPlan: Direct – Growth.\nRisk level (riskometer): {risk}."
    )
    return Section("category_risk", ("Category and risk",), text, ("category", "risk_level"))


def _summary_strip(page: _Page, h1: int, data: dict[str, Any]) -> list[Section]:
    nav_i = next(
        (i for i in range(h1, len(page.lines)) if _NAV_DATE_RE.fullmatch(page.lines[i])), None
    )
    if nav_i is None:
        raise ParseError("NAV date not found")
    nav_date = datetime.strptime(_NAV_DATE_RE.fullmatch(page.lines[nav_i]).group(1), "%d %b '%y")
    nav = page.lines[nav_i + 1]
    if not re.fullmatch(_RUPEES, nav):
        raise ParseError(f"unexpected NAV value {nav!r}")
    _, aum = page.value("Fund size (AUM)", h1, rf"{_RUPEES} Cr")
    _, expense = page.value("Expense ratio", h1, _PERCENT)

    # As-of date of the expense ratio: the latest JSON record with the value the page shows.
    dates = [
        _ist(r["as_on_date"])
        for r in data.get("historic_fund_expense") or []
        if r.get("as_on_date") and f"{float(r.get('expense_ratio', -1)):.2f}%" == expense
    ]
    as_of = f" (as of {_day(max(dates))})" if dates else ""
    return [
        Section("nav", ("NAV",), f"Latest NAV: {nav} (as of {_day(nav_date)}).", ("nav",)),
        Section("fund_size", ("Fund size (AUM)",), f"Fund size (AUM): {aum}.", ("fund_size",)),
        Section(
            "expense_ratio",
            ("Expense ratio",),
            f"Expense ratio: {expense}{as_of}.",
            ("expense_ratio",),
        ),
    ]


def _minimum_investments(page: _Page, h1: int) -> Section:
    block = page.find("Minimum investments", h1)
    _, first = page.value("Min. for 1st investment", block, _RUPEES)
    _, second = page.value("Min. for 2nd investment", block, _RUPEES)
    _, sip = page.value("Min. for SIP", block, _RUPEES)
    text = (
        f"Minimum first investment (lump sum): {first}.\n"
        f"Minimum second (additional) investment: {second}.\n"
        f"Minimum SIP amount: {sip}."
    )
    return Section(
        "minimum_investments",
        ("Minimum investments",),
        text,
        ("min_lumpsum", "min_additional", "min_sip"),
    )


def _exit_load_tax(page: _Page, h1: int) -> list[Section]:
    heading = "Exit load, stamp duty and tax"
    block = page.find(heading, h1)
    end = page.find("Tax implication", block) + 2
    labels = {"Exit load", "Stamp duty on investment:", "Tax implication"}
    _, exit_load = page.value("Exit load", block, r".+", end)
    _, stamp = page.value("Stamp duty on investment:", block, r".*\d.*", end)
    _, tax = page.value("Tax implication", block, r".+", end)
    if exit_load in labels:
        raise ParseError("exit load value missing")
    return [
        Section(
            "exit_load",
            (heading, "Exit load"),
            f"Exit load: {_sentence(exit_load)}",
            ("exit_load",),
        ),
        Section(
            "stamp_duty",
            (heading, "Stamp duty"),
            f"Stamp duty on investment: {_sentence(stamp)}",
            ("stamp_duty",),
        ),
        Section(
            "tax_implication",
            (heading, "Tax implication"),
            f"Tax implication: {_sentence(tax)}",
            ("tax_implication",),
        ),
    ]


def _fund_management(page: _Page, h1: int, data: dict[str, Any]) -> Section:
    block = page.find("Fund management", h1)
    managers = data.get("fund_manager_details") or []
    if not managers:
        raise ParseError("fund_manager_details missing")
    summary, details = [], []
    for m in managers:
        person = m.get("person_name") or ""
        since = _ist(m["date_from"]).strftime("%b %Y") if m.get("date_from") else None
        i = page.find(person, block)  # stale JSON (risk 3b) fails here...
        if since and page.lines[i + 1] != since:  # ...or here
            raise ParseError(f"manager {person!r}: page shows {page.lines[i + 1]!r}, not {since}")
        summary.append(f"{person} (since {since})" if since else person)
        bio = " ".join(
            f"{label}: {_sentence(m[key])}"
            for key, label in (("education", "Education"), ("experience", "Experience"))
            if m.get(key)
        )
        if bio:
            details.append(f"{person}. {bio}")
    text = "Fund managers: " + ", ".join(summary) + ".\n" + "\n".join(details)
    return Section("fund_management", ("Fund management",), text.strip(), ("fund_managers",))


def _objective_benchmark(page: _Page, h1: int) -> list[Section]:
    _, objective = page.value("Investment Objective", h1)
    _, benchmark = page.value("Fund benchmark", h1)
    return [
        Section(
            "investment_objective",
            ("Investment objective",),
            f"Investment objective: {_sentence(objective)}",
            ("investment_objective",),
        ),
        Section(
            "benchmark",
            ("Fund benchmark",),
            f"Fund benchmark: {_sentence(benchmark)}",
            ("benchmark",),
        ),
    ]


def _holdings(page: _Page, h1: int, data: dict[str, Any]) -> Section:
    _, count = page.value("Holdings (", h1, r"\d+")
    holdings = data.get("holdings") or []
    if len(holdings) != int(count):
        raise ParseError(f"page shows {count} holdings, embedded JSON has {len(holdings)}")
    if holdings and holdings[0].get("company_name") not in page.lines:
        raise ParseError("first holding not shown on the page")
    dates = [_ist(h["portfolio_date"]) for h in holdings if h.get("portfolio_date")]
    as_of = f" as of {_day(max(dates))}" if dates else ""
    lead = f"Portfolio holdings{as_of} ({len(holdings)} holdings, % of fund assets):"
    rows = [
        f"{h['company_name']} ({h.get('sector_name') or '--'}, {h.get('instrument_name') or '--'}):"
        f" {float(h['corpus_per']):.2f}%"
        for h in holdings
    ]
    return Section("holdings", ("Holdings",), "\n".join([lead, *rows]), ("holdings",))


def _fund_house_registrar(page: _Page, h1: int) -> list[Section]:
    block = page.find("Fund house", h1)
    custodian_i = page.find("Custodian", block)
    house = page.lines[block + 1]
    _, phone = page.value("Phone", block, r"[\d\s()+–-]{6,}", custodian_i)
    _, address = page.value("Address", block, r".+", custodian_i)
    sections = [
        Section(
            "fund_house",
            ("Fund house",),
            f"Fund house (AMC): {_sentence(house)}\nPhone: {_sentence(phone)}\n"
            f"Address: {_sentence(address)}",
            ("fund_house",),
        )
    ]
    custodian = page.lines[custodian_i + 1]
    if custodian not in {"--", "-", "Registrar & Transfer Agent"}:
        sections.append(
            Section(
                "custodian", ("Custodian",), f"Custodian: {_sentence(custodian)}", ("custodian",)
            )
        )
    rta_i, rta = page.value("Registrar & Transfer Agent", custodian_i)
    _, rta_address = page.value("Address", rta_i)
    sections.append(
        Section(
            "registrar",
            ("Registrar & transfer agent",),
            f"Registrar & transfer agent: {_sentence(rta)}\nAddress: {_sentence(rta_address)}",
            ("registrar",),
        )
    )
    return sections
