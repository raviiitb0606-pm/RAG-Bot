"""P6.1-P6.2, P6.7: the SQLite thread store (arch §8.1)."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from mf_faq.redacted import RedactedText
from mf_faq.threads.store import TITLE_CHARS, ThreadNotFound, ThreadStore


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, clock) -> ThreadStore:
    return ThreadStore(tmp_path / "app.db", clock=clock)


def r(text: str) -> RedactedText:
    return RedactedText(text)


def test_schema_uses_wal_and_keeps_the_ingest_sources_table(tmp_path):
    db = tmp_path / "app.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sources (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO sources VALUES ('groww-hdfc-large-cap')")
    ThreadStore(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_create_and_get_thread(store):
    thread = store.create_thread()
    got = store.get_thread(thread.id)
    assert got.id == thread.id and got.title is None and got.message_count == 0
    assert got.active_scheme_id is None


def test_unknown_thread_raises(store):
    with pytest.raises(ThreadNotFound):
        store.get_thread("nope")
    with pytest.raises(ThreadNotFound):
        store.get_messages("nope")
    with pytest.raises(ThreadNotFound):
        store.append_message("nope", "user", r("hi"))
    with pytest.raises(ThreadNotFound):
        store.set_active_scheme("nope", "hdfc-mid-cap")


def test_append_message_needs_redacted_text(store):
    thread = store.create_thread()
    with pytest.raises(TypeError, match="RedactedText"):
        store.append_message(thread.id, "user", "My PAN is ABCDE1234F")  # type: ignore[arg-type]
    assert store.get_messages(thread.id) == []


def test_append_message_rejects_unknown_role(store):
    thread = store.create_thread()
    with pytest.raises(ValueError):
        store.append_message(thread.id, "system", r("x"))


def test_title_is_the_first_user_message_shortened(store, clock):
    thread = store.create_thread()
    long = "What is the exit load of HDFC Small Cap Fund " + "and more " * 10
    clock.advance(seconds=1)
    store.append_message(thread.id, "user", r(long))
    store.append_message(thread.id, "assistant", r("Answer."), kind="answer")
    store.append_message(thread.id, "user", r("Second question"))
    got = store.get_thread(thread.id)
    assert got.title.startswith("What is the exit load")
    assert len(got.title) == TITLE_CHARS and got.title.endswith("…")
    assert got.message_count == 3
    assert got.updated_at > thread.updated_at


def test_messages_in_order_and_limit_keeps_the_last(store):
    thread = store.create_thread()
    for i in range(5):
        store.append_message(thread.id, "user", r(f"q{i}"))
        store.append_message(
            thread.id, "assistant", r(f"a{i}"), kind="answer", source_id="s", last_updated="d"
        )
    all_ = store.get_messages(thread.id)
    assert [m.content for m in all_][:3] == ["q0", "a0", "q1"]
    last = store.get_messages(thread.id, limit=3)
    assert [m.content for m in last] == ["a3", "q4", "a4"]
    assert last[-1].kind == "answer" and last[-1].source_id == "s"
    assert last[-1].last_updated == "d"


def test_list_threads_only_the_given_ids_most_recent_first(store, clock):
    a = store.create_thread()
    clock.advance(minutes=1)
    b = store.create_thread()
    store.create_thread()  # someone else's: not asked for
    clock.advance(minutes=1)
    store.append_message(a.id, "user", r("hello"))
    listed = store.list_threads([b.id, a.id, a.id, "missing"])
    assert [t.id for t in listed] == [a.id, b.id]
    assert store.list_threads([]) == []


def test_active_scheme_is_per_thread(store):
    a, b = store.create_thread(), store.create_thread()
    store.set_active_scheme(a.id, "hdfc-mid-cap")
    store.set_active_scheme(b.id, "hdfc-small-cap")
    assert store.get_thread(a.id).active_scheme_id == "hdfc-mid-cap"
    assert store.get_thread(b.id).active_scheme_id == "hdfc-small-cap"
    store.set_active_scheme(a.id, None)
    assert store.get_thread(a.id).active_scheme_id is None


def test_delete_cascades_to_messages(store, tmp_path):
    thread = store.create_thread()
    store.append_message(thread.id, "user", r("q"))
    assert store.delete_thread(thread.id) is True
    assert store.delete_thread(thread.id) is False
    with sqlite3.connect(tmp_path / "app.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_purge_drops_threads_idle_for_longer_than_ttl(store, clock):
    old = store.create_thread()
    store.append_message(old.id, "user", r("old"))
    clock.advance(days=20)
    recent = store.create_thread()
    clock.advance(days=11)  # old: 31 days idle, recent: 11
    assert store.purge_older_than(30) == 1
    assert [t.id for t in store.list_threads([old.id, recent.id])] == [recent.id]
    assert store.purge_older_than(30) == 0
