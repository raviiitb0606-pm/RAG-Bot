"""API request/response models (P6.3, arch §9.1).

Every text field in a response is either fixed copy, registry data, or text that was stored
redacted; nothing here carries raw user input.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_QUESTION_CHARS = 500

Question = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
]


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Question


class ThreadOut(BaseModel):
    id: str
    title: str | None  # the first question, redacted and shortened; None until one is asked
    created_at: str
    updated_at: str
    message_count: int


class ThreadList(BaseModel):
    threads: list[ThreadOut]


class MessageOut(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    kind: str | None  # assistant only: answer | refusal | performance | pii | not_found | …
    text: str  # user: the redacted question; assistant: the body, without link or footer
    created_at: str
    source_url: str | None = None
    source_title: str | None = None
    last_updated: str | None = None


class MessageList(BaseModel):
    messages: list[MessageOut]


class ReplyOut(BaseModel):
    """The assistant's reply (arch §9.1 payload) plus what the UI needs to update the thread."""

    kind: str
    answer: str
    source_url: str
    source_title: str
    last_updated: str
    text: str  # body + "Source:" line + footer, for plain-text clients
    created_at: str
    user_text: str  # the question as stored (PII replaced by [REDACTED_<TYPE>])
    thread: ThreadOut


class Example(BaseModel):
    question: str
    fact: str  # fact key from schemes.yaml, e.g. "exit_load"
    label: str  # its wording, e.g. "exit load"
    scheme_id: str


class SchemeOut(BaseModel):
    scheme_id: str
    name: str
    short_name: str
    category: str
    plan: str
    source_url: str


class MetaOut(BaseModel):
    welcome: str
    disclaimer: str
    pii_hint: str
    examples: list[Example] = Field(min_length=3, max_length=3)
    schemes: list[SchemeOut]
    corpus_version: str | None
    thread_ttl_days: int
    max_question_chars: int = MAX_QUESTION_CHARS


class HealthOut(BaseModel):
    status: Literal["ok"]
    index_loaded: bool
    index_version: str | None
    corpus_version: str | None


class ErrorOut(BaseModel):
    detail: str
