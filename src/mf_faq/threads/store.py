"""SQLite thread and message repository; redacted text only (P6.1, P6.2, arch §8.1).

- One short-lived connection per call (`check_same_thread` is never an issue under the API's
  thread pool); WAL mode lets reads run alongside a write; foreign keys are on, so deleting a
  thread deletes its messages.
- `append_message` takes `RedactedText`, not `str`: raw user input can't be stored by mistake
  (P-1). The thread title is the first user message, so it is redacted too.
- Threads are anonymous: no user identity, only the UUID the browser keeps (arch §8.2).
- `purge_older_than(days)` drops threads with no activity for `days` (retention, P6.7).

The `sources` table in the same file belongs to ingestion (`ingest/build.py`); it isn't touched.
"""

import sqlite3
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mf_faq.redacted import RedactedText

TITLE_CHARS = 60
MAX_LISTED = 100  # ids per `list_threads` call; the browser keeps far fewer

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  id               TEXT PRIMARY KEY,
  title            TEXT,
  active_scheme_id TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id               INTEGER PRIMARY KEY,
  thread_id        TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  role             TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content          TEXT NOT NULL,
  kind             TEXT,
  source_id        TEXT,
  last_updated     TEXT,
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, id);
CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated_at);
"""


class ThreadNotFound(KeyError):
    """No thread with this id (deleted, purged, or never created)."""


@dataclass(frozen=True)
class ThreadRow:
    id: str
    title: str | None
    active_scheme_id: str | None
    created_at: str
    updated_at: str
    message_count: int = 0


@dataclass(frozen=True)
class MessageRow:
    id: int
    thread_id: str
    role: str
    content: str
    kind: str | None
    source_id: str | None
    last_updated: str | None  # footer date of an assistant reply, as shown then
    created_at: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _title(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1].rstrip() + "…"


class ThreadStore:
    def __init__(self, db_path: Path, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self._path = Path(db_path)
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:  # one transaction: commit on success, roll back on error
                yield conn
        finally:
            conn.close()

    def _now(self) -> str:
        return self._clock().isoformat(timespec="milliseconds")

    # -- threads -----------------------------------------------------------------------------

    def create_thread(self) -> ThreadRow:
        now = self._now()
        row = ThreadRow(str(uuid.uuid4()), None, None, now, now)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO threads (id, title, active_scheme_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (row.id, row.title, row.active_scheme_id, row.created_at, row.updated_at),
            )
        return row

    def get_thread(self, thread_id: str) -> ThreadRow:
        rows = self._threads("t.id = ?", (thread_id,))
        if not rows:
            raise ThreadNotFound(thread_id)
        return rows[0]

    def list_threads(self, ids: Sequence[str]) -> list[ThreadRow]:
        """The given threads that still exist, most recently active first."""
        ids = list(dict.fromkeys(ids))[:MAX_LISTED]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self._threads(f"t.id IN ({marks})", tuple(ids))

    def _threads(self, where: str, params: tuple) -> list[ThreadRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.*, (SELECT COUNT(*) FROM messages m WHERE m.thread_id = t.id) AS n "
                f"FROM threads t WHERE {where} ORDER BY t.updated_at DESC, t.id",
                params,
            ).fetchall()
        return [
            ThreadRow(
                r["id"], r["title"], r["active_scheme_id"], r["created_at"], r["updated_at"], r["n"]
            )
            for r in rows
        ]

    def set_active_scheme(self, thread_id: str, scheme_id: str | None) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE threads SET active_scheme_id = ? WHERE id = ?", (scheme_id, thread_id)
            )
            if cur.rowcount == 0:
                raise ThreadNotFound(thread_id)

    def delete_thread(self, thread_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,)).rowcount > 0

    def purge_older_than(self, days: int) -> int:
        """Delete threads (and their messages) with no activity for `days`; returns the count."""
        cutoff = (self._clock() - timedelta(days=days)).isoformat(timespec="milliseconds")
        with self._connect() as conn:
            return conn.execute("DELETE FROM threads WHERE updated_at < ?", (cutoff,)).rowcount

    # -- messages ----------------------------------------------------------------------------

    def get_messages(self, thread_id: str, limit: int | None = None) -> list[MessageRow]:
        """The thread's messages in order; with `limit`, only the last `limit` of them."""
        self.get_thread(thread_id)  # 404 rather than an empty list for an unknown id
        sql = "SELECT * FROM messages WHERE thread_id = ? ORDER BY id DESC"
        params: tuple = (thread_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (max(limit, 0),)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            MessageRow(
                r["id"],
                r["thread_id"],
                r["role"],
                r["content"],
                r["kind"],
                r["source_id"],
                r["last_updated"],
                r["created_at"],
            )
            for r in reversed(rows)
        ]

    def append_message(
        self,
        thread_id: str,
        role: str,
        content: RedactedText,
        *,
        kind: str | None = None,
        source_id: str | None = None,
        last_updated: str | None = None,
    ) -> MessageRow:
        if not isinstance(content, RedactedText):  # P6.2: never store unredacted text
            raise TypeError("append_message() needs RedactedText (run the PII guard first)")
        if role not in ("user", "assistant"):
            raise ValueError(f"role must be 'user' or 'assistant', not {role!r}")
        now = self._now()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE threads SET updated_at = ?, "
                "title = COALESCE(title, CASE WHEN ? = 'user' THEN ? END) WHERE id = ?",
                (now, role, _title(content), thread_id),
            )
            if cur.rowcount == 0:
                raise ThreadNotFound(thread_id)
            cur = conn.execute(
                "INSERT INTO messages "
                "(thread_id, role, content, kind, source_id, last_updated, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (thread_id, role, str(content), kind, source_id, last_updated, now),
            )
        return MessageRow(
            cur.lastrowid, thread_id, role, str(content), kind, source_id, last_updated, now
        )
