"""Unsupported-topic lexicon: questions the 5 scheme pages can't answer (P3.4 layer C, P4.2).

Retrieval can't catch these: once a scheme is named, the scheme filter always returns that
scheme's closest section, and it looks relevant ("Sharpe *ratio*" → expense ratio). Their cosine
scores overlap answerable questions (research §3.5), so a word list decides instead.

Precision first: a term is listed only if no answerable question needs it. Each term is a topic
that is not on any page (lock-in, launch date, ratings, risk ratios, statements, KYC, SWP/STP,
IDCW, regular plan…) or a how-to the pages don't explain ("how do I redeem"). It started from
the research lexicon (16/16 scheme-scoped out-of-corpus questions caught, 0/112 in-corpus false
positives); `tests/unit/test_retrieve.py` checks every in-corpus eval question and every holding
name still passes. Shared with the Phase 4 router.
"""

import re

_TERMS: dict[str, str] = {
    "lock_in": r"lock[\s-]?in",
    "launch_date": r"launch(?:ed)?|inception|start(?:ed)? on",
    "rating": r"(?:groww\s+|star\s+|crisil\s+|value\s+research\s+)?ratings?|rated",
    "statement": r"statements?|capital\s+gains?\s+report",
    "kyc": r"kyc",
    "nominee": r"nomin(?:ee|ees|ate|ation)",
    "swp_stp": r"swp|stp|systematic\s+(?:withdrawal|transfer)",
    "idcw": r"idcw|dividends?|payouts?",
    "risk_ratio": (
        r"sharpe|sortino|standard\s+deviation|beta|alpha|turnover|p/?e\s+ratio|p/?b\s+ratio"
        r"|tracking\s+error|tracking\s+difference|information\s+ratio|r[\s-]?squared"
    ),
    "regular_plan": r"regular\s+plan|idcw\s+plan|dividend\s+plan",
    "people": r"ceo|cio|chairman|managing\s+director",
    "sip_dates": r"sip\s+dates?|sip\s+date",
    "mandate": r"mandates?|auto[\s-]?debit|e[\s-]?nach|nach",
    "elss": r"elss|80c",
    "itr": r"income\s+tax\s+returns?|itr",
    "redemption_rules": r"minimum\s+redemption|redemption\s+time|redemption\s+proceeds",
    "how_to": (
        r"how\s+(?:do|can|should|to)\s+(?:i\s+|we\s+)?"
        r"(?:redeem|withdraw|sell|switch|invest|buy|start|stop|pause|cancel|open|close|apply)"
    ),
}
_LEXICON = re.compile(
    "|".join(f"(?P<{name}>\\b(?:{pattern})\\b)" for name, pattern in _TERMS.items()),
    re.IGNORECASE,
)


def unsupported_topic(text: str) -> str | None:
    """The first unsupported topic `text` asks about (e.g. "lock_in"), or None."""
    match = _LEXICON.search(text)
    return match.lastgroup if match else None
