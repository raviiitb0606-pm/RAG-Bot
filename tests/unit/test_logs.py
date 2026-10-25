import json
import logging

from mf_faq.logs import DROPPED, JsonFormatter, log_event
from mf_faq.redacted import RedactedText


def _capture(caplog, event, **fields):
    with caplog.at_level(logging.INFO, logger="t"):
        log_event(logging.getLogger("t"), event, **fields)
    return json.loads(JsonFormatter().format(caplog.records[-1]))


def test_redacted_text_is_logged(caplog):
    out = _capture(caplog, "turn.completed", text=RedactedText("PAN [REDACTED_PAN]"), turn=3)
    assert out["event"] == "turn.completed"
    assert out["text"] == "PAN [REDACTED_PAN]"
    assert out["turn"] == 3


def test_plain_str_text_is_dropped(caplog):
    out = _capture(caplog, "turn.completed", question="My PAN is ABCDE1234F")
    assert out["question"] == DROPPED
    assert "ABCDE1234F" not in json.dumps(out)


def test_nested_and_list_text_is_dropped(caplog):
    out = _capture(
        caplog,
        "llm.request",
        history=[RedactedText("ok"), "raw 9876543210"],
        meta={"answer": "raw"},
    )
    assert out["history"] == ["ok", DROPPED]
    assert out["meta"] == {"answer": DROPPED}


def test_free_text_event_name_rejected(caplog):
    out = _capture(caplog, "user said ABCDE1234F")
    assert out["event"] == "invalid_event_name"
