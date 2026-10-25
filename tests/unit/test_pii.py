"""P4.1: PII detection and redaction (P-1, arch §6.2, edge cases PII-01…16, INP-05)."""

from pathlib import Path

import pytest

from mf_faq.eval.retrieval import load_cases
from mf_faq.pipeline.pii import contains_pii, normalize, redact, verhoeff_valid
from mf_faq.redacted import RedactedText

ROOT = Path(__file__).resolve().parents[2]
VALID_AADHAAR = "234567890124"  # passes the Verhoeff check (fixture, not a real number)


def kinds(text: str) -> tuple[str, ...]:
    return redact(text)[1]


@pytest.mark.parametrize("text", ["ABCDE1234F", "abcde1234f", "ABCDE 1234 F", "My PAN: AbCdE1234f"])
def test_pii_01_pan_formats(text):
    redacted, found = redact(text)
    assert found == ("PAN",) and "[REDACTED_PAN]" in redacted
    assert "1234" not in redacted


@pytest.mark.parametrize(
    "text", [VALID_AADHAAR, "2345 6789 0124", "2345-6789-0124", "aadhaar is 2345 6789 0124"]
)
def test_pii_02_aadhaar_with_valid_checksum(text):
    assert kinds(text) == ("AADHAAR",)


def test_pii_02_aadhaar_checksum_cuts_false_positives():
    assert verhoeff_valid(VALID_AADHAAR) and not verhoeff_valid("234567890123")
    assert "AADHAAR" not in kinds("2345 6789 0123")  # grouped, bad checksum: not an Aadhaar


@pytest.mark.parametrize("text", ["XXXX XXXX 0123", "xxxx-xxxx-0123", "**** **** 0123"])
def test_pii_03_masked_aadhaar(text):
    assert kinds(text) == ("AADHAAR",)


@pytest.mark.parametrize(
    "text", ["+91 98765 43210", "09876543210", "98765-43210", "9876543210", "+919876543210"]
)
def test_pii_04_phone_formats(text):
    assert kinds(text) == ("PHONE",)


@pytest.mark.parametrize(
    "text",
    [
        "user@example.com",
        "first.last+mf@mail.co.in",
        "user at example dot com",
        "user [at] example [dot] in",
    ],
)
def test_pii_05_email_formats(text):
    assert kinds(text) == ("EMAIL",)


@pytest.mark.parametrize(
    "text",
    [
        "my OTP is 482913",
        "code 4829",
        "one time password: 1234",
        "482913 is my OTP",
        "verification code 99887766",
    ],
)
def test_pii_06_otp_with_context(text):
    assert kinds(text) == ("OTP",)


@pytest.mark.parametrize(
    "text",
    [
        "folio 12345678/90",
        "a/c 001234567890",
        "account number: 55667788",
        "Folio No. 4455667",
        "My folio number is 12345678901",
    ],
)
def test_pii_07_account_and_folio(text):
    assert kinds(text) == ("ACCOUNT",)


def test_pii_08_pii_plus_question_is_still_pii():
    """D-04: block entirely; the redacted text keeps the question but no number."""
    redacted, found = redact("My PAN is ABCDE1234F, what is the exit load of Mid Cap?")
    assert found == ("PAN",)
    assert redacted == "My PAN is [REDACTED_PAN], what is the exit load of Mid Cap?"


def test_pii_10_names_are_not_blocked():
    assert not contains_pii("My name is Ravi, what is the benchmark of HDFC Mid Cap Fund?")


@pytest.mark.parametrize(
    "text",
    [
        "SIP of ₹500",
        "AUM ₹50,000 crore",
        "Nifty 50",
        "since 2007",
        "1 year",
        "₹1,01,856.06 Cr",
        "NAV ₹1,133.76 as of 23 Oct 2026",
        "What is the minimum SIP of 100 for HDFC Mid Cap Fund?",
        "Exit load of 1% if redeemed within 365 days",
        "Nifty 500 Multicap 50:25:25 TRI",
        "AMFI scheme code 119062",
        "ISIN INF179K01YV8",
        "What is the phone number of the AMC?",
        "What is the pin code of the HDFC AMC office?",
        "Fund size (AUM): ₹37,293.95 Cr.",
        "If I invest ₹10,000 for 5 years, how much will I get?",
        "Holdings as of 30/09/2026",
    ],
)
def test_pii_11_ordinary_figures_are_not_pii(text):
    redacted, found = redact(text)
    assert found == () and redacted == normalize(text)


def test_pii_12_amount_shaped_like_a_phone_fails_closed():
    assert kinds("9876543210 rupees") == ("PHONE",)
    assert kinds("₹500000000 AUM") == ("ACCOUNT",)  # arch §6.2: accepted false positive


def test_pii_16_zero_width_and_full_width_tricks():
    assert kinds("ABCDE​1234F") == ("PAN",)
    assert kinds("９８７６５４３２１０") == ("PHONE",)  # full-width digits, NFKC


def test_inp_05_normalize_strips_zero_width_and_keeps_case():
    assert normalize("e​xit Load﻿") == "exit Load"


def test_several_types_in_order_and_types_returned_once():
    redacted, found = redact("PAN ABCDE1234F, mail a@b.com, PAN again PQRST9876Z")
    assert found == ("EMAIL", "PAN")
    assert redacted == "PAN [REDACTED_PAN], mail [REDACTED_EMAIL], PAN again [REDACTED_PAN]"


def test_result_is_redacted_text():
    redacted, _ = redact("hello")
    assert isinstance(redacted, RedactedText) and redacted == "hello"


def test_no_eval_question_is_mistaken_for_pii():
    """Every non-PII question in the golden and retrieval sets passes untouched."""
    cases = [c for c in load_cases(ROOT / "tests" / "eval") if c.expect_kind != "pii"]
    assert len(cases) > 150
    assert [c.q for c in cases if contains_pii(c.q)] == []
    pii_cases = [c for c in load_cases(ROOT / "tests" / "eval") if c.expect_kind == "pii"]
    assert pii_cases and all(contains_pii(c.q) for c in pii_cases)
