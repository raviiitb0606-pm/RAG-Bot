"""Remove returns, NAV history and peer comparisons before indexing (P2.4, CR-2).

Three layers, so no return figure reaches the index:

1. `strip_performance_json()` deletes the performance keys from the page's embedded JSON
   before the parser reads it, so the parser *cannot* pick them up.
2. The parser (P2.3) reads only whitelisted fact groups; the returns header, return
   calculator, "Returns and rankings" table, peer table and rating are never read.
3. `strip_performance_sections()` drops any sentence that still matches a return-figure
   pattern (edge case PAR-03), and the build gate (P2.7) fails if one survives.

`find_performance_patterns()` is shared by the gate and validator check V6 (P4.6). Its
patterns are deliberately tight: the page's own tax rule ("returns are taxed at 20%") and
exit load ("1% if redeemed within 1 year") must pass (PAR-04, Phase 1 carry-over).
"""

import logging
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from mf_faq.logs import log_event

if TYPE_CHECKING:
    from mf_faq.ingest.parse_html import Section

logger = logging.getLogger(__name__)

# Keys of `mfServerSideData` that carry returns, rankings, ratings or opinion (P1.3, D-03).
PERFORMANCE_JSON_KEYS = frozenset(
    {
        "stats",
        "return_stats",
        "sip_return",
        "simple_return",
        "peerComparison",
        "analysis",
        "groww_rating",
        "crisil_rating",
        "meta_desc",  # "with three year return of 15.26"
    }
)

# Visible headings that show the page has performance content (for `has_performance_section`).
_PERFORMANCE_MARKERS = ("Returns and rankings", "Return calculator", "3Y annualised")

_NUM = r"[+-]?\d+(?:\.\d+)?"
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx, re.IGNORECASE))
    for name, rx in (
        (
            "return_term",
            r"\b(?:cagr|xirr|annuali[sz]ed|absolute returns?|trailing returns?|historic returns"
            r"|category average|sip returns?|fund returns)\b",
        ),
        ("period_token", r"\b\d{1,2}\s?Y\b(?!\w)"),
        ("signed_percent", r"\+\s?\d+(?:\.\d+)?\s?%"),
        ("return_figure", rf"\breturns?\s+(?:of|was|were|is|at|stood at)\s+{_NUM}"),
        ("figure_return", rf"{_NUM}\s?%\s+(?:returns?|cagr|annuali[sz]ed|p\.\s?a\.)"),
        (
            "growth_verb",
            rf"\b(?:delivered|generated|returned|gained|grew|rose|fell|outperformed"
            rf"|underperformed|beat)\b[^.]{{0,40}}?{_NUM}\s?%",
        ),
        (
            "over_period",
            rf"{_NUM}\s?%\s+(?:in|over)\s+(?:the\s+)?(?:last\s+|past\s+)?\d+\s*(?:years?|months?|yrs?)\b",
        ),
        ("rank", r"\brank(?:ed|ing)?\b\s*(?:\(|#|\d)"),
        ("rating", r"\b(?:groww|star|crisil)\s+rating\b"),
        ("calculator", r"\bwould(?:'ve| have) become\b"),
    )
)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z₹])")


def find_performance_patterns(text: str) -> list[str]:
    """Return `name: matched text` for every return/performance pattern found in `text`."""
    hits = []
    for name, pattern in _PATTERNS:
        hits.extend(f"{name}: {m.group(0)}" for m in pattern.finditer(text))
    return hits


def strip_performance_json(data: dict[str, Any]) -> dict[str, Any]:
    """A copy of `mfServerSideData` without the performance keys."""
    return {k: v for k, v in data.items() if k not in PERFORMANCE_JSON_KEYS}


def has_performance_section(data: dict[str, Any], lines: list[str]) -> bool:
    """True if the page shows returns, so the performance template can point to it (P2.4)."""
    if any(data.get(k) for k in ("stats", "return_stats", "simple_return")):
        return True
    return any(marker in lines for marker in _PERFORMANCE_MARKERS)


def strip_performance_sections(sections: list["Section"]) -> list["Section"]:
    """Drop sentences that match a performance pattern; drop sections left empty (PAR-03)."""
    kept: list[Section] = []
    for section in sections:
        lines = []
        removed = 0
        for line in section.text.split("\n"):
            sentences = _SENTENCE_RE.split(line)
            clean = [s for s in sentences if not find_performance_patterns(s)]
            removed += len(sentences) - len(clean)
            if clean:
                lines.append(" ".join(clean))
        if removed:
            log_event(
                logger,
                "ingest.performance_stripped",
                level=logging.WARNING,
                group=section.group,
                sentences=removed,
            )
        if lines:
            kept.append(replace(section, text="\n".join(lines)))
    return kept
