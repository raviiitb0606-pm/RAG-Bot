"""Fixed reply templates and the link rule (P4.3, arch §7).

Every body is at most 3 sentences and holds no link: the composer (P4.7) adds the one link and
the footer, so every reply has the same shape (R-1…R-3). Bodies name schemes and facts from
`schemes.yaml`, never values, so they can't go stale.

Kinds: `answer` (the validated LLM answer), `refusal`, `performance`, `pii`, `not_found`,
`multi_scheme`, `clarify`, `help`, `error`. `clarify` is decision D-09 (a follow-up with no
scheme to resolve, "the other one"). Two variants change only the wording: `refusal` /
`transaction` (D-08: "I can't place transactions") and `not_found` / `out_of_scope` (another
fund house or HDFC scheme: name the 5 that are covered).

**Link rule:** the Groww page of the scheme named in the message (the first one, if several),
else the thread's `active_scheme_id`, else `MF_FAQ_DEFAULT_SOURCE_ID`. A not-found reply after
retrieval links the best-ranked page instead. Every link is one of the 5 pages.
"""

from collections.abc import Sequence
from enum import StrEnum

from mf_faq.ingest.registry import SchemeCatalog
from mf_faq.pipeline.retrieve import NotFound, Status
from mf_faq.pipeline.router import Intent
from mf_faq.pipeline.sources import SourceEntry, SourceRegistry


class Kind(StrEnum):
    ANSWER = "answer"
    REFUSAL = "refusal"
    PERFORMANCE = "performance"
    PII = "pii"
    NOT_FOUND = "not_found"
    MULTI_SCHEME = "multi_scheme"
    CLARIFY = "clarify"
    HELP = "help"
    ERROR = "error"


TRANSACTION = "transaction"  # refusal variant (D-08)
OUT_OF_SCOPE = "out_of_scope"  # not_found variant
BUSY = "busy"  # error variant: the model's rate limits are spent (P5.6)

# Facts named in the not-found and help replies: the common ones, all on every page.
LISTED_FACTS = (
    "expense_ratio",
    "exit_load",
    "min_sip",
    "risk_level",
    "benchmark",
    "fund_managers",
    "fund_size",
    "nav",
)

_BODIES: dict[tuple[Kind, str | None], str] = {
    (Kind.REFUSAL, None): (
        "I can only share factual information about these mutual fund schemes, so I can't "
        "advise on whether to invest or which fund is better. You can review the scheme's "
        "details at the link below."
    ),
    (Kind.REFUSAL, TRANSACTION): (
        "I can't place transactions or start, stop or change investments; I can only share "
        "factual information about these mutual fund schemes. You can review the scheme's "
        "details at the link below."
    ),
    (Kind.PERFORMANCE, None): (
        "I can't share or compare returns; the scheme's page below shows its latest "
        "performance details."
    ),
    (Kind.PII, None): (
        "For your safety, please don't share personal details such as PAN, Aadhaar, account "
        "numbers, OTPs, email or phone numbers. I've not stored them. I can help with factual "
        "questions about the supported schemes."
    ),
    (Kind.NOT_FOUND, None): (
        "I couldn't find that in the scheme pages I use. I can answer questions about {facts} "
        "for the 5 supported schemes."
    ),
    (Kind.NOT_FOUND, OUT_OF_SCOPE): (
        "I only cover 5 HDFC Mutual Fund schemes ({schemes}), so I couldn't find that in the "
        "scheme pages I use. I can answer questions about their {facts}."
    ),
    (Kind.MULTI_SCHEME, None): (
        "I can answer for one scheme at a time — which of these would you like: {schemes}?"
    ),
    (Kind.CLARIFY, None): (
        "Which scheme do you mean: {schemes}? I can answer for one scheme at a time."
    ),
    (Kind.HELP, None): (
        "I answer factual questions about 5 HDFC Mutual Fund schemes ({schemes}) from their "
        "Groww scheme pages, such as their {facts}. Facts only — no investment advice."
    ),
    (Kind.ERROR, None): "Something went wrong on my side. Please try again in a moment.",
    (Kind.ERROR, BUSY): (
        "I'm answering a lot of questions right now. Please try again in a minute."
    ),
}


def join_names(items: Sequence[str]) -> str:
    """ "A", "A and B", "A, B and C"."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


class Templates:
    def __init__(self, catalog: SchemeCatalog) -> None:
        self._names = {s.scheme_id: s.short_name for s in catalog.schemes}
        self._facts = join_names([catalog.fact_labels[f] for f in LISTED_FACTS])

    def body(
        self, kind: Kind, *, variant: str | None = None, scheme_ids: Sequence[str] = ()
    ) -> str:
        """The fixed text for `kind`. `scheme_ids` lists the schemes a multi-scheme reply offers."""
        if kind is Kind.ANSWER:
            raise ValueError("an answer's body is the validated LLM answer, not a template")
        template = _BODIES.get((kind, variant))
        if template is None:
            raise ValueError(f"no template for {kind.value!r} / {variant!r}")
        offered = [sid for sid in scheme_ids if sid in self._names]
        if kind is not Kind.MULTI_SCHEME or len(offered) < 2:
            offered = list(self._names)
        return template.format(
            schemes=join_names([self._names[sid] for sid in offered]), facts=self._facts
        )


def kind_for_intent(intent: Intent) -> tuple[Kind, str | None] | None:
    """The reply for a router hit, or None for `factual` (go on to retrieval)."""
    return {
        Intent.GREETING: (Kind.HELP, None),
        Intent.ADVISORY: (Kind.REFUSAL, None),
        Intent.TRANSACTION: (Kind.REFUSAL, TRANSACTION),
        Intent.PERFORMANCE: (Kind.PERFORMANCE, None),
        Intent.HISTORICAL: (Kind.NOT_FOUND, None),
        Intent.PERSONAL: (Kind.NOT_FOUND, None),
    }.get(intent)


def kind_for_evidence(status: Status, reason: NotFound | None) -> tuple[Kind, str | None] | None:
    """The reply for a retrieval outcome, or None when chunks go on to the LLM."""
    if status is Status.OK:
        return None
    if status is Status.NEEDS_SCHEME:
        return Kind.CLARIFY, None
    if status is Status.MULTI_SCHEME:
        return Kind.MULTI_SCHEME, None
    return Kind.NOT_FOUND, OUT_OF_SCOPE if reason is NotFound.OUT_OF_SCOPE else None


def pick_source(
    registry: SourceRegistry,
    *,
    default_source_id: str,
    scheme_ids: Sequence[str] = (),
    active_scheme_id: str | None = None,
    best_source_id: str | None = None,
) -> SourceEntry:
    """The link rule (arch §7). Unknown ids fall through to the next rule, never to a new URL."""
    if best_source_id and best_source_id in registry:
        return registry.get(best_source_id)
    for scheme_id in (*scheme_ids, active_scheme_id):
        if scheme_id:
            try:
                return registry.for_scheme(scheme_id)
            except KeyError:
                continue
    return registry.get(default_source_id)
