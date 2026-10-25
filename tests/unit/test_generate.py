"""P5.1-P5.6: prompt, request building, response checks, error mapping, rate limiter."""

from types import SimpleNamespace

import groq
import httpx
import pytest
from test_validate import CHUNK

from mf_faq.config import Settings
from mf_faq.pipeline.draft import AnswerDraft
from mf_faq.pipeline.generate import (
    RESPONSE_FORMAT,
    SYSTEM_PROMPT,
    GenerationError,
    GroqGenerator,
    RateLimited,
    RateLimiter,
    Turn,
    build_messages,
    estimate_tokens,
)
from mf_faq.pipeline.validate import Failure
from mf_faq.redacted import RedactedText

R = RedactedText


def test_schema_meets_strict_mode_rules():
    schema = RESPONSE_FORMAT["json_schema"]["schema"]
    assert RESPONSE_FORMAT["json_schema"]["strict"] is True
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "also_cited_chunk_ids" in schema["required"]


def test_old_drafts_without_extra_citations_still_parse():
    old = '{"status":"answered","answer":"x","cited_chunk_id":"a:b"}'
    assert AnswerDraft.model_validate_json(old).also_cited_chunk_ids == ()


def test_system_prompt_is_frozen_text():
    assert "{" in SYSTEM_PROMPT and "202" not in SYSTEM_PROMPT  # no dates
    for status in ("answered", "insufficient", "advisory", "performance"):
        assert f'"{status}"' in SYSTEM_PROMPT


def test_build_messages_layout():
    history = [Turn("user" if i % 2 == 0 else "assistant", R(f"t{i}")) for i in range(10)]
    msgs = build_messages(R("What is the exit load?"), [CHUNK], history, max_turns=6)
    assert msgs[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert [m["content"] for m in msgs[1:-1]] == [f"t{i}" for i in range(4, 10)]
    last = msgs[-1]
    assert last["role"] == "user"
    assert f'<chunk id="{CHUNK.chunk_id}" source="{CHUNK.source_id}">' in last["content"]
    assert last["content"].endswith("Question: What is the exit load?")
    assert len(build_messages(R("q"), [CHUNK], history, max_turns=0)) == 2


def test_history_turns_are_clipped_and_retry_note_added():
    msgs = build_messages(
        R("q"), [CHUNK], [Turn("user", R("x" * 5000))], failures=[Failure("V4", "n", "d")]
    )
    assert len(msgs[1]["content"]) <= 400
    assert "V4 n: d" in msgs[-1]["content"]


def test_prompt_estimate_is_positive():
    assert estimate_tokens(build_messages(R("q"), [CHUNK])) > 300


# --- rate limiter ---------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def limiter(clock, **kw):
    args = dict(rpm=30, tpm=8000, rpd=1000, tpd=200_000, max_wait_s=10) | kw
    return RateLimiter(**args, clock=clock, sleep=clock.sleep)


def test_limiter_waits_for_token_budget():
    clock = Clock()
    lim = limiter(clock)
    lim.acquire(5000)
    clock.now += 20
    lim.acquire(2000)  # 7000 in window: fits
    assert clock.slept == []
    clock.now += 10  # t=30: the 5000 leaves the window at t=60 → wait 30 > max_wait
    with pytest.raises(RateLimited):
        lim.acquire(3000)
    lim.max_wait_s = 60
    lim.acquire(3000)
    assert clock.slept and clock.slept[0] == pytest.approx(30)


def test_limiter_request_count_and_settle():
    clock = Clock()
    lim = limiter(clock, rpm=2)
    lim.acquire(10)
    lim.acquire(10)
    with pytest.raises(RateLimited):
        lim.acquire(10)
    clock.now += 61
    event = lim.acquire(7000)
    lim.settle(event, 100)
    lim.acquire(7000)  # fits only because the first was settled down


def test_limiter_daily_budgets_and_off_switch():
    clock = Clock()
    lim = limiter(clock, rpd=2, rpm=0)
    lim.acquire(1)
    clock.now += 100
    lim.acquire(1)
    clock.now += 100
    with pytest.raises(RateLimited, match="daily"):
        lim.acquire(1)
    clock.now += 86_400
    lim.acquire(1)  # the day rolled over
    off = limiter(clock, rpm=0, tpm=0, rpd=0, tpd=0)
    for _ in range(50):
        off.acquire(10**6)
    with pytest.raises(RateLimited):
        limiter(clock, tpm=100).acquire(500)  # bigger than the whole minute budget


# --- GroqGenerator --------------------------------------------------------------------------


def completion(content, finish="stop", usage=(100, 20, 64)):
    u = SimpleNamespace(
        prompt_tokens=usage[0],
        completion_tokens=usage[1],
        prompt_tokens_details=SimpleNamespace(cached_tokens=usage[2]),
    )
    msg = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)], usage=u)


class FakeClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(with_raw_response=self))

    def create(self, **kw):
        self.calls.append(kw)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return SimpleNamespace(parse=lambda: self.outcome, headers={})


def generator(outcome):
    settings = Settings(_env_file=None, groq_api_key="k")
    client = FakeClient(outcome)
    return GroqGenerator(settings, client=client), client


GOOD = '{"status":"answered","answer":"The exit load is 1%.","cited_chunk_id":"a:b"}'


def test_generate_parses_draft_and_sends_expected_request():
    gen, client = generator(completion(GOOD))
    out = gen.generate([{"role": "user", "content": "hi"}])
    assert out.draft == AnswerDraft(
        status="answered", answer="The exit load is 1%.", cited_chunk_id="a:b"
    )
    assert (out.usage.prompt_tokens, out.usage.cached_tokens) == (100, 64)
    call = client.calls[0]
    assert call["model"] == "openai/gpt-oss-120b"
    assert call["include_reasoning"] is False and call["temperature"] == 0
    assert call["reasoning_effort"] == "low" and call["max_completion_tokens"] == 1024
    assert call["response_format"] == RESPONSE_FORMAT


@pytest.mark.parametrize(
    "outcome,reason",
    [
        (completion(GOOD, finish="length"), "finish_length"),
        (completion(""), "empty_content"),
        (completion(None), "empty_content"),
        (completion("I cannot help with that."), "invalid_json"),
        (completion('{"status":"answered"}'), "invalid_json"),
        (SimpleNamespace(choices=[], usage=None), "no_choices"),
    ],
)
def test_bad_output_is_a_retryable_error(outcome, reason):
    gen, _ = generator(outcome)
    with pytest.raises(GenerationError) as e:
        gen.generate([{"role": "user", "content": "hi"}])
    assert (e.value.reason, e.value.retryable) == (reason, True)


def _status_error(cls, code):
    req = httpx.Request("POST", "https://api.groq.com")
    return cls("boom", response=httpx.Response(code, request=req), body=None)


@pytest.mark.parametrize(
    "error,reason",
    [
        (groq.APITimeoutError(httpx.Request("POST", "https://x")), "timeout"),
        (groq.APIConnectionError(request=httpx.Request("POST", "https://x")), "connection"),
        (_status_error(groq.RateLimitError, 429), "rate_limited"),
        (_status_error(groq.InternalServerError, 503), "status_503"),
        (_status_error(groq.AuthenticationError, 401), "status_401"),
    ],
)
def test_sdk_errors_become_generation_errors(error, reason):
    gen, _ = generator(error)
    with pytest.raises(GenerationError) as e:
        gen.generate([{"role": "user", "content": "hi"}])
    assert (e.value.reason, e.value.retryable) == (reason, False)
    assert "boom" not in str(e.value)


def test_missing_key_fails_fast(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        GroqGenerator(Settings(_env_file=None))
