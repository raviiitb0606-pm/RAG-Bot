"""Build the final reply: answer + one link + footer from the registry (P4.7, arch §6.8).

The final message is assembled **only** here:

    {body}

    Source: {url of the cited / linked page}
    Last updated from sources: {that page's fetched_at, DD MMM YYYY in IST}

`body` is the validated LLM answer (`kind = answer`, link = the cited chunk's page) or a fixed
template (`templates.py`, link = the link rule). The URL and date come from the registry (the
live index's manifest), never from the LLM. The structured fields are returned too, so the UI
can render the link as an anchor.

Before returning, the composer checks its own output: at most 3 sentences, exactly one URL and
it is one of the 5 allowed pages, and the footer line. A failure is a bug, not a user error, so
it raises `ComposeError` (the caller answers with the `error` template).
"""

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from mf_faq.ingest.chunk import Chunk
from mf_faq.ingest.registry import SchemeCatalog, is_allowed_url
from mf_faq.pipeline.sources import SourceEntry, SourceRegistry
from mf_faq.pipeline.templates import Kind, Templates, pick_source
from mf_faq.pipeline.text import count_sentences

FOOTER_PREFIX = "Last updated from sources: "
FOOTER_RE = re.compile(r"^Last updated from sources: \d{2} [A-Z][a-z]{2} \d{4}$")
URL_RE = re.compile(r"https?://\S+")
MAX_SENTENCES = 3


class ComposeError(RuntimeError):
    """The composed reply breaks the response contract (R-1…R-3); a bug, never shown as-is."""


@dataclass(frozen=True)
class Reply:
    kind: Kind
    answer: str  # the body alone (no link, no footer)
    source_id: str
    source_url: str
    source_title: str
    last_updated: str  # DD MMM YYYY
    text: str  # body + link + footer, as shown in plain-text clients

    def to_dict(self) -> dict:
        return {**asdict(self), "kind": self.kind.value}


class Composer:
    def __init__(
        self, registry: SourceRegistry, catalog: SchemeCatalog, default_source_id: str
    ) -> None:
        if default_source_id not in registry:  # OPS-02: fail at startup, not on a reply
            raise ValueError(
                f"MF_FAQ_DEFAULT_SOURCE_ID={default_source_id!r} is not one of the 5 sources: "
                f"{sorted(e.id for e in registry)}"
            )
        self.registry = registry
        self._registry = registry
        self._templates = Templates(catalog)
        self._default = default_source_id

    def answer(self, answer: str, cited: Chunk) -> Reply:
        """A validated answer, linked to the page of the chunk it cites."""
        return self._build(Kind.ANSWER, answer.strip(), self._registry.get(cited.source_id))

    def template(
        self,
        kind: Kind,
        *,
        variant: str | None = None,
        scheme_ids: Sequence[str] = (),
        active_scheme_id: str | None = None,
        best_source_id: str | None = None,
    ) -> Reply:
        """A fixed reply; its link follows the link rule (arch §7)."""
        body = self._templates.body(kind, variant=variant, scheme_ids=scheme_ids)
        source = pick_source(
            self._registry,
            default_source_id=self._default,
            scheme_ids=scheme_ids,
            active_scheme_id=active_scheme_id,
            best_source_id=best_source_id,
        )
        return self._build(kind, body, source)

    def _build(self, kind: Kind, body: str, source: SourceEntry) -> Reply:
        footer = FOOTER_PREFIX + source.last_updated
        text = f"{body}\n\nSource: {source.url}\n{footer}"
        reply = Reply(kind, body, source.id, source.url, source.title, source.last_updated, text)
        _check(reply)
        return reply


def _check(reply: Reply) -> None:
    problems = []
    if not reply.answer:
        problems.append("empty body")
    if count_sentences(reply.answer) > MAX_SENTENCES:
        problems.append(f"{count_sentences(reply.answer)} sentences")
    urls = URL_RE.findall(reply.text)
    if urls != [reply.source_url] or not is_allowed_url(reply.source_url):
        problems.append(f"links {urls}, expected exactly one allowed URL")
    if not FOOTER_RE.match(reply.text.splitlines()[-1]):
        problems.append("footer missing or malformed")
    if problems:
        raise ComposeError(f"{reply.kind.value} reply breaks R-1…R-3: " + "; ".join(problems))
