"""Post-generation checks V1-V7 on the LLM's `AnswerDraft` (P4.6, arch §6.7).

Each check is its own function and returns the failures it found (empty = pass); `validate()`
runs them all. A failure has a stable code and name, for the retry note sent back to the model
(P5.7) and for the eval backlog (logged without the text).

- V1 `citation`: a cited chunk wasn't sent in this request, its page isn't one of the 5 allowed
  URLs, or the extra citations (`also_cited_chunk_ids`) are on another page than the main one
  (the reply has exactly one link).
- V2 `sentence_count`: more than 3 sentences (each line / bullet counts).
- V3 `no_links`: a URL, `www.`, a bare or spelled-out domain, a markdown link or a "Source:"
  line.
- V4 `number_grounding`: a number, %, ₹ amount, duration or date that isn't in any of the
  cited chunks (headers included, VAL-06).
- V5 `advice_language`: advice wording ("you should", "recommend", "good choice"…), unless
  the sentence is quoted verbatim from the chunk (D-05).
- V6 `performance`: a return figure: the ingest patterns (P2.4), or a % in a sentence about
  returns that isn't about tax.
- V7 `consistency`: `answered` with no text or no citation; any other status with text.

V1-V6 only look at `answered` drafts: the other statuses carry no answer to check (V7).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from mf_faq.ingest.chunk import Chunk
from mf_faq.ingest.registry import is_allowed_url
from mf_faq.ingest.strip_performance import find_performance_patterns
from mf_faq.pipeline.draft import AnswerDraft
from mf_faq.pipeline.text import count_sentences, split_sentences, ungrounded_numbers

MAX_SENTENCES = 3


@dataclass(frozen=True)
class Failure:
    code: str  # "V1" … "V7"
    name: str
    detail: str


@dataclass(frozen=True)
class ValidationResult:
    failures: tuple[Failure, ...]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(f.code for f in self.failures))


def _cited(draft: AnswerDraft, chunks: Sequence[Chunk]) -> Chunk | None:
    return next((c for c in chunks if c.chunk_id == draft.cited_chunk_id), None)


def cited_chunks(draft: AnswerDraft, chunks: Sequence[Chunk]) -> list[Chunk]:
    """The main cited chunk first, then the extra ones; ids that weren't sent are skipped."""
    by_id = {c.chunk_id: c for c in chunks}
    ids = dict.fromkeys((draft.cited_chunk_id, *draft.also_cited_chunk_ids))
    return [by_id[i] for i in ids if i in by_id]


def check_citation(
    draft: AnswerDraft, chunks: Sequence[Chunk], source_urls: Mapping[str, str]
) -> list[Failure]:
    """V1: every citation is one of this request's chunks, all from one allowed page."""
    chunk = _cited(draft, chunks)
    if chunk is None:
        return [Failure("V1", "citation", f"cited {draft.cited_chunk_id!r} was not sent")]
    url = source_urls.get(chunk.source_id)
    if url is None or not is_allowed_url(url):
        return [Failure("V1", "citation", f"{chunk.source_id} is not an allowed page")]
    sent = {c.chunk_id: c for c in chunks}
    failures = []
    for extra in draft.also_cited_chunk_ids:
        if extra not in sent:
            failures.append(Failure("V1", "citation", f"cited {extra!r} was not sent"))
        elif sent[extra].source_id != chunk.source_id:
            failures.append(
                Failure("V1", "citation", f"{extra} is on another page than {chunk.chunk_id}")
            )
    return failures


def check_sentence_count(answer: str) -> list[Failure]:
    """V2: at most 3 sentences (R-1)."""
    n = count_sentences(answer)
    if n > MAX_SENTENCES:
        return [Failure("V2", "sentence_count", f"{n} sentences")]
    return []


_LINK_RE = re.compile(
    r"https?://|\bwww\.|\[[^\]]*\]\([^)]*\)"  # scheme, www., markdown link
    r"|\b[\w-]+\.(?:in|com|org|net|co|io|gov|nic|info|app)\b(?!\.\d)"  # bare domain
    r"|\b[\w-]+\s*(?:\(|\[)?\s*dot\s*(?:\)|\])?\s*(?:in|com|org|net)\b"  # "groww dot in"
    r"|^\s*(?:source|sources|link|reference|last updated)\b\s*:",  # a line of its own
    re.IGNORECASE | re.MULTILINE,
)


def check_no_links(answer: str) -> list[Failure]:
    """V3: no link of any kind; the composer adds the only one (R-2)."""
    found = [m.group(0) for m in _LINK_RE.finditer(answer)]
    return [Failure("V3", "no_links", f"link-like text: {found}")] if found else []


def check_number_grounding(answer: str, *cited: Chunk) -> list[Failure]:
    """V4: every figure in the answer is in one of the cited chunks, headers included (VAL-06)."""
    missing = ungrounded_numbers(answer, "\n".join(c.content for c in cited))
    ids = ", ".join(c.chunk_id for c in cited)
    return [Failure("V4", "number_grounding", f"not in {ids}: {missing}")] if missing else []


_ADVICE_RE = re.compile(
    r"\b(?:recommend\w*|suggest\w*|you\s+should|you\s+must|you\s+may\s+want\s+to|i\s+would"
    r"|i'd|consider\s+(?:investing|buying|switching|adding)|good\s+(?:choice|option|fund|"
    r"investment|bet|pick)|great\s+(?:choice|option|fund|investment)|better\s+(?:than|choice|"
    r"option)|best\s+(?:fund|choice|option|investment)|ideal\s+for|suitable\s+for\s+you"
    r"|worth\s+(?:investing|buying|it)|safe\s+bet|go\s+for\s+it|should\s+invest|buy\s+now"
    r"|strong\s+performer|outperform\w*|attractive|undervalued|overvalued)\b",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(r"\s+")


def _squash(text: str) -> str:
    return _SPACE_RE.sub(" ", text).strip().lower().rstrip(".!?\"'” ")


def check_advice_language(answer: str, chunk: Chunk | None = None) -> list[Failure]:
    """V5: no advice wording (CR-1). A sentence copied verbatim from the chunk is a quoted
    product label ("suitable for investors seeking …"), not advice (D-05)."""
    source = _squash(chunk.content) if chunk else ""
    hits = []
    for sentence in split_sentences(answer):
        found = [m.group(0) for m in _ADVICE_RE.finditer(sentence)]
        if found and not (source and _squash(sentence) in source):
            hits += found
    return [Failure("V5", "advice_language", f"advice wording: {hits}")] if hits else []


# Not "growth": every scheme is "Direct Growth", so that word says nothing about returns.
_RETURN_WORD_RE = re.compile(r"\b(?:returns?|yield\w*|grew|gains?)\b", re.IGNORECASE)
_PERCENT_RE = re.compile(r"\d\s*%|\bper\s?cent\b|\bpercent\b", re.IGNORECASE)
_TAX_RE = re.compile(r"\btax(?:ed|es|ation|able)?\b|\bstcg\b|\bltcg\b", re.IGNORECASE)


def check_performance(answer: str) -> list[Failure]:
    """V6: no return or performance figure (CR-2, CR-3).

    The ingest patterns (P2.4) catch CAGR, "returned 15%", "3Y" and the like; on top, any
    sentence with a % and a return word fails unless it is about tax ("returns are taxed at
    20%" is the page's rule). So "the 0.7% expense ratio reduces returns" fails: an accepted
    false positive (PRF-07).
    """
    hits = find_performance_patterns(answer)
    for sentence in split_sentences(answer):
        if (
            _PERCENT_RE.search(sentence)
            and _RETURN_WORD_RE.search(sentence)
            and not _TAX_RE.search(sentence)
        ):
            hits.append(f"percent_with_return: {sentence}")
    return [Failure("V6", "performance", f"return figures: {hits}")] if hits else []


def check_consistency(draft: AnswerDraft) -> list[Failure]:
    """V7: the status and the other fields agree (LLM-05)."""
    has_text = bool(draft.answer.strip())
    if draft.status == "answered":
        problems = [] if has_text else ["answered with an empty answer"]
        if not draft.cited_chunk_id:
            problems.append("answered without a citation")
    else:
        problems = [f"{draft.status} with answer text"] if has_text else []
        if draft.also_cited_chunk_ids:
            problems.append(f"{draft.status} with citations")
    return [Failure("V7", "consistency", p) for p in problems]


def validate(
    draft: AnswerDraft, chunks: Sequence[Chunk], source_urls: Mapping[str, str]
) -> ValidationResult:
    """All checks; V1-V6 only for an answered draft that passes V7's basic shape."""
    failures = check_consistency(draft)
    if draft.status == "answered" and not failures:
        failures += check_citation(draft, chunks, source_urls)
        failures += check_sentence_count(draft.answer)
        failures += check_no_links(draft.answer)
        chunk = _cited(draft, chunks)
        if chunk is not None:
            failures += check_number_grounding(draft.answer, *cited_chunks(draft, chunks))
        failures += check_advice_language(draft.answer, chunk)
        failures += check_performance(draft.answer)
    return ValidationResult(tuple(failures))
