"""Scheme resolution and thread follow-up carry-over (P3.2, arch §6.4).

`SchemeResolver(catalog).resolve(text, active_scheme_id)` is a pure function of its inputs: it
returns 0, 1 or several scheme ids plus the reason. The order of the checks matters:

1. **Other fund house named** ("SBI Small Cap Fund", "Kotak MF") → `other_amc`. It runs first
   because generic aliases like "small cap fund" score 100 against other AMCs' schemes
   (research §3.7). A name counts only with a fund word right after it and no company word
   ("ICICI Bank", "SBI Funds Management", "Tata Motors" are holdings, so they're answerable).
2. **Name matching**: the supported aliases and the `not_in_scope` HDFC schemes are matched in
   one pass (rapidfuzz ratio ≥ 85 over word windows, so typos like "hdfc smal cap" match), and
   the longest matched span wins an overlap. So "HDFC Silver ETF FoF" beats the deny-listed
   "HDFC Silver ETF", and "HDFC Large & Mid Cap Fund" beats "mid cap". Any deny-listed match →
   `not_in_scope`, unless it is part of a named scheme's own name ("… FoF is in the Silver ETF").
   Otherwise every scheme named → `matched` (several = the multi-scheme reply).
3. **Follow-up carry-over**: no scheme named, but the text refers back ("its", "this fund",
   "what about …") or names only a fact ("exit load?") → the thread's active scheme
   (`carry_over`), or `needs_scheme` when the thread has none (D-09). Words that point at some
   other or general fund ("ELSS", "mutual funds", "flexi cap", a bare "SBI") stop the carry-over.

Anything else is `none`: a general question, answered with `not_found` and no retrieval (P3.4 B).
"""

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from rapidfuzz import fuzz

from mf_faq.ingest.registry import SchemeCatalog

FUZZY_CUTOFF = 85


def _wordset(text: str) -> frozenset[str]:
    return frozenset(text.split())


# Words after an AMC name that make it a fund house ("SBI Small Cap", "Kotak MF")...
FUND_WORDS = _wordset(
    "fund funds mf mfs mutual amc scheme schemes etf etfs fof fofs cap caps large mid small "
    "multi flexi largecap midcap smallcap multicap flexicap bluechip elss index liquid debt "
    "hybrid equity gilt arbitrage overnight contra value focused tax saver nifty sensex gold "
    "silver"
)
# ...and words that make it a company, as in the holdings lists ("ICICI Bank Ltd").
COMPANY_WORDS = _wordset(
    "ltd limited bank banks finance financial services motors steel communications consumer "
    "products industries insurance life lombard capital healthcare auto automotive electricals "
    "corporation company management securities cards power housing transport vision care "
    "telecom chemicals cement enterprises"
)
_AMC_CONTEXT = 3  # words looked at after an AMC name

_ZERO_WIDTH_RE = re.compile("[­​-‏⁠﻿]")
_WORD_RE = re.compile(r"[a-z0-9]+")
_DROP_WORDS = frozenset({"and"})  # "Large & Mid Cap" = "Large and Mid Cap" = "Large Mid Cap"

# Refers back to the thread's scheme: "its exit load", "this fund", "and the benchmark?".
_REFERENCE_RE = re.compile(
    r"\b(it|its|itself|this|that|these|those|same|they|them|their|also|too)\b"
    r"|^\W*(and|what about|how about|what of)\b|\bthe (fund|scheme)\b"
)
# Names only a fact the pages answer: "exit load?", "minimum SIP", "who manages".
_FACT_RE = re.compile(
    r"\b(expense|ter|exit|load|sip|sips|lump ?sum|minimum|min|riskometer|risk|risky|benchmark"
    r"|managers?|manages|managed|manage|nav|aum|size|assets under management|category|objective"
    r"|stamp duty|tax|taxed|taxation|holdings?|holds?|stocks?|shares?|portfolio|sectors?"
    r"|fund house|amc|registrar|rta|custodian|contact|phone|address)\b"
)
# Points at some other or general fund, so the thread's scheme must not carry over (FUP-06).
_GENERAL_RE = re.compile(
    r"\b(elss|(?<!hdfc )mutual funds?|funds(?! management)|schemes"
    r"|(index|liquid|debt|hybrid|gold) funds?|flexi ?cap|caps?"
    r"|other (funds?|schemes?|amcs?|fund houses?))\b"
)
# Asks what the thread's scheme holds: "How much HDFC Silver ETF does it hold?".
_HOLDS_RE = re.compile(
    r"\b(it|its|this fund|the fund)\b.*\b(hold\w*|invest\w*|portfolio|weight\w*|exposure)\b"
    r"|\b(hold\w*|invest\w*|portfolio|weight\w*|exposure)\b.*\b(it|its|this fund|the fund)\b"
)
# "And the other one?": which one is unclear (FUP-04, D-09).
_OTHER_ONE_RE = re.compile(r"\b(the )?other (one|fund|scheme)\b|\bthe other\b")
# "Exit load of all 5 funds" (MUL-03).
_ALL_SCHEMES_RE = re.compile(
    r"\b(all|each|every)( of)?( the| these| those| your| 5| five| hdfc)* (funds?|schemes?)\b"
    r"|\ball (of )?(them|5|five)\b"
)
# "Compare it with Small Cap" (MUL-04): the named scheme and the thread's scheme.
_COMPARE_RE = re.compile(r"\b(compare|compared|comparison|vs|versus|against|than|differ\w*)\b")


class Reason(StrEnum):
    MATCHED = "matched"  # 1+ supported schemes named in the text
    CARRY_OVER = "carry_over"  # follow-up: the thread's active scheme
    OTHER_AMC = "other_amc"  # another fund house's scheme: out of scope
    NOT_IN_SCOPE = "not_in_scope"  # another HDFC scheme: out of scope
    NEEDS_SCHEME = "needs_scheme"  # scheme-specific, but which one is unknown (D-09)
    NONE = "none"  # general question, no scheme


@dataclass(frozen=True)
class SchemeResolution:
    scheme_ids: tuple[str, ...]
    reason: Reason
    matched: tuple[str, ...] = ()  # the names that decided it (for logs and eval)

    @property
    def out_of_scope(self) -> bool:
        return self.reason in (Reason.OTHER_AMC, Reason.NOT_IN_SCOPE)

    @property
    def scheme_id(self) -> str | None:
        """The one scheme to retrieve from, or None (0 or several: no retrieval, P3.3)."""
        return self.scheme_ids[0] if len(self.scheme_ids) == 1 else None


@dataclass(frozen=True)
class _Name:
    text: str  # normalised, space-joined words
    words: int
    scheme_id: str | None  # None = a not_in_scope HDFC scheme
    label: str  # as written in schemes.yaml


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    score: float
    name: _Name


def normalize(text: str) -> str:
    """NFKC, lower case, no zero-width characters, no possessive 's (INP-05)."""
    text = _ZERO_WIDTH_RE.sub("", unicodedata.normalize("NFKC", text)).lower()
    return re.sub(r"'s\b", "", text.replace("’", "'"))


def words(normalized: str) -> list[str]:
    return [w for w in _WORD_RE.findall(normalized) if w not in _DROP_WORDS]


def _deny_variants(name: str) -> set[str]:
    """How people write a deny-listed scheme: with or without "HDFC" and the final "Fund"."""
    full = words(normalize(name))
    out = {" ".join(full)}
    if full[-1] == "fund" and full[-2:] != ["of", "fund"] and len(full) > 2:
        out.add(" ".join(full[:-1]))  # "hdfc flexi cap"
    if full[0] == "hdfc":
        bare = full[1:]
        # "flexi cap fund", "silver etf"; but never a lone word like "liquid" or "value", nor
        # "long term" (that is the tax rule), so short bare names need a fund-type last word.
        if len(bare) >= 3 or (len(bare) == 2 and bare[-1] in {"etf", "fof"}):
            out.add(" ".join(bare))
    return out


class SchemeResolver:
    def __init__(self, catalog: SchemeCatalog) -> None:
        self._scheme_ids = tuple(s.scheme_id for s in catalog.schemes)
        names = [
            _Name(" ".join(w), len(w), s.scheme_id, alias)
            for s in catalog.schemes
            for alias in {*s.aliases, s.short_name, s.name}
            if (w := words(normalize(alias)))
        ]
        names += [
            _Name(variant, len(variant.split()), None, deny)
            for deny in catalog.not_in_scope
            for variant in _deny_variants(deny)
        ]
        self._names = names
        self._full_names = {
            s.scheme_id: " ".join(words(normalize(s.name))) for s in catalog.schemes
        }
        amcs = [(n, True) for n in catalog.other_amcs.names]
        amcs += [(n, False) for n in catalog.other_amcs.need_fund_word]
        # Longest first, so "ICICI Prudential" is consumed before "ICICI".
        self._amcs = sorted(
            ((tuple(words(normalize(n))), n, strong) for n, strong in amcs),
            key=lambda a: -len(a[0]),
        )

    def resolve(self, text: str, active_scheme_id: str | None = None) -> SchemeResolution:
        norm = normalize(text)
        toks = words(norm)
        active = active_scheme_id if active_scheme_id in self._scheme_ids else None

        # 1. Another fund house's scheme.
        amc_hits, bare_amcs = self._amcs_named(toks)
        if amc_hits:
            return SchemeResolution((), Reason.OTHER_AMC, tuple(amc_hits))

        # 2. Supported and deny-listed scheme names, longest span first.
        matches = self._match_names(toks)
        named = tuple(dict.fromkeys(m.name.scheme_id for m in matches if m.name.scheme_id))
        # A deny-listed name inside the scheme's own name is what that scheme holds: "What % of
        # HDFC Silver ETF FoF is in the Silver ETF?", or "How much HDFC Silver ETF does it hold?"
        # in a Silver FoF thread.
        holder = named or ((active,) if active and _HOLDS_RE.search(norm) else ())
        denied = tuple(
            m.name.label
            for m in matches
            if m.name.scheme_id is None
            and not any(m.name.text in self._full_names[sid] for sid in holder)
        )
        if denied:
            return SchemeResolution((), Reason.NOT_IN_SCOPE, denied)
        labels = tuple(m.name.label for m in matches)
        if named:
            refers_back = _REFERENCE_RE.search(norm) and _COMPARE_RE.search(norm)
            if len(named) == 1 and active and active != named[0] and refers_back:
                return SchemeResolution((active, named[0]), Reason.MATCHED, labels)
            return SchemeResolution(named, Reason.MATCHED, labels)
        if _ALL_SCHEMES_RE.search(norm):
            return SchemeResolution(self._scheme_ids, Reason.MATCHED)

        # 3. Follow-up carry-over.
        if _OTHER_ONE_RE.search(norm):
            return SchemeResolution((), Reason.NEEDS_SCHEME)
        if bare_amcs or _GENERAL_RE.search(norm):
            return SchemeResolution((), Reason.NONE, tuple(bare_amcs))
        if _REFERENCE_RE.search(norm) or _FACT_RE.search(norm):
            if active:
                return SchemeResolution((active,), Reason.CARRY_OVER)
            return SchemeResolution((), Reason.NEEDS_SCHEME)
        return SchemeResolution((), Reason.NONE)

    def _amcs_named(self, toks: list[str]) -> tuple[list[str], list[str]]:
        """(fund houses named as such, strong names mentioned bare like "exit load of SBI")."""
        hits: list[str] = []
        bare: list[str] = []
        i = 0
        while i < len(toks):
            for name_toks, label, strong in self._amcs:
                end = i + len(name_toks)
                if tuple(toks[i:end]) != name_toks:
                    continue
                after = toks[end : end + _AMC_CONTEXT]
                if "hdfc" in after:
                    after = after[: after.index("hdfc")]
                company = any(w in COMPANY_WORDS for w in after)
                fund = any(w in FUND_WORDS for w in after)
                if fund and not company:
                    hits.append(label)
                elif strong and not company:
                    bare.append(label)
                i = end
                break
            else:
                i += 1
        return hits, bare

    def _match_names(self, toks: list[str]) -> list[_Match]:
        windows: dict[int, list[str]] = {}
        found: list[_Match] = []
        for name in self._names:
            for size in (name.words - 1, name.words, name.words + 1):
                if size < 1 or size > len(toks):
                    continue
                if size not in windows:
                    windows[size] = [
                        " ".join(toks[i : i + size]) for i in range(len(toks) - size + 1)
                    ]
                for i, window in enumerate(windows[size]):
                    score = fuzz.ratio(name.text, window, score_cutoff=FUZZY_CUTOFF)
                    if score:
                        found.append(_Match(i, i + size, score, name))
        # Longest span first, then the closer match, then the longer name.
        found.sort(key=lambda m: (m.start - m.end, -m.score, -len(m.name.text)))
        taken: list[_Match] = []
        used: set[int] = set()
        for m in found:
            span = range(m.start, m.end)
            if used.isdisjoint(span):
                taken.append(m)
                used.update(span)
        return sorted(taken, key=lambda m: m.start)
