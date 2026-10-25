"""Sentence splitter and number normaliser for the validator (P4.4, P4.5, arch §6.7 V2/V4).

`split_sentences()` counts sentences the way a reader would (R-1 is "at most 3 sentences"):
abbreviations (`Rs.`, `e.g.`, `i.e.`, `No. 5`, `approx.`, `p.a.`, `Ltd.`, `vs.`), initials,
decimals (`1.05%`) and ellipses don't end a sentence; every non-empty line does, so each bullet
point counts as a sentence (LLM-03).

`extract_numbers()` turns every figure in a text into a canonical token, so `1%` = `1.00%`,
`₹100` = `Rs. 100` = `100 rupees` = `100/-`, `1,00,000` = `100000` = `1 lakh`, `3 years` =
`three years`. Durations keep their unit: `365 days` ≠ `1 year` (VAL-04, no conversion).
`ungrounded_numbers(answer, chunk)` lists the answer's figures that the chunk doesn't contain
(V4): a bare number matches the same value in any form, a percentage, amount, duration or date
needs the same kind. "Nil" / "no exit load" in a chunk grounds `0%` (VAL-05).
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# --- sentences ------------------------------------------------------------------------------

_MARK = "\x00"  # stands in for a protected "."
# Always abbreviations, wherever they appear.
_ABBREV_ALWAYS = re.compile(
    r"\b(?:rs|e\.g|i\.e|approx|p\.a|vs|viz|cf|etc|mr|mrs|ms|dr|sr|jr|st|yrs?|hrs?|mins?"
    r"|inr|max|min|incl|excl|avg|est|govt|dept)\.",
    re.IGNORECASE,
)
_NO_NUMBER = re.compile(r"\bNo\.(?=\s*\d)", re.IGNORECASE)  # "No. 5", not the answer "No."
# Company suffixes end a sentence only when a capitalised word follows ("… Ltd. It …").
_ABBREV_SUFFIX = re.compile(
    r"\b(?i:ltd|pvt|co|inc|corp|bros)\.(?=\s*(?:[a-z0-9(,;:%]|(?i:ltd|pvt|inc|corp)\b))"
)
_INITIAL = re.compile(r"\b[A-Z]\.(?=\s*[A-Z])")  # "R. Srinivasan", "U.S."
_ELLIPSIS = re.compile(r"\.\.\.|…")
_DECIMAL = re.compile(r"(?<=\d)\.(?=\d)")
_BULLET = re.compile(r"^\s*(?:[-*•–·]|\d{1,2}[.)])\s+")
_SPLIT = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+")
_WORDY = re.compile(r"[A-Za-z0-9₹]")


def _protect(line: str) -> str:
    for pattern in (_ELLIPSIS, _DECIMAL, _ABBREV_ALWAYS, _NO_NUMBER, _ABBREV_SUFFIX, _INITIAL):
        line = pattern.sub(lambda m: m.group(0).replace(".", _MARK).replace("…", _MARK), line)
    return line


def split_sentences(text: str) -> list[str]:
    """Sentences of `text`, each line (bullet) on its own; empty pieces dropped."""
    sentences: list[str] = []
    for line in text.splitlines():
        line = _BULLET.sub("", line)
        for part in _SPLIT.split(_protect(line).strip()):
            if _WORDY.search(part):
                sentences.append(part.replace(_MARK, ".").strip())
    return sentences


def count_sentences(text: str) -> int:
    return len(split_sentences(text))


# --- numbers --------------------------------------------------------------------------------

_WORD_NUMBERS = {
    w: i
    for i, w in enumerate(
        (
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
            "twenty",
        )
    )
}
_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_UNITS = {"day": "day", "week": "week", "month": "month", "year": "year", "yr": "year"}
_SCALES = {"lakh": Decimal(100_000), "lakhs": Decimal(100_000), "lac": Decimal(100_000)}
_SCALES |= {"crore": Decimal(10_000_000), "crores": Decimal(10_000_000)}
_SCALES |= {"cr": Decimal(10_000_000)}

_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_DATE_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH})\.?,?\s+'?(\d{{4}}|\d{{2}})\b", re.IGNORECASE
)
_MONTH_YEAR_RE = re.compile(rf"\b({_MONTH})\.?,?\s+(\d{{4}})\b", re.IGNORECASE)
_PERCENT_RE = re.compile(rf"({_NUM})\s*(?:%|per\s?cent\b|percent\b)", re.IGNORECASE)
_SCALE = r"lakhs?|lac|crores?|cr\b"
_MONEY_RE = re.compile(
    rf"(?:₹|\brs\.?|\binr)\s*({_NUM})(?:\s*({_SCALE}))?"
    rf"|({_NUM})\s*(?:({_SCALE})\s*)?(?:rupees|/-)",
    re.IGNORECASE,
)
_WORD_NUM = "|".join(_WORD_NUMBERS)
_DURATION_RE = re.compile(
    rf"\b({_NUM}|{_WORD_NUM})[\s-]+(days?|weeks?|months?|years?|yrs?)\b", re.IGNORECASE
)
_SCALED_RE = re.compile(rf"({_NUM})\s*({_SCALE})", re.IGNORECASE)
_BARE_RE = re.compile(rf"(?<![\w.])({_NUM})(?![\w])")
_NIL_RE = re.compile(r"\bnil\b|\bno exit load\b|\bnot applicable\b", re.IGNORECASE)


@dataclass(frozen=True)
class Figure:
    kind: str  # pct | inr | dur | date | month | num
    value: str  # canonical: Decimal string, or an ISO date
    unit: str = ""

    @property
    def token(self) -> str:
        return f"{self.kind}:{self.value}" + (f":{self.unit}" if self.unit else "")


def _decimal(raw: str) -> Decimal:
    if raw.lower() in _WORD_NUMBERS:
        return Decimal(_WORD_NUMBERS[raw.lower()])
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation as e:  # pragma: no cover - the regexes only pass digits
        raise ValueError(raw) from e


def _canon(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _year(raw: str) -> int:
    year = int(raw)
    return year + 2000 if year < 100 else year


def extract_figures(text: str) -> list[Figure]:
    """Every figure in `text`, most specific reading first; each character is read once."""
    figures: list[Figure] = []
    taken = [False] * len(text)

    def claim(m: re.Match[str]) -> bool:
        if any(taken[m.start() : m.end()]):
            return False
        taken[m.start() : m.end()] = [True] * (m.end() - m.start())
        return True

    for m in _DATE_RE.finditer(text):
        if claim(m):
            month = _MONTHS[m.group(2).lower()]
            iso = f"{_year(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"
            figures.append(Figure("date", iso))
    for m in _MONTH_YEAR_RE.finditer(text):
        if claim(m):
            figures.append(Figure("month", f"{m.group(2)}-{_MONTHS[m.group(1).lower()]:02d}"))
    for m in _PERCENT_RE.finditer(text):
        if claim(m):
            figures.append(Figure("pct", _canon(_decimal(m.group(1)))))
    for m in _MONEY_RE.finditer(text):
        if claim(m):
            number, scale = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            value = _decimal(number) * _SCALES.get((scale or "").lower(), Decimal(1))
            figures.append(Figure("inr", _canon(value)))
    for m in _DURATION_RE.finditer(text):
        if claim(m):
            unit = _UNITS[m.group(2).lower().rstrip("s")]
            figures.append(Figure("dur", _canon(_decimal(m.group(1))), unit))
    for m in _SCALED_RE.finditer(text):
        if claim(m):
            value = _decimal(m.group(1)) * _SCALES[m.group(2).lower()]
            figures.append(Figure("num", _canon(value)))
    for m in _BARE_RE.finditer(text):
        if claim(m):
            figures.append(Figure("num", _canon(_decimal(m.group(1)))))
    return figures


def extract_numbers(text: str) -> set[str]:
    """Canonical tokens, e.g. {"pct:1.05", "inr:500", "dur:1:year", "date:2026-10-23"}."""
    return {f.token for f in extract_figures(text)}


def _grounding_set(source: str) -> tuple[set[str], set[str]]:
    """(tokens, bare values) a source can ground; generous on the source side."""
    tokens, values = set(), set()
    for f in extract_figures(source):
        tokens.add(f.token)
        if f.kind in ("date", "month"):
            year, month, *day = f.value.split("-")
            values.update({str(int(year)), str(int(month)), *(str(int(d)) for d in day)})
        else:
            values.add(f.value)
    for m in _BARE_RE.finditer(source):  # "Rs 1.25 lakh" also grounds a bare "1.25"
        values.add(_canon(_decimal(m.group(1))))
    if _NIL_RE.search(source):
        tokens.add("pct:0")
        values.add("0")
    return tokens, values


def ungrounded_numbers(answer: str, source: str) -> list[str]:
    """The answer's figures that don't appear (normalised) in `source` (V4)."""
    tokens, values = _grounding_set(source)
    missing: list[str] = []
    for f in extract_figures(answer):
        if f.kind == "num":
            ok = f.value in values
        elif f.kind == "inr":
            ok = f.token in tokens or f.value in values
        else:
            ok = f.token in tokens
        if not ok:
            missing.append(f.token)
    return missing
