"""P5.7-P5.9: a full turn with a stub generator (no network, no embedding model)."""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

from test_validate import CHUNK  # noqa: E402

from conftest import CATALOG, SOURCES  # noqa: E402
from mf_faq.ingest.parse_html import IST  # noqa: E402
from mf_faq.pipeline.answer import AnswerPipeline, ThreadContext  # noqa: E402
from mf_faq.pipeline.compose import URL_RE, Composer  # noqa: E402
from mf_faq.pipeline.draft import AnswerDraft  # noqa: E402
from mf_faq.pipeline.generate import Generation, GenerationError, Turn  # noqa: E402
from mf_faq.pipeline.retrieve import Evidence, Hit, NotFound, SearchResult, Status  # noqa: E402
from mf_faq.pipeline.schemes import Reason, SchemeResolver  # noqa: E402
from mf_faq.pipeline.scope import unsupported_topic  # noqa: E402
from mf_faq.pipeline.sources import SourceEntry, SourceRegistry  # noqa: E402
from mf_faq.pipeline.templates import Kind  # noqa: E402
from mf_faq.redacted import RedactedText  # noqa: E402

LARGE = CHUNK.scheme_id
URLS = {s.id: s.url for s in SOURCES}


class StubRetriever:
    """Same A/B/clarify layers as `Retriever.evidence`; retrieval always finds CHUNK."""

    def evidence(self, text, resolution):
        if resolution.out_of_scope:
            return Evidence(Status.INSUFFICIENT, (), NotFound.OUT_OF_SCOPE)
        if resolution.reason is Reason.NONE:
            return Evidence(Status.INSUFFICIENT, (), NotFound.NO_SCHEME)
        if topic := unsupported_topic(text):
            return Evidence(
                Status.INSUFFICIENT, resolution.scheme_ids, NotFound.UNSUPPORTED_TOPIC, (topic,)
            )
        if resolution.reason is Reason.NEEDS_SCHEME:
            return Evidence(Status.NEEDS_SCHEME)
        ids = resolution.scheme_ids
        if len(ids) > 1:
            return Evidence(Status.MULTI_SCHEME, ids)
        hit = Hit(CHUNK, 0.03, 0.8, 5.0, 0, 0)
        return Evidence(Status.OK, ids, search=SearchResult((hit,), 0.8, 5.0, 1.0))


class StubGenerator:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate(self, messages):
        self.calls.append(messages)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return Generation(out)


def d(answer="The exit load is 1% within 1 year.", status="answered", cited=CHUNK.chunk_id):
    return AnswerDraft(status=status, answer=answer, cited_chunk_id=cited)


def pipeline(generator):
    when = datetime(2026, 10, 25, tzinfo=IST)
    registry = SourceRegistry(
        [SourceEntry(s.id, s.url, s.title, s.scheme_id, when) for s in SOURCES]
    )
    return AnswerPipeline(
        retriever=StubRetriever(),
        resolver=SchemeResolver(CATALOG),
        generator=generator,
        composer=Composer(registry, CATALOG, "groww-hdfc-large-cap"),
        catalog=CATALOG,
    )


ASK = "What is the exit load of HDFC Large Cap Fund?"


def run(generator, text=ASK, ctx=None):
    return pipeline(generator).answer_turn(ctx or ThreadContext(), text)


def assert_contract(result):
    text = result.reply.text
    assert URL_RE.findall(text) == [result.reply.source_url]
    assert result.reply.source_url in URLS.values()
    assert text.splitlines()[-1].startswith("Last updated from sources: ")


def test_valid_answer_passes():
    gen = StubGenerator(d())
    r = run(gen)
    assert r.reply.kind is Kind.ANSWER
    assert r.reply.answer == "The exit load is 1% within 1 year."
    assert r.reply.source_id == CHUNK.source_id
    assert r.active_scheme_id == LARGE
    assert len(gen.calls) == 1
    assert_contract(r)


def test_made_up_number_retries_then_not_found():
    gen = StubGenerator(d("The exit load is 7%."), d("The exit load is 8%."))
    r = run(gen)
    assert r.reply.kind is Kind.NOT_FOUND
    assert r.trace["outcome"] == "validation_failed" and r.trace["attempts"] == 2
    assert "V4" in gen.calls[1][-1]["content"]  # the retry names the failed check
    assert_contract(r)


def test_retry_can_recover():
    gen = StubGenerator(d("See https://groww.in/x. Exit load 1%."), d())
    r = run(gen)
    assert r.reply.kind is Kind.ANSWER and len(gen.calls) == 2
    assert "V3" in gen.calls[1][-1]["content"]


def test_unknown_chunk_id_retries():
    gen = StubGenerator(d(cited="nope:chunk"), d())
    assert run(gen).reply.kind is Kind.ANSWER
    assert "V1" in gen.calls[1][-1]["content"]


@pytest.mark.parametrize(
    "status,kind",
    [
        ("advisory", Kind.REFUSAL),
        ("performance", Kind.PERFORMANCE),
        ("insufficient", Kind.NOT_FOUND),
    ],
)
def test_llm_status_maps_to_template(status, kind):
    r = run(StubGenerator(d("", status, None)))
    assert r.reply.kind is kind
    assert_contract(r)


def test_provider_failure_is_error_template_without_retry():
    gen = StubGenerator(GenerationError("rate_limited"))
    r = run(gen)
    assert r.reply.kind is Kind.ERROR and len(gen.calls) == 1
    assert "rate_limited" not in r.reply.text
    assert_contract(r)


@pytest.mark.parametrize("reason", ["per_minute_budget_spent", "daily_budget_spent"])
def test_spent_limits_say_busy(reason):
    r = run(StubGenerator(GenerationError(reason)))
    assert r.reply.kind is Kind.ERROR and "try again in a minute" in r.reply.answer


def test_bad_output_retries_once_then_not_found():
    gen = StubGenerator(
        GenerationError("invalid_json", retryable=True),
        GenerationError("finish_length", retryable=True),
    )
    r = run(gen)
    assert r.reply.kind is Kind.NOT_FOUND and len(gen.calls) == 2


def test_unexpected_exception_becomes_error_template():
    r = run(StubGenerator(RuntimeError("secret internals")))
    assert r.reply.kind is Kind.ERROR and "secret" not in r.reply.text


@pytest.mark.parametrize(
    "text",
    [
        "My PAN is ABCDE1234F, what is the exit load of HDFC Large Cap Fund?",
        "Should I invest in HDFC Large Cap Fund?",
        "What are the 3 year returns of HDFC Large Cap Fund?",
        "Hello",
        "What is the exit load of SBI Small Cap Fund?",
        "What is the lock-in of HDFC Large Cap Fund?",
        "Compare HDFC Large Cap Fund and HDFC Mid Cap Fund exit load",
        "And its exit load?",
    ],
)
def test_pre_llm_paths_never_call_the_generator(text):
    gen = StubGenerator()  # any call would pop from an empty list and fail
    r = run(gen, text)
    assert gen.calls == []
    assert r.reply.kind is not Kind.ANSWER
    assert_contract(r)


def test_pii_is_redacted_in_the_result():
    r = run(StubGenerator(), "my pan is ABCDE1234F")
    assert r.reply.kind is Kind.PII and r.pii_types == ("PAN",)
    assert "ABCDE1234F" not in r.user_text and isinstance(r.user_text, RedactedText)


def test_pii_never_reaches_the_prompt_or_the_log(caplog):
    caplog.set_level("DEBUG")
    run(StubGenerator(), "call me on 9876543210 about HDFC Large Cap exit load")
    assert "9876543210" not in caplog.text


def test_follow_up_carries_the_active_scheme_and_history():
    gen = StubGenerator(d())
    ctx = ThreadContext(
        history=[Turn("user", RedactedText("Tell me about HDFC Large Cap Fund"))],
        active_scheme_id=LARGE,
    )
    r = run(gen, "And its exit load?", ctx)
    assert r.reply.kind is Kind.ANSWER and r.active_scheme_id == LARGE
    assert gen.calls[0][1]["content"] == "Tell me about HDFC Large Cap Fund"


def test_out_of_scope_does_not_move_the_active_scheme():
    r = run(
        StubGenerator(), "exit load of SBI Small Cap Fund?", ThreadContext(active_scheme_id=LARGE)
    )
    assert r.active_scheme_id == LARGE


def test_two_facts_from_two_chunks_answer_with_one_link():
    from test_validate import SIZE, TWO_FACTS

    class TwoChunkRetriever(StubRetriever):
        def evidence(self, text, resolution):
            ev = super().evidence(text, resolution)
            hits = (Hit(CHUNK, 0.03, 0.8, 5.0, 0, 0), Hit(SIZE, 0.02, 0.75, 4.0, 1, 1))
            return Evidence(ev.status, ev.scheme_ids, search=SearchResult(hits, 0.8, 5.0, 1.0))

    gen = StubGenerator(
        AnswerDraft(
            status="answered",
            answer=TWO_FACTS,
            cited_chunk_id=CHUNK.chunk_id,
            also_cited_chunk_ids=(SIZE.chunk_id,),
        )
    )
    p = pipeline(gen)
    p._retriever = TwoChunkRetriever()
    r = p.answer_turn(ThreadContext(), "Exit load and fund size of HDFC Large Cap Fund?")
    assert r.reply.kind is Kind.ANSWER and r.reply.answer == TWO_FACTS
    assert r.trace["outcome"] == "answered"
    assert_contract(r)
