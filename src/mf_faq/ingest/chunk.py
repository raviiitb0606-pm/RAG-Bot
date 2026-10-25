"""One chunk per fact group, with context headers and metadata (P2.5, P3.0, arch §5.4).

`Chunk.content` is what gets embedded and BM25-indexed:

    [Scheme: HDFC Mid Cap Fund | Source: Groww scheme page | Section: … > Exit load]
    Covers: exit load, redemption charge, penalty or fee for withdrawing or selling units early.
    Exit load of 1% if redeemed within 1 year.

The "Covers:" line is fixed per fact group (`schemes.yaml` → `fact_groups`) and never holds a
value: it describes the section in users' words, which closes the vocabulary gap between
questions and section labels (research §3.2).

A chunk whose content is over `max_tokens` (in practice only holdings, PAR-12) is split on line
boundaries. Tokens are counted with the **embedding model's own tokenizer** when one is given
(the regex estimate undercounts WordPiece by ~15%, so "599 tokens" was really 694 and got
truncated at 512, research §3.1). Every part keeps the header and the covers line and repeats
the section's first line, e.g. "Portfolio holdings as of 30 Sep 2026 (123 holdings, …):", so
each part stands alone.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Protocol

from mf_faq.ingest.parse_html import Section
from mf_faq.ingest.registry import Scheme, Source

MAX_TOKENS = 400  # per chunk, header and covers line included (MF_FAQ_HOLDINGS_MAX_TOKENS)
SOURCE_LABEL = "Groww scheme page"
# Bump whenever `Chunk.content` changes shape: the index records it and refuses a mismatch (P3.1).
CHUNK_FORMAT_VERSION = "2"  # header + covers + text; split by the model's tokenizer

# Rough WordPiece-like count: words, number pieces and punctuation each count as one.
_TOKEN_RE = re.compile(r"\w+|[^\w\s]")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


class ChunkError(ValueError):
    """The sections can't be chunked (e.g. a fact group without a covers line)."""


class Tokenizer(Protocol):
    """Counts tokens the way the embedding model sees them. `ingest/index.py`'s Embedder is one."""

    model_name: str
    max_seq_length: int

    def count_tokens(self, text: str) -> int: ...


class ApproxTokenizer:
    """Regex estimate, for tests and tools that don't load the model. Undercounts by ~15%."""

    model_name = "approx"
    max_seq_length = 512

    def count_tokens(self, text: str) -> int:
        return approx_tokens(text)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    source_id: str
    scheme_id: str
    doc_type: str
    publisher: str
    heading_path: str
    facts: tuple[str, ...]
    header: str
    covers: str
    text: str

    @property
    def covers_line(self) -> str:
        return f"Covers: {self.covers}."

    @property
    def content(self) -> str:
        """What is embedded (P3.1) and, minus the header, BM25-indexed (P3.3)."""
        return f"{self.header}\n{self.covers_line}\n{self.text}"

    @property
    def group(self) -> str:
        return self.chunk_id.split(":")[1]

    def to_dict(self) -> dict:
        return {**asdict(self), "facts": list(self.facts)}


def approx_tokens(text: str) -> int:
    return len(_TOKEN_RE.findall(text))


def context_header(scheme: Scheme, heading_path: str) -> str:
    return f"[Scheme: {scheme.short_name} | Source: {SOURCE_LABEL} | Section: {heading_path}]"


def _split(lead: str, body: list[str], fits: Callable[[list[str]], bool]) -> list[str]:
    """Pack `body` lines greedily into parts that `fits`, each part starting with `lead`."""
    lines: list[str] = []
    for line in body:  # a line that can't fit on its own is split into sentences first
        lines.extend([line] if fits([line]) else _SENTENCE_RE.split(line))
    parts: list[list[str]] = [[]]
    for line in lines:
        if parts[-1] and not fits([*parts[-1], line]):
            parts.append([])
        parts[-1].append(line)
    return ["\n".join([lead, *p]) if lead else "\n".join(p) for p in parts]


def chunk_page(
    sections: list[Section],
    source: Source,
    scheme: Scheme,
    covers: Mapping[str, str],
    *,
    tokenizer: Tokenizer | None = None,
    max_tokens: int = MAX_TOKENS,
) -> list[Chunk]:
    tokenizer = tokenizer or ApproxTokenizer()
    chunks: list[Chunk] = []
    for section in sections:
        if section.group not in covers:
            raise ChunkError(f"fact group {section.group!r} has no covers line in schemes.yaml")
        heading = " > ".join(section.heading_path)
        header = context_header(scheme, heading)
        prefix = f"{header}\nCovers: {covers[section.group]}.\n"
        first, *rest = section.text.split("\n")
        lead = first if rest else ""

        def fits(lines: list[str], prefix: str = prefix, lead: str = lead) -> bool:
            text = "\n".join([lead, *lines] if lead else lines)
            return tokenizer.count_tokens(prefix + text) <= max_tokens

        if tokenizer.count_tokens(prefix + section.text) <= max_tokens:
            texts = [section.text]
        else:
            texts = _split(lead, rest if rest else [first], fits)
        for n, text in enumerate(texts, start=1):
            suffix = f":{n}" if len(texts) > 1 else ""
            chunks.append(
                Chunk(
                    chunk_id=f"{source.id}:{section.group}{suffix}",
                    source_id=source.id,
                    scheme_id=scheme.scheme_id,
                    doc_type=source.doc_type,
                    publisher=source.publisher,
                    heading_path=heading,
                    facts=section.facts,
                    header=header,
                    covers=covers[section.group],
                    text=text,
                )
            )
    return chunks
