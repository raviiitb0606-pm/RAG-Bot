"""P-1: PII sent through the API never reaches SQLite, the logs or the model."""

import json
import logging
import sqlite3

import pytest
from api_support import ScriptedGenerator, client

from mf_faq.pipeline.pii import contains_pii

PII_MESSAGES = {
    "PAN": "My PAN is ABCDE1234F, what is the exit load of HDFC Mid Cap Fund?",
    "AADHAAR": "Aadhaar 2345 6789 0124 - expense ratio of HDFC Small Cap Fund?",
    "PHONE": "Call me on +91 98765 43210 about HDFC Multi Cap Fund",
    "EMAIL": "Mail ravi.k@example.com the benchmark of HDFC Large Cap Fund",
    "OTP": "my OTP is 482913",
    "ACCOUNT": "folio number 12345678901 for HDFC Mid Cap Fund",
}
RAW_VALUES = (
    "ABCDE1234F",
    "2345 6789 0124",
    "98765 43210",
    "ravi.k@example.com",
    "482913",
    "12345678901",
)


@pytest.fixture
def run(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    gen = ScriptedGenerator()
    with client(tmp_path, gen) as api:
        tid = api.post("/api/threads").json()["id"]
        replies = [
            api.post(f"/api/threads/{tid}/messages", json={"text": t}).json()
            for t in PII_MESSAGES.values()
        ]
        history = api.get(f"/api/threads/{tid}/messages").json()["messages"]
        threads = api.get("/api/threads", params={"ids": tid}).json()["threads"]
    return tmp_path, gen, replies, history, threads, caplog


def test_aadhaar_sample_is_valid():
    assert contains_pii(PII_MESSAGES["AADHAAR"])  # else the test would prove nothing


def test_every_pii_message_gets_the_pii_reply(run):
    _, gen, replies, _, _, _ = run
    assert [r["kind"] for r in replies] == ["pii"] * len(PII_MESSAGES)
    assert gen.calls == []  # never sent to the model
    for reply, kind in zip(replies, PII_MESSAGES, strict=True):
        assert f"[REDACTED_{kind}]" in reply["user_text"]


def test_no_pii_in_sqlite(run):
    tmp_path, *_ = run
    db = tmp_path / "app.db"
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT title FROM threads").fetchall()
        rows += conn.execute("SELECT content FROM messages").fetchall()
    texts = [r[0] for r in rows if r[0]]
    assert len(texts) == 1 + 2 * len(PII_MESSAGES)
    assert not [t for t in texts if contains_pii(t)]
    # And not anywhere in the files on disk (database, WAL), in any form.
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("app.db*"))
    for value in RAW_VALUES:
        assert value.encode() not in raw, value


def test_no_pii_in_api_responses(run):
    _, _, replies, history, threads, _ = run
    dumped = json.dumps([replies, history, threads])
    for value in RAW_VALUES:
        assert value not in dumped


def test_no_pii_in_logs(run):
    *_, caplog = run
    records = [
        json.dumps({"msg": r.getMessage(), **getattr(r, "fields", {})}, default=str)
        for r in caplog.get_records("setup")
    ]
    assert any('"turn.completed"' in r or "turn.completed" in r for r in records)
    for value in RAW_VALUES:
        assert not [r for r in records if value in r], value


def test_turn_log_carries_thread_and_turn(run):
    *_, caplog = run
    turns = [r.fields for r in caplog.get_records("setup") if r.getMessage() == "turn.completed"]
    assert [t["turn"] for t in turns] == list(range(1, len(PII_MESSAGES) + 1))
    assert all(t["thread_id"] and t["kind"] == "pii" for t in turns)
