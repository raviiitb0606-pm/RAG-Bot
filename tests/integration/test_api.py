"""P6.3-P6.9: REST API (arch §9.1) over the real pipeline with a stub retriever and generator."""

import uuid

import pytest
from api_support import ANSWER, URL_BY_SCHEME, ScriptedGenerator, client, make_app

from conftest import CATALOG
from mf_faq.api.app import DISCLAIMER, EXAMPLES, examples
from mf_faq.pipeline.compose import FOOTER_RE, URL_RE


@pytest.fixture
def api(tmp_path):
    with client(tmp_path) as c:
        yield c


def new_thread(api) -> str:
    resp = api.post("/api/threads")
    assert resp.status_code == 201
    return resp.json()["id"]


def ask(api, thread_id: str, text: str):
    return api.post(f"/api/threads/{thread_id}/messages", json={"text": text})


# -- meta and health ---------------------------------------------------------------------------


def test_meta(api):
    meta = api.get("/api/meta").json()
    assert meta["disclaimer"] == DISCLAIMER == "Facts-only. No investment advice."
    assert "5 HDFC Mutual Fund schemes" in meta["welcome"]
    assert len(meta["examples"]) == 3
    assert {s["scheme_id"] for s in meta["schemes"]} == set(URL_BY_SCHEME)
    assert all(s["source_url"] == URL_BY_SCHEME[s["scheme_id"]] for s in meta["schemes"])
    assert meta["corpus_version"] == "2026-10-25.test"
    assert meta["thread_ttl_days"] == 30 and meta["max_question_chars"] == 500
    assert "PAN" in meta["pii_hint"]


def test_examples_ask_for_facts_their_page_shows():
    for example in examples(CATALOG):
        scheme = CATALOG.by_id(example.scheme_id)
        assert example.fact in scheme.available_facts
        assert scheme.short_name in example.question
    assert len({fact for _, fact, _ in EXAMPLES}) == 3


def test_examples_get_an_answer(api):
    for example in api.get("/api/meta").json()["examples"]:
        reply = ask(api, new_thread(api), example["question"]).json()
        assert reply["kind"] == "answer", example
        assert reply["source_url"] == URL_BY_SCHEME[example["scheme_id"]]


def test_health(api):
    assert api.get("/api/health").json() == {
        "status": "ok",
        "index_loaded": True,
        "index_version": "test-index",
        "corpus_version": "2026-10-25.test",
    }


def test_openapi_lists_every_endpoint(api):
    paths = api.get("/openapi.json").json()["paths"]
    assert set(paths) == {
        "/api/meta",
        "/api/health",
        "/api/threads",
        "/api/threads/{thread_id}/messages",
        "/api/threads/{thread_id}",
    }
    assert set(paths["/api/threads"]) == {"get", "post"}
    assert set(paths["/api/threads/{thread_id}/messages"]) == {"get", "post"}
    assert set(paths["/api/threads/{thread_id}"]) == {"delete"}
    assert api.get("/docs").status_code == 200


# -- threads -----------------------------------------------------------------------------------


def test_thread_crud(api):
    a, b = new_thread(api), new_thread(api)
    listed = api.get("/api/threads", params={"ids": f"{a},{b},not-a-uuid,{uuid.uuid4()}"}).json()
    assert {t["id"] for t in listed["threads"]} == {a, b}
    assert all(t["title"] is None and t["message_count"] == 0 for t in listed["threads"])

    ask(api, a, "What is the exit load of HDFC Mid Cap Fund?")
    first = api.get("/api/threads", params={"ids": f"{b},{a}"}).json()["threads"][0]
    assert first["id"] == a and first["message_count"] == 2
    assert first["title"] == "What is the exit load of HDFC Mid Cap Fund?"

    assert api.delete(f"/api/threads/{a}").status_code == 204
    assert api.get(f"/api/threads/{a}/messages").status_code == 404
    assert api.delete(f"/api/threads/{a}").status_code == 404
    assert [t["id"] for t in api.get("/api/threads", params={"ids": a}).json()["threads"]] == []


def test_list_threads_without_ids_lists_nothing(api):
    new_thread(api)
    assert api.get("/api/threads").json() == {"threads": []}


def test_message_round_trip(api):
    tid = new_thread(api)
    resp = ask(api, tid, "  What is the exit load of HDFC Mid Cap Fund?  ")
    assert resp.status_code == 200
    reply = resp.json()
    assert reply["kind"] == "answer"
    assert reply["answer"] == ANSWER
    assert reply["source_url"] == URL_BY_SCHEME["hdfc-mid-cap"]
    assert reply["last_updated"] == "25 Oct 2026"
    assert URL_RE.findall(reply["text"]) == [reply["source_url"]]
    assert FOOTER_RE.match(reply["text"].splitlines()[-1])
    assert reply["user_text"] == "What is the exit load of HDFC Mid Cap Fund?"
    assert reply["thread"]["id"] == tid and reply["thread"]["message_count"] == 2

    history = api.get(f"/api/threads/{tid}/messages").json()["messages"]
    assert [m["role"] for m in history] == ["user", "assistant"]
    user, assistant = history
    assert user["text"] == reply["user_text"] and user["kind"] is None
    assert user["source_url"] is None
    assert assistant["text"] == ANSWER and assistant["kind"] == "answer"
    assert assistant["source_url"] == reply["source_url"]
    assert assistant["source_title"] == reply["source_title"]
    assert assistant["last_updated"] == "25 Oct 2026"


def test_template_replies_are_stored_with_their_kind(api):
    tid = new_thread(api)
    assert ask(api, tid, "Should I invest in HDFC Small Cap Fund?").json()["kind"] == "refusal"
    assert ask(api, tid, "What is the exit load?").json()["kind"] == "answer"  # carry-over
    kinds = [m["kind"] for m in api.get(f"/api/threads/{tid}/messages").json()["messages"]]
    assert kinds == [None, "refusal", None, "answer"]


def test_history_is_sent_to_the_model(tmp_path):
    gen = ScriptedGenerator()
    with client(tmp_path, gen) as api:
        tid = new_thread(api)
        ask(api, tid, "What is the exit load of HDFC Mid Cap Fund?")
        ask(api, tid, "And its expense ratio?")
    second = gen.calls[1]
    roles = [m["role"] for m in second]
    assert roles == ["system", "user", "assistant", "user"]
    assert "HDFC Mid Cap Fund" in second[1]["content"]


def test_history_is_capped(tmp_path):
    gen = ScriptedGenerator()
    with client(tmp_path, gen, history_turns=2) as api:
        tid = new_thread(api)
        for _ in range(3):
            ask(api, tid, "What is the exit load of HDFC Mid Cap Fund?")
    assert len(gen.calls[-1]) == 1 + 2 + 1  # system + 2 history turns + question


# -- errors ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [{"text": ""}, {"text": "   "}, {"text": "x" * 501}, {}, {"text": 5}, {"text": "hi", "x": 1}],
)
def test_validation_errors(api, body):
    resp = api.post(f"/api/threads/{new_thread(api)}/messages", json=body)
    assert resp.status_code == 422
    assert all("input" not in e for e in resp.json()["detail"])  # the body is never echoed


def test_text_of_500_characters_is_accepted(api):
    assert ask(api, new_thread(api), "x" * 500).status_code == 200


def test_unknown_or_malformed_thread_ids(api):
    missing = uuid.uuid4()
    assert ask(api, str(missing), "hi").status_code == 404
    assert api.get(f"/api/threads/{missing}/messages").json() == {"detail": "Thread not found."}
    assert api.get("/api/threads/not-a-uuid/messages").status_code == 422


def test_oversized_body_is_rejected(api):
    resp = api.post(
        f"/api/threads/{new_thread(api)}/messages",
        content=b'{"text": "' + b"x" * 10_000 + b'"}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413


def test_rate_limit(tmp_path):
    with client(tmp_path, api_rate_limit="3/minute") as api:
        tid = new_thread(api)
        codes = [
            ask(api, tid, "What is the exit load of HDFC Mid Cap Fund?").status_code
            for _ in range(4)
        ]
    assert codes == [200, 200, 200, 429]


def test_unexpected_error_is_generic(tmp_path):
    app = make_app(tmp_path)
    app.state.service.store.append_message = None  # a bug outside the pipeline: a real 500
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as api:
        resp = ask(api, new_thread(api), "What is the exit load of HDFC Mid Cap Fund?")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Something went wrong on our side. Please try again."}


def test_pipeline_bug_still_answers_with_the_error_template(tmp_path):
    class Broken:
        def generate(self, messages):
            raise AssertionError("internal detail")

    with client(tmp_path, Broken()) as api:
        reply = ask(api, new_thread(api), "What is the exit load of HDFC Mid Cap Fund?").json()
    assert reply["kind"] == "error" and "internal" not in reply["text"]


# -- static UI and headers ---------------------------------------------------------------------


def test_serves_the_ui_with_security_headers(api):
    resp = api.get("/")
    assert resp.status_code == 200
    assert "Facts-only. No investment advice." in resp.text
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    for asset in ("/app.js", "/styles.css"):
        assert api.get(asset).status_code == 200


def test_api_responses_are_not_cached(api):
    assert api.get("/api/meta").headers["cache-control"] == "no-store"


def test_no_cors_headers_for_other_origins(api):
    resp = api.get("/api/meta", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers


def test_purge_runs_on_startup(tmp_path):
    app = make_app(tmp_path)
    store = app.state.service.store
    old = store.create_thread()
    with store._connect() as conn:
        conn.execute("UPDATE threads SET updated_at = '2000-01-01T00:00:00.000+00:00'")
    from fastapi.testclient import TestClient

    with TestClient(app) as api:
        assert api.get("/api/threads", params={"ids": old.id}).json() == {"threads": []}
