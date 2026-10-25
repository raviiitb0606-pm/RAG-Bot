"""P4.4 / P4.5: sentence splitter and number normaliser (VAL-01…VAL-06)."""

import pytest

from mf_faq.pipeline.text import (
    count_sentences,
    extract_figures,
    extract_numbers,
    split_sentences,
    ungrounded_numbers,
)

# --- sentences (V2) -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The minimum SIP is Rs. 100 for the fund.",
        "It charges fees, e.g. the exit load, on early exits.",
        "The direct plan, i.e. this one, has no commission.",
        "Folio No. 5 is fine.",
        "The cost is approx. 1% a year.",
        "It is 0.5% p.a. for this fund.",
        "Managed by HDFC Asset Management Co. Ltd. since 2020.",
        "Large cap vs. mid cap is a category question.",
        "R. Srinivasan has managed it since 2021.",
    ],
)
def test_val_01_abbreviations_do_not_end_sentences(text):
    assert count_sentences(text) == 1


@pytest.mark.parametrize(
    "text",
    ["The expense ratio is 0.77% a year.", "It has been 1.5 yrs.", "It waits… then exits."],
)
def test_val_02_decimals_and_ellipses_do_not_split(text):
    assert count_sentences(text) == 1


def test_sentences_split_on_real_ends():
    text = "The exit load is 1%. It applies within 1 year! Is that clear? Yes."
    assert split_sentences(text) == [
        "The exit load is 1%.",
        "It applies within 1 year!",
        "Is that clear?",
        "Yes.",
    ]
    assert count_sentences("No. The fund has no lock-in.") == 2  # "No." as an answer
    assert count_sentences("The fund is run by HDFC AMC Ltd. It is in Mumbai.") == 2


def test_llm_03_each_line_or_bullet_is_a_sentence():
    assert count_sentences("- one\n- two\n* three\n1. four") == 4
    assert count_sentences("Line one\nLine two") == 2


def test_empty_and_punctuation_only():
    assert count_sentences("") == 0 and count_sentences(" … \n -- ") == 0
    assert count_sentences("No full stop at the end") == 1


# --- numbers (V4) ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a, b",
    [
        ("1%", "1.00%"),
        ("1.05%", "1.05 per cent"),
        ("₹100", "Rs. 100"),
        ("₹100", "100 rupees"),
        ("₹500", "500/-"),
        ("₹1,00,000", "Rs 100000"),
        ("₹1.25 lakh", "Rs 1,25,000"),
        ("₹1,01,856.06 Cr", "₹1,01,856.06 crore"),
        ("3 years", "three years"),
        ("1 year", "one yr"),
        ("23 Oct 2026", "23rd October 2026"),
        ("Oct 2026", "October 2026"),
    ],
)
def test_val_03_formats_normalise_to_one_value(a, b):
    assert extract_numbers(a) == extract_numbers(b)


def test_val_04_durations_keep_their_unit():
    assert extract_numbers("365 days") != extract_numbers("1 year")
    assert extract_numbers("12 months") != extract_numbers("1 year")
    assert ungrounded_numbers("within 12 months", "within 1 year") == ["dur:12:month"]


def test_val_05_nil_grounds_zero_percent():
    assert ungrounded_numbers("The exit load is 0%.", "Exit load: Nil.") == []
    assert ungrounded_numbers("The exit load is 0%.", "There is no exit load.") == []
    assert ungrounded_numbers("The exit load is 0%.", "Exit load of 1%.") == ["pct:0"]


def test_val_06_numbers_in_names_ground_against_the_header():
    chunk = "[Scheme: X | Section: Benchmark]\nBenchmark: NIFTY 100 Total Return Index."
    assert ungrounded_numbers("Its benchmark is the NIFTY 100 TRI.", chunk) == []
    assert ungrounded_numbers("Its benchmark is the Nifty 50.", chunk) == ["num:50"]


def test_grounding_rules():
    chunk = "Latest NAV: ₹1,133.76 (as of 23 Oct 2026). Exit load of 1% if redeemed within 1 year."
    assert ungrounded_numbers("The NAV is ₹1,133.76 as of 23 October 2026.", chunk) == []
    assert ungrounded_numbers("The NAV is 1,133.76.", chunk) == []  # bare number, any form
    assert ungrounded_numbers("It is 1% within one year.", chunk) == []
    assert ungrounded_numbers("It was 1.5% in 2025.", chunk) == ["pct:1.5", "num:2025"]
    assert ungrounded_numbers("The NAV is ₹1,200.", chunk) == ["inr:1200"]
    assert ungrounded_numbers("As of 24 Oct 2026.", chunk) == ["date:2026-10-24"]
    assert ungrounded_numbers("It was in 2026.", chunk) == []  # a date's year grounds it
    assert ungrounded_numbers("Gains over ₹1.25 lakh.", "Rs 1.25 lakh in a year") == []
    assert ungrounded_numbers("1.25", "Rs 1.25 lakh") == []  # the raw figure, too
    assert ungrounded_numbers("No figures here.", chunk) == []


def test_figures_are_read_once_most_specific_first():
    figures = extract_figures("₹1,00,000 for 3 years at 1.05% on 23 Oct 2026; Nifty 50")
    assert [f.token for f in figures] == [
        "date:2026-10-23",
        "pct:1.05",
        "inr:100000",
        "dur:3:year",
        "num:50",
    ]
    assert extract_numbers("1 lakh") == {"num:100000"}
    assert extract_numbers("23 Oct '26") == {"date:2026-10-23"}
    assert extract_numbers("50:25:25") == {"num:50", "num:25"}
