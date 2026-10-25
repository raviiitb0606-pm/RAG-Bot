"""Detect and redact PII before anything else (P4.1, P-1, arch §6.2).

`redact(text)` runs first on every message, before logging, routing, retrieval or any LLM call.
It returns `(RedactedText, found_types)`: every match is replaced by `[REDACTED_<TYPE>]`, and
only the redacted text may be stored or logged. Any hit means the `pii` reply and no LLM call,
even when a valid question comes with it (D-04: block entirely).

The text is NFKC-normalised and stripped of zero-width characters first, so PII can't be hidden
with full-width digits or "ABCDE​1234F" (INP-05, PII-16). The raw text is not kept.

Detectors, in the order they run:
- EMAIL: the standard form, and "user at example dot com" (PII-05).
- PAN: 5 letters, 4 digits, 1 letter; any case; optional spaces (PII-01).
- AADHAAR: 12 digits (first 2-9), optionally grouped 4-4-4, with a valid Verhoeff checksum;
  or masked "XXXX XXXX 0123" (PII-02, PII-03).
- PHONE: an Indian mobile: optional +91 / 91 / 0, then 10 digits starting 6-9, optionally
  split 5-5 (PII-04).
- OTP: 4-8 digits within a few words of "otp", "one time password", "verification code",
  "pin" or "code" (but not "scheme code") (PII-06).
- ACCOUNT: digits after "account", "a/c", "acct" or "folio" (5+ digits, "/" and "-"
  allowed), or any bare run of 9-18 digits (PII-07).

Precision is second to safety: a 10-digit amount ("9876543210 rupees") counts as a phone
(PII-12). Ordinary figures stay untouched: "SIP of ₹500", "₹1,01,856.06 Cr", "Nifty 50",
"since 2007", "1 year", NAVs, dates, ISINs (PII-11).
"""

import re
import unicodedata
from collections.abc import Callable

from mf_faq.redacted import RedactedText

_ZERO_WIDTH_RE = re.compile("[­​-‏⁠﻿]")

# Verhoeff tables (Aadhaar's check digit).
_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 7, 6, 8, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_valid(digits: str) -> bool:
    check = 0
    for i, ch in enumerate(reversed(digits)):
        check = _D[check][_P[i % 8][int(ch)]]
    return check == 0


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


_DOT = r"(?:\s*(?:\(|\[)?\s*dot\s*(?:\)|\])?\s*|\.)"
_PATTERNS: tuple[tuple[str, re.Pattern[str], Callable[[re.Match[str]], bool] | None], ...] = (
    ("EMAIL", re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I), None),
    (
        "EMAIL",
        re.compile(
            rf"\b[\w.+-]+\s*(?:\(|\[)?\s*\bat\b\s*(?:\)|\])?\s*[\w-]+(?:{_DOT}[\w-]+)*"
            rf"\s*(?:\(|\[)?\s*\bdot\b\s*(?:\)|\])?\s*(?:com|in|org|net|co|edu|gov|io)\b",
            re.I,
        ),
        None,
    ),
    ("PAN", re.compile(r"\b[a-z]{5}\s?\d{4}\s?[a-z]\b", re.I), None),
    (
        "AADHAAR",
        re.compile(r"(?<![\d-])[2-9]\d{3}([\s-]?)\d{4}\1\d{4}(?![\d-])"),
        lambda m: verhoeff_valid(_digits(m.group(0))),
    ),
    ("AADHAAR", re.compile(r"(?<![\w*])[x*]{4}[\s-]?[x*]{4}[\s-]?\d{4}\b", re.I), None),
    (
        "PHONE",
        re.compile(r"(?<![\d+])(?:\+\s?91[\s-]?|91[\s-]|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"),
        None,
    ),
    (
        "OTP",
        re.compile(
            r"\b(?:otp|one[\s-]time[\s-]password|verification\s+code|security\s+code|pin(?!\s*code)"
            r"|(?<!scheme\s)(?<!amfi\s)(?<!fund\s)(?<!isin\s)(?<!product\s)code)\b"
            r"(?:\W+\w+){0,3}?\W+\d{4,8}\b"
            r"|\b\d{4,8}\b(?:\W+\w+){0,3}?\W+(?:is\s+)?(?:my|the)\s+(?:otp|pin|code)\b",
            re.I,
        ),
        None,
    ),
    (
        "ACCOUNT",
        re.compile(
            r"\b(?:account|acct|a/c|folio)(?:\s*(?:no|number|num|#))?\.?\s*(?:is\s*)?[:#-]?\s*"
            r"\d[\d/-]{3,}\d",
            re.I,
        ),
        lambda m: len(_digits(m.group(0))) >= 5,
    ),
    ("ACCOUNT", re.compile(r"(?<![\d.,/])\d{9,18}(?![\d.,/]|\s*%)"), None),
)


def normalize(text: str) -> str:
    """NFKC and no zero-width characters; case is kept (INP-05)."""
    return _ZERO_WIDTH_RE.sub("", unicodedata.normalize("NFKC", text))


def redact(text: str) -> tuple[RedactedText, tuple[str, ...]]:
    """(text with every PII match replaced by `[REDACTED_<TYPE>]`, the types found, in order)."""
    redacted = normalize(text)
    found: list[str] = []
    for kind, pattern, accept in _PATTERNS:

        def replace(m: re.Match[str], kind: str = kind, accept=accept) -> str:
            if accept and not accept(m):
                return m.group(0)
            if kind not in found:
                found.append(kind)
            return f"[REDACTED_{kind}]"

        redacted = pattern.sub(replace, redacted)
    return RedactedText(redacted), tuple(found)


def contains_pii(text: str) -> bool:
    return bool(redact(text)[1])
