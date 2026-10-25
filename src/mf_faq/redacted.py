"""Marker type for user text that has been through the PII guard (P-1)."""


class RedactedText(str):
    """A `str` that is known to be PII-redacted.

    Only `pipeline.pii.redact()` should construct it. Logs (and later the thread
    store, P6.2) accept user text only in this type, so raw input can't slip through.
    """

    __slots__ = ()
