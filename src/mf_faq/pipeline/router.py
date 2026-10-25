"""Precision-first rule-based intent pre-screen (P4.2, arch §6.3).

`route(text)` runs right after the PII guard, on the redacted text, and catches the obvious
cases without spending an LLM call. Rules are checked in this order; the first hit wins:

- `greeting` → `help`: the **whole** message is a greeting, thanks, "what can you do",
  "where does your data come from", or only emoji / punctuation (OTH-01, OTH-05, OTH-06,
  INP-03). Short abuse gets the same neutral reply (OTH-04).
- `advisory` → `refusal`: "should I invest", "which is better", "best fund", "recommend",
  "is it worth it", "is it safe", "SIP or lump sum", "how much should I invest", "is a 1%
  expense ratio high?" (ADV-01…06).
- `transaction` → `refusal` with transaction wording: "Buy ₹5,000 of Mid Cap", "Redeem my
  units" (OTH-07, D-08).
- `performance` → `performance`: returns, CAGR, XIRR, "beaten its benchmark", rank, "how much
  will I get", NAV history or a past NAV / fund size (PRF-01…05).
- `historical` → `not_found`: any other fact asked for a past date ("expense ratio last
  year", "exit load in 2019"); the pages only show current values.
- `personal` → `not_found`: "show my holdings", "my SIP status", "my balance" (OTH-08).
- `factual`: everything else goes on to scheme resolution and retrieval.

Precision first: a rule only lists phrasings no factual question needs. "What is the
benchmark?" is not "best" (ADV-08), "How risky is it?" and "Is it high risk?" are facts (the
riskometer), "How are returns taxed?" is the page's tax rule, and a greeting that comes with a
question ("hi, what's the exit load?") is a question (OTH-02). What the rules miss is caught by
the LLM's own `status` (Phase 5) and validator checks V5/V6.
"""

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Intent(StrEnum):
    GREETING = "greeting"
    ADVISORY = "advisory"
    TRANSACTION = "transaction"
    PERFORMANCE = "performance"
    HISTORICAL = "historical"
    PERSONAL = "personal"
    FACTUAL = "factual"


@dataclass(frozen=True)
class Route:
    intent: Intent
    rule: str | None = None  # the rule that fired, for logs and tests

    @property
    def is_factual(self) -> bool:
        return self.intent is Intent.FACTUAL


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)


_GREETING = _rx(
    r"^\W*(?:hi+|hello+|hey+|hiya|namaste|namaskar|yo|good\s+(?:morning|afternoon|evening|day)"
    r"|thanks?(?:\s+(?:a\s+lot|so\s+much|you))?|thank\s+you(?:\s+so\s+much)?|thx|ty|ok(?:ay)?"
    r"|cool|great|nice|got\s+it|bye|goodbye|see\s+you)"
    r"(?:\W+(?:there|bot|team|again|all|everyone))*\W*$",
    r"^\W*(?:help|menu|start|about)\W*$",
    r"^\W*(?:what\s+(?:can|do)\s+you\s+(?:do|answer|help\s+with)|how\s+can\s+you\s+help"
    r"|what\s+can\s+i\s+ask(?:\s+you)?|who\s+are\s+you|what\s+are\s+you|what\s+is\s+this"
    r"|how\s+does\s+this\s+work|where\s+(?:does|do)\s+(?:your|the)\s+(?:data|information|facts?)"
    r"\s+come\s+from|what\s+(?:are\s+)?(?:your|the)\s+sources?|what\s+model\s+are\s+you"
    r"|(?:show|tell|reveal|print)\s+(?:me\s+)?(?:your|the)\s+(?:system\s+)?(?:prompt|instructions))"
    r"\W*$",
    r"^[\W_]*$",  # only emoji / punctuation (INP-03)
)
_PROFANITY = _rx(r"\b(?:fuck\w*|shit\w*|bastard|idiot|stupid|asshole|bitch\w*|damn)\b")

_INVEST_VERBS = (
    r"invest|buy|sell|redeem|switch|hold|exit|stay|continue|start|stop|choose|pick|go\s+for"
    r"|put|move|keep|add|increase|reduce|withdraw|enter|park|book\s+profits?|opt\s+for|prefer"
)
_ADVISORY = _rx(
    rf"\bshould\s+(?:i|we)\s+(?:\w+\s+){{0,2}}?(?:{_INVEST_VERBS})\b",
    r"\bshould\s+(?:i|we)\b[^?]*\b(?:or|instead)\b",  # "should I do X or Y"
    r"\b(?:is|are)\s+(?:it|this|they|these|hdfc\b[^?]*?)\s+(?:a\s+)?(?:good|wise|right|smart)\b",
    r"\bbetter\b",
    r"\bbest\b(?!\s+way\s+to\s+(?:contact|reach|call|get\s+in\s+touch))",
    r"\brecommend\w*|\bsuggest\w*|\badvi[sc]e\w*|\badvisor|\badviser",
    r"\bworth\s+(?:it|investing|buying|the\s+money|a\s+try|holding)\b|\bis\s+it\s+worth\b",
    r"\bgood\s+(?:fund|scheme|investment|choice|option|time|idea|buy|bet|pick|returns?)\b",
    r"\b(?:right|good|best|ideal)\s+time\s+to\b|\btime\s+to\s+(?:buy|invest|enter|exit|sell)\b",
    r"\bgood\s+for\s+(?:me|\d+\s+years?|long[\s-]term|short[\s-]term|retirement|beginners?)\b",
    r"\b(?:is|are)\b[^?]*\bsafe\b|\bsafe\s+(?:to|for|investment|bet|option)\b",
    r"\bsip\s+or\s+(?:a\s+)?lump\s*sum\b|\blump\s*sum\s+or\s+(?:a\s+)?sip\b",
    r"\bhow\s+much\s+(?:money\s+)?should\s+(?:i|we)\b",
    r"\bwhich\s+(?:one|fund|scheme|of\s+(?:these|them|the\s+(?:two|funds|schemes)))\b[^?]*"
    rf"\b(?:should|would|prefer|pick|choose|go\s+for|{_INVEST_VERBS})\b",
    r"\bwould\s+you\s+(?:pick|choose|recommend|buy|invest|suggest|prefer|go\s+for)\b",
    r"\bif\s+you\s+were\s+me\b|\b(?:in\s+)?your\s+opinion\b|\bdo\s+you\s+think\b"
    r"|\bwhat\s+do\s+you\s+think\b|\byour\s+view\b",
    r"\b(?:suitable|right|ideal)\s+for\s+(?:me|my)\b|\bfor\s+my\s+(?:goals?|retirement|child|"
    r"children|portfolio|risk\s+profile)\b",
    r"\bpredict\w*|\bforecast\w*|\bwill\s+(?:it|the\s+(?:nav|fund|price))\s+(?:go\s+up|go\s+down|"
    r"rise|fall|grow|increase|drop|crash|recover)\b",
    r"\bpretend\s+(?:you(?:'re|\s+are)|to\s+be)\b",
    # An opinion about a fact: "Is a 1% expense ratio high?" (ADV-05).
    r"\b(?:expense\s+ratio|ter|exit\s+load|fees?|charges?|costs?)\b[^?.]*\b(?:too\s+)?(?:high"
    r"|low|expensive|cheap|reasonable|fair|steep|competitive)\b",
    r"\b(?:high|low|expensive|cheap|reasonable)\b[^?.]*\b(?:expense\s+ratio|ter|exit\s+load)\b",
)
_TRADE = r"buy|purchase|redeem|sell|invest|place|book|execute|withdraw|transfer"
_TRANSACTION = _rx(
    # An order: "Buy ₹5,000 of Mid Cap", "Redeem my units", "Invest 5000 for me".
    rf"^\W*(?:please\s+)?(?:{_TRADE})\b(?:\s+\S+){{0,5}}?\s+(?:₹|rs\.?\s*\d|inr|\d|my\s+"
    r"(?:units|money|investment|holdings?)|all\s+(?:my\s+)?units|for\s+me\b|now\b|today\b)",
    r"^\W*(?:please\s+)?(?:start|stop|cancel|pause|set\s+up|modify|renew)\s+(?:a\s+|an\s+|my\s+)?"
    r"(?:new\s+)?(?:sip|swp|stp|mandate|order|investment)\b(?!\s+(?:date|amount|minimum)\b)",
    rf"\b(?:can|could|will|would)\s+you\s+(?:please\s+)?(?:{_TRADE}|start|stop|cancel|pause"
    r"|set\s+up)\b",
)
# "Returns" is performance unless the message is about tax: "How are returns taxed?" is the
# page's tax rule, and "income tax return" is out of corpus (layer C), not performance.
_RETURNS = re.compile(r"\breturns?\b", re.IGNORECASE)
_TAX = re.compile(r"\btax(?:ed|es|ation|able)?\b|\bitr\b|\bstcg\b|\bltcg\b", re.IGNORECASE)
_PERFORMANCE = _rx(
    r"\bcagr\b|\bxirr\b|\bannuali[sz]ed\b|\bperform(?:ance|ed|ing)?\b|\boutperform\w*"
    r"|\bunderperform\w*|\bbeat(?:s|en|ing)?\b|\brank(?:s|ed|ing)?\b|\bgrowth\s+of\b",
    r"\bhow\s+much\s+(?:will|would|did|could|can|do)\s+(?:i|it|my|we|you)\b[^?]*\b(?:get|earn"
    r"|make|grow|become|have|be\s+worth|gain)\b",
    r"\bwhat\s+(?:will|would)\b[^?]*\bbecome\b|\bhow\s+much\s+(?:has|did)\s+it\s+(?:grow|gain)\b",
    r"\bnav\s+(?:history|chart|trend|graph)\b|\b(?:historical|past|previous|old)\s+navs?\b"
    r"|\b52[\s-]week\b|\b(?:highest|lowest|all[\s-]time)\s+nav\b",
    r"\bmade\s+money\b|\bhow\s+much\s+profit\b|\bgained\b",
)
_MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_PAST = _rx(
    r"\b(?:last|previous|past|prior)\s+(?:year|month|week|quarter|fy|financial\s+year)\b",
    r"\b\d+\s+(?:years?|months?|weeks?|days?)\s+ago\b|\bback\s+in\b|\bused\s+to\b",
    r"\byesterday\b|\bearlier\s+this\s+year\b|\bat\s+(?:launch|inception)\b",
)
_YEAR = re.compile(
    r"\b(?:in|on|as\s+(?:of|on)|during|for|at|of)\s+(?:the\s+)?(?:(?:\d{1,2}"
    rf"(?:st|nd|rd|th)?\s+)?(?:{_MONTHS})\w*\.?,?\s+)?((?:19|20)\d{{2}})\b"
    r"|\b(?:fy|financial\s+year)\s*'?((?:19|20)?\d{2})\b",
    re.IGNORECASE,
)
_NAV_OR_AUM = _rx(
    r"\bnavs?\b|\bnet\s+asset\s+value\b|\baum\b|\bfund\s+size\b|\bunit\s+price\b"
    r"|\bprice\s+of\s+(?:one|a|each)\s+unit\b"
)
_PERSONAL = _rx(
    r"\b(?:show|check|see|view|get|fetch|tell\s+me)\s+(?:me\s+)?my\s+(?:holdings?|portfolio"
    r"|balance|units|sips?|investments?|account|folio|statement|transactions?|orders?)\b",
    r"\bmy\s+(?:sip|order|transaction|redemption|kyc|mandate|payment)\s+status\b",
    r"\bmy\s+(?:balance|portfolio|holdings|folio|current\s+value|investment\s+value)\b",
    r"\bhow\s+(?:much|many)\s+(?:units|money)\s+do\s+i\s+(?:have|hold|own)\b",
)


def _past_year(text: str, today: date) -> bool:
    for m in _YEAR.finditer(text):
        raw = m.group(1) or m.group(2)
        year = int(raw) if len(raw) == 4 else 2000 + int(raw)
        if year < today.year:
            return True
    return False


def route(text: str, *, today: date | None = None) -> Route:
    """The first rule that matches `text` (already PII-redacted), else `factual`."""
    today = today or date.today()
    if _GREETING.search(text):
        return Route(Intent.GREETING, "greeting")
    if _PROFANITY.search(text) and len(text.split()) <= 6:
        return Route(Intent.GREETING, "profanity")  # neutral help reply (OTH-04)
    if _ADVISORY.search(text):
        return Route(Intent.ADVISORY, "advisory")
    if _TRANSACTION.search(text):
        return Route(Intent.TRANSACTION, "transaction")
    if _PERFORMANCE.search(text) or (_RETURNS.search(text) and not _TAX.search(text)):
        return Route(Intent.PERFORMANCE, "performance")
    historical = _PAST.search(text) or _past_year(text, today)
    if historical and _NAV_OR_AUM.search(text):
        return Route(Intent.PERFORMANCE, "past_nav")  # NAV/AUM history is performance (PRF-03)
    if historical:
        return Route(Intent.HISTORICAL, "historical")
    if _PERSONAL.search(text):
        return Route(Intent.PERSONAL, "personal")
    return Route(Intent.FACTUAL)
