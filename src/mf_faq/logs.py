"""JSON logging (P0.13, arch §13).

Rule from day one: only redacted text is logged (P-1). Use `log_event()` with a
fixed event name and structured fields; never put user text in the message
string. Any text-bearing field that isn't `RedactedText` is dropped.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from mf_faq.redacted import RedactedText

# Field names that can carry user or model text. Values must be RedactedText.
TEXT_FIELDS = frozenset(
    {"text", "question", "answer", "content", "message", "query", "prompt", "history", "title"}
)
DROPPED = "[UNREDACTED_TEXT_DROPPED]"
_EVENT_RE = re.compile(r"^[a-z0-9_.]+$")


def _scrub(key: str, value: Any) -> Any:
    if key in TEXT_FIELDS:
        if isinstance(value, RedactedText):
            return str(value)
        if isinstance(value, list | tuple):
            return [str(v) if isinstance(v, RedactedText) else DROPPED for v in value]
        return DROPPED
    if isinstance(value, dict):
        return {k: _scrub(k, v) for k, v in value.items()}
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            # Exception type only: messages can echo user input.
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log one structured record. `event` must be a fixed identifier like `turn.completed`."""
    if not _EVENT_RE.match(event):
        fields = {"invalid_event": True, **fields}
        event = "invalid_event_name"
    scrubbed = {k: _scrub(k, v) for k, v in fields.items()}
    logger.log(level, event, extra={"fields": scrubbed})
