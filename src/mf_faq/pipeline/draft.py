"""The LLM's structured answer (arch §6.6), shared by the validator (P4.6) and generation (P5).

Its JSON schema is sent as Groq's strict `response_format`, so every field is required and no
extra keys are allowed (`additionalProperties: false`). The schema is built in serialization
mode with `json_schema_serialization_defaults_required`, so `also_cited_chunk_ids` is required
for the model even though Python code may leave it out.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class AnswerDraft(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_serialization_defaults_required=True
    )

    status: Literal["answered", "insufficient", "advisory", "performance"]
    answer: str  # ≤ 3 sentences; empty unless status == "answered"
    cited_chunk_id: str | None  # one of the chunk ids sent; required (nullable), no default
    # Other chunks the answer uses, for a question about several facts ("NAV and fund size?").
    # They must come from the same page as `cited_chunk_id`, so the reply still has one link.
    also_cited_chunk_ids: tuple[str, ...] = ()
