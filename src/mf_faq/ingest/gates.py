"""Build gates: the corpus is exactly the 5 pages and holds no return figures (P2.7, arch §5.5).

With a `tokenizer` (the embedding model), no chunk may exceed its `max_seq_length` (P3.0).

`check_gates()` returns every failure, not just the first, so one run shows all problems.
The build writes nothing unless the list is empty.
"""

from mf_faq.ingest.chunk import Chunk, Tokenizer
from mf_faq.ingest.registry import ALLOWED_URLS, SchemeCatalog, Source, is_allowed_url
from mf_faq.ingest.strip_performance import find_performance_patterns


def check_gates(
    sources: list[Source],
    catalog: SchemeCatalog,
    chunks: dict[str, list[Chunk]],
    *,
    tokenizer: Tokenizer | None = None,
) -> list[str]:
    failures: list[str] = []

    # Exactly the 5 allowlisted pages, one per scheme (SRC-02, SRC-04, SRC-05).
    if len(sources) != len(ALLOWED_URLS):
        failures.append(f"expected exactly {len(ALLOWED_URLS)} sources, got {len(sources)}")
    if {s.url for s in sources} != ALLOWED_URLS or not all(is_allowed_url(s.url) for s in sources):
        failures.append("sources are not exactly the ALLOWED_URLS")
    if sorted(s.scheme_id for s in sources) != sorted(c.scheme_id for c in catalog.schemes):
        failures.append("each scheme in schemes.yaml needs exactly one source")

    by_id = {s.id: s for s in sources}
    for source in sources:
        page_chunks = chunks.get(source.id, [])
        if not page_chunks:
            failures.append(f"{source.id}: produced 0 chunks")
            continue
        # Every answerable fact must be in some chunk (layout-change check, PAR-01).
        found = {f for c in page_chunks for f in c.facts}
        missing = set(catalog.by_source(source.id).available_facts) - found
        if missing:
            failures.append(f"{source.id}: available_facts not found on page: {sorted(missing)}")

    for source_id, page_chunks in chunks.items():
        for chunk in page_chunks:
            if chunk.source_id not in by_id or chunk.source_id != source_id:
                failures.append(f"{chunk.chunk_id}: not from a registered source")
            hits = find_performance_patterns(chunk.content)
            if hits:
                failures.append(f"{chunk.chunk_id}: return/performance pattern: {hits}")
            # The model silently drops tokens past its limit (P3.0, research §3.1).
            n = tokenizer.count_tokens(chunk.content) if tokenizer else 0
            if tokenizer and n > tokenizer.max_seq_length:
                failures.append(
                    f"{chunk.chunk_id}: {n} tokens, over {tokenizer.model_name}'s "
                    f"max_seq_length {tokenizer.max_seq_length}"
                )
    return failures
