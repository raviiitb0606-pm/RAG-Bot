"""The source registry the composer and templates read: URL, title and footer date (P4.7).

It is loaded from the **live index's own** `manifest.json` (`data/index/<version>/manifest.json`),
not from `data/manifest.json` or the `sources` table in `app.db`: those are rewritten by every
corpus build, even one whose index then fails its gates, so their `fetched_at` could describe
content that isn't live. The index copy always matches the chunks being cited.

Every entry must be one of the 5 `ALLOWED_URLS`; the registry refuses anything else, so a link
the composer writes can only ever be one of the 5 pages.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mf_faq.ingest.parse_html import IST
from mf_faq.ingest.registry import ALLOWED_URLS, is_allowed_url


class RegistryLoadError(ValueError):
    """The manifest doesn't describe exactly the 5 allowed pages."""


@dataclass(frozen=True)
class SourceEntry:
    id: str
    url: str
    title: str
    scheme_id: str
    fetched_at: datetime  # aware; the last time the page's facts changed (arch §5.6)
    status: str | None = None  # "stale" keeps the last good fetched_at (VAL-09)

    @property
    def last_updated(self) -> str:
        """`DD MMM YYYY` in IST, e.g. `25 Oct 2026` (arch §5.6, VAL-08)."""
        return self.fetched_at.astimezone(IST).strftime("%d %b %Y")


class SourceRegistry:
    def __init__(self, entries: list[SourceEntry]) -> None:
        if len(entries) != len(ALLOWED_URLS) or {e.url for e in entries} != ALLOWED_URLS:
            raise RegistryLoadError("the registry must hold exactly the 5 allowed scheme pages")
        bad = [e.url for e in entries if not is_allowed_url(e.url)]
        if bad:  # pragma: no cover - the set comparison above already rules this out
            raise RegistryLoadError(f"URLs not in ALLOWED_URLS: {bad}")
        self._by_id = {e.id: e for e in entries}
        self._by_scheme = {e.scheme_id: e for e in entries}
        if len(self._by_id) != len(entries) or len(self._by_scheme) != len(entries):
            raise RegistryLoadError("duplicate source or scheme id in the registry")

    @classmethod
    def from_manifest(cls, path: Path) -> "SourceRegistry":
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            entries = [
                SourceEntry(
                    id=s["id"],
                    url=s["url"],
                    title=s["title"],
                    scheme_id=s["scheme_id"],
                    fetched_at=datetime.fromisoformat(s["fetched_at"]),
                    status=s.get("status"),
                )
                for s in manifest["sources"]
            ]
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise RegistryLoadError(f"{path}: no usable source registry: {e}") from e
        if any(e.fetched_at.tzinfo is None for e in entries):
            raise RegistryLoadError(f"{path}: fetched_at must carry a time zone")
        return cls(entries)

    def __contains__(self, source_id: object) -> bool:
        return source_id in self._by_id

    def __iter__(self):
        return iter(self._by_id.values())

    def get(self, source_id: str) -> SourceEntry:
        return self._by_id[source_id]

    def for_scheme(self, scheme_id: str) -> SourceEntry:
        return self._by_scheme[scheme_id]

    @property
    def urls(self) -> dict[str, str]:
        return {e.id: e.url for e in self}
