"""P3.2: scheme resolution, other-AMC and deny-list checks, follow-up carry-over."""

import pytest
import yaml

from conftest import CATALOG, ROOT, page_html, source
from mf_faq.ingest.build import process_html
from mf_faq.pipeline.schemes import Reason, SchemeResolver, normalize

LC, MC, SC, MU, SV = (
    "hdfc-large-cap",
    "hdfc-mid-cap",
    "hdfc-small-cap",
    "hdfc-multi-cap",
    "hdfc-silver-etf-fof",
)
ALL = (LC, MC, SC, MU, SV)

resolver = SchemeResolver(CATALOG)


def resolve(text, active=None):
    return resolver.resolve(text, active)


@pytest.mark.parametrize(
    "text, scheme",
    [
        ("What is the expense ratio of HDFC Mid Cap Fund – Direct Growth?", MC),
        ("HDFC Mid Cap Fund's exit load", MC),
        ("hdfc midcap nav", MC),
        ("hdfc mid-cap fund benchmark", MC),
        ("HDFC Mid Cap Opportunities Fund exit load", MC),  # former name (SCP-12)
        ("mid cap fund exit load", MC),  # category only, one mid-cap scheme in scope (SCP-05)
        ("hdfc smal cap exit load", SC),  # typo (INP-06)
        ("HDFC Smallcap expense ratio", SC),
        ("HDFC LargeCap", LC),
        ("large-cap expense ratio", LC),
        ("multi cap", MU),
        ("silver fof expense ratio", SV),
        ("the silver one", SV),  # nickname (SCP-11)
        ("HDFC Silver Fund of Fund exit load", SV),
        ("HDFC Silver ETF FoF expense ratio", SV),
        ("What percentage of HDFC Silver ETF FoF is in the Silver ETF?", SV),
        ("e​xit load of HDFC Mid Cap", MC),  # zero-width character (INP-05)
    ],
)
def test_aliases_and_misspellings(text, scheme):
    result = resolve(text)
    assert result.scheme_ids == (scheme,) and result.reason is Reason.MATCHED, result


@pytest.mark.parametrize(
    "text, schemes",
    [
        ("What is the expense ratio of HDFC Mid Cap Fund and HDFC Small Cap Fund?", (MC, SC)),
        ("Which has the lower expense ratio, Mid Cap or Small Cap?", (MC, SC)),  # MUL-02
        ("Exit load of HDFC Large Cap Fund and HDFC Multi Cap Fund?", (LC, MU)),
        ("Exit load of all 5 funds", ALL),  # MUL-03
        ("expense ratio of each scheme", ALL),
    ],
)
def test_several_schemes(text, schemes):
    result = resolve(text)
    assert result.scheme_ids == schemes and result.reason is Reason.MATCHED
    assert result.scheme_id is None  # no single scheme, so no retrieval


def test_same_scheme_twice_is_one_scheme():
    assert resolve("HDFC Large Cap Fund vs the large cap fund").scheme_ids == (LC,)


@pytest.mark.parametrize(
    "text",
    [
        "What is the exit load of SBI Small Cap Fund?",
        "What is the exit load of SBI Bluechip Fund?",
        "ICICI Prudential Large Cap Fund expense ratio",
        "Nippon India Silver ETF FoF exit load",
        "What is the expense ratio of Axis Mid Cap Fund?",
        "Axis Midcap",
        "Kotak Multicap Fund benchmark",
        "Mirae Asset Large Cap Fund",
        "Who manages Parag Parikh Flexi Cap Fund?",
        "Union Small Cap Fund exit load",
        "Groww Large Cap Fund",
        "Nippon Small Cap",  # SCP-04
        "HDFC Mid Cap Fund vs SBI Mid Cap",
    ],
)
def test_other_amc_never_resolves_to_hdfc(text):
    for active in (None, MC):
        result = resolve(text, active)
        assert result.scheme_ids == () and result.reason is Reason.OTHER_AMC, result
        assert result.out_of_scope


@pytest.mark.parametrize(
    "text, scheme",
    [
        ("Does HDFC Large Cap Fund hold ICICI Bank?", LC),
        ("How much SBI does HDFC Large Cap hold?", LC),
        ("What is the Groww rating of HDFC Multi Cap Fund?", MU),  # Groww is the source
        ("According to Groww, what is the expense ratio of HDFC Mid Cap Fund?", MC),
        ("Weight of Tata Motors in HDFC Small Cap Fund", SC),
        ("Is Union Bank of India in HDFC Mid Cap Fund?", MC),
    ],
)
def test_company_names_are_not_fund_houses(text, scheme):
    assert resolve(text).scheme_ids == (scheme,)


def _holdings_names() -> list[tuple[str, str]]:
    """Every holding listed on the 5 pages, with its scheme."""
    out = []
    for scheme in CATALOG.schemes:
        src = source(scheme.source_id)
        _, chunks, _ = process_html(page_html(src.id), src, CATALOG)
        for chunk in chunks:
            if chunk.chunk_id.split(":")[1] == "holdings":
                out += [
                    (line.split(" (")[0], scheme.scheme_id) for line in chunk.text.split("\n")[1:]
                ]
    return out


def test_no_holding_is_mistaken_for_another_scheme_or_fund_house():
    """ICICI Bank, SBI Funds Management, Tata Motors, HDFC Silver ETF... are answerable."""
    names = _holdings_names()
    assert len(names) > 200
    bad = []
    for name, scheme_id in names:
        short = CATALOG.by_id(scheme_id).short_name
        for text, active in (
            (f"Does {short} hold {name}?", None),
            (f"What is the weight of {name} in {short}?", None),
            (f"How much {name} does it hold?", scheme_id),
        ):
            if resolve(text, active).scheme_ids != (scheme_id,):
                bad.append(text)
    assert not bad


@pytest.mark.parametrize(
    "text, label",
    [
        ("What is the expense ratio of HDFC Flexi Cap Fund?", "HDFC Flexi Cap Fund"),  # SCP-01
        ("HDFC Flexicap exit load", "HDFC Flexi Cap Fund"),
        ("HDFC ELSS Tax Saver Fund lock-in", "HDFC ELSS Tax Saver Fund"),
        ("HDFC Large and Mid Cap Fund expense ratio", "HDFC Large & Mid Cap Fund"),  # SCP-02
        ("HDFC Large & Mid Cap exit load", "HDFC Large & Mid Cap Fund"),
        ("large and mid cap fund benchmark", "HDFC Large & Mid Cap Fund"),
        ("What is the expense ratio of HDFC Silver ETF?", "HDFC Silver ETF"),  # SCP-03
        ("silver etf exit load", "HDFC Silver ETF"),
        ("HDFC Multi Asset Allocation Fund", "HDFC Multi Asset Allocation Fund"),
        ("HDFC Nifty Midcap 150 Index Fund", "HDFC NIFTY Midcap 150 Index Fund"),
        ("HDFC Value Fund", "HDFC Value Fund"),
    ],
)
def test_not_in_scope_beats_aliases(text, label):
    for active in (None, MU):
        result = resolve(text, active)
        assert result.scheme_ids == () and result.reason is Reason.NOT_IN_SCOPE, result
        assert result.matched == (label,)


@pytest.mark.parametrize(
    "text",
    [
        "What is the long term capital gains tax on it?",  # not "HDFC Long Term Fund"
        "What is the NAV value?",  # not "HDFC Value Fund"
        "How liquid is it?",  # not "HDFC Liquid Fund"
    ],
)
def test_deny_list_needs_more_than_a_common_word(text):
    assert resolve(text, MC).scheme_ids == (MC,)


@pytest.mark.parametrize(
    "text",
    [
        "And its exit load?",
        "What about the benchmark?",
        "Who manages it?",
        "Holdings?",
        "exit load?",
        "minimum SIP",
        "Does this fund have a stamp duty?",
        "Is its benchmark index the NIFTY 100?",
        "Which ETF does it invest in?",
        "What is the address of HDFC Mutual Fund?",
    ],
)
def test_follow_up_carries_the_active_scheme(text):
    result = resolve(text, LC)
    assert result.scheme_ids == (LC,) and result.reason is Reason.CARRY_OVER, result


def test_follow_up_without_active_scheme_asks_which():
    for text in ("And its exit load?", "What is the expense ratio?"):  # FUP-02, D-09
        result = resolve(text)
        assert result.scheme_ids == () and result.reason is Reason.NEEDS_SCHEME


def test_the_other_one_is_ambiguous():
    assert resolve("And the other one?", MC).reason is Reason.NEEDS_SCHEME  # FUP-04


@pytest.mark.parametrize(
    "text",
    [
        "What is the lock-in for ELSS funds?",  # FUP-06
        "ELSS lock-in?",
        "Do all mutual funds charge stamp duty?",
        "What is a mutual fund?",
        "What is the expense ratio of index funds?",
        "exit load of SBI?",  # a bare other-AMC name
        "How do I complete my KYC?",
        "What is the price of silver today?",
    ],
)
def test_general_questions_do_not_carry_over(text):
    result = resolve(text, MC)
    assert result.scheme_ids == () and result.reason is Reason.NONE, result


def test_switching_scheme_carries_the_most_recent():
    """FUP-03: the caller stores the last resolved scheme; the resolver uses what it's given."""
    active = None
    for text in ("Exit load of HDFC Small Cap Fund?", "Minimum SIP for HDFC Silver ETF FoF?"):
        active = resolve(text, active).scheme_id
    assert resolve("And its expense ratio?", active).scheme_ids == (SV,)


def test_compare_it_with_another_scheme():
    result = resolve("Compare it with Small Cap", MC)  # MUL-04
    assert result.scheme_ids == (MC, SC) and result.reason is Reason.MATCHED
    assert resolve("What is the exit load of HDFC Small Cap Fund?", MC).scheme_ids == (SC,)


def test_unknown_active_scheme_is_ignored():
    assert resolve("And its exit load?", "hdfc-flexi-cap").reason is Reason.NEEDS_SCHEME


def test_normalize():
    assert normalize("HDFC Mid Cap Fund’s e​xit ＬＯＡＤ") == "hdfc mid cap fund exit load"


def _golden_turns():
    for case in yaml.safe_load((ROOT / "tests" / "eval" / "golden.yaml").read_text()):
        yield case["id"], case.get("turns", [case])


@pytest.mark.parametrize("case_id, turns", list(_golden_turns()))
def test_golden_cases(case_id, turns):
    """Every golden `answer` turn resolves to its page; multi-scheme turns to several schemes."""
    by_source = {s.source_id: s.scheme_id for s in CATALOG.schemes}
    active = None
    for turn in turns:
        result = resolve(turn["q"], active)
        kind = turn["expect_kind"]
        if kind == "answer":
            assert result.scheme_ids == (by_source[turn["expect_source"]],), turn["q"]
        elif kind == "multi_scheme":
            assert len(result.scheme_ids) > 1, turn["q"]
        active = result.scheme_id or active


def test_research_in_corpus_questions_resolve():
    """The Phase 3 research query sets (paraphrase, held-out, holdings) name their scheme."""
    import sys

    sys.path.insert(0, str(ROOT / "docs" / "research" / "phase3"))
    try:
        from queries import HELDOUT, HOLDINGS_DEEP, PARAPHRASES
    finally:
        sys.path.pop(0)
    misses = [
        (q, sid, resolve(q))
        for q, sid, _ in PARAPHRASES + HELDOUT + HOLDINGS_DEEP
        if resolve(q).scheme_ids != (sid,)
    ]
    # The one question naming only the fund house needs the D-09 clarify reply (or P4.3).
    assert [q for q, *_ in misses] == ["What is the address of HDFC Mutual Fund?"]
    assert misses[0][2].reason is Reason.NEEDS_SCHEME


def test_catalog_has_other_amcs():
    raw = yaml.safe_load((ROOT / "corpus" / "schemes.yaml").read_text())
    assert {"SBI", "ICICI Prudential", "Nippon India", "Axis", "Kotak"} <= set(
        raw["other_amcs"]["names"]
    )
    assert "Groww" in raw["other_amcs"]["need_fund_word"]
