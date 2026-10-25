"""Load and validate corpus/sources.yaml against the exact-URL allowlist (P2.1, arch §5.1).

The allowlist itself is P1.1. Anything not listed here is rejected, even other groww.in pages.
`load_schemes()` reads corpus/schemes.yaml (P1.5) and checks it agrees with the sources.
"""

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ALLOWED_HOSTS = frozenset({"groww.in"})
ALLOWED_URLS = frozenset(
    {
        "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
        "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth",
        "https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth",
        "https://groww.in/mutual-funds/hdfc-multi-cap-fund-direct-growth",
        "https://groww.in/mutual-funds/hdfc-silver-etf-fof-direct-growth",
    }
)


class RegistryError(ValueError):
    """sources.yaml breaks a corpus rule; the build must stop."""


def normalize_url(url: str) -> str:
    """Lower-case scheme and host; drop query string, fragment and trailing slash."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def is_allowed_url(url: str) -> bool:
    normalized = normalize_url(url)
    return urlsplit(normalized).netloc in ALLOWED_HOSTS and normalized in ALLOWED_URLS


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    url: str
    publisher: Literal["GROWW"]
    doc_type: Literal["scheme_page"]
    scheme_id: str
    title: str
    notes: str | None = None

    @field_validator("url")
    @classmethod
    def _allowed(cls, url: str) -> str:
        if not is_allowed_url(url):
            raise ValueError(f"URL not in ALLOWED_URLS: {url}")
        return normalize_url(url)

    @property
    def slug(self) -> str:
        return urlsplit(self.url).path.rsplit("/", 1)[-1]


def validate_sources(entries: list[dict]) -> list[Source]:
    try:
        sources = [Source.model_validate(e) for e in entries]
    except ValidationError as e:
        raise RegistryError(str(e)) from e
    if len(sources) != len(ALLOWED_URLS):
        raise RegistryError(f"expected exactly {len(ALLOWED_URLS)} sources, got {len(sources)}")
    for field in ("id", "url", "scheme_id"):
        values = [getattr(s, field) for s in sources]
        if len(set(values)) != len(values):
            raise RegistryError(f"duplicate {field} in sources.yaml")
    return sources


def load_sources(path: Path) -> list[Source]:
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise RegistryError(f"{path}: expected a list of sources")
    return validate_sources(entries)


class Scheme(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scheme_id: str
    source_id: str
    name: str
    short_name: str
    category: str
    plan: str
    aliases: list[str]
    available_facts: list[str]


class FactGroup(BaseModel):
    """One chunk group (P3.0): `covers` is the value-free "Covers:" line of its chunks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    covers: str = Field(min_length=1)

    @field_validator("covers")
    @classmethod
    def _value_free(cls, covers: str) -> str:
        if re.search(r"[\d%₹]", covers):
            raise ValueError(f"covers line must not contain fact values: {covers!r}")
        return covers.strip().rstrip(".")


class OtherAmcs(BaseModel):
    """Other fund houses (P3.2). `need_fund_word` names are also common words or companies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    names: list[str] = Field(default_factory=list)
    need_fund_word: list[str] = Field(default_factory=list)


class SchemeCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_labels: dict[str, str]
    fact_groups: dict[str, FactGroup] = Field(default_factory=dict)
    excluded_page_content: list[dict[str, str]] = Field(default_factory=list)
    schemes: list[Scheme]
    not_in_scope: list[str] = Field(default_factory=list)
    other_amcs: OtherAmcs = Field(default_factory=OtherAmcs)

    def by_id(self, scheme_id: str) -> Scheme:
        for scheme in self.schemes:
            if scheme.scheme_id == scheme_id:
                return scheme
        raise KeyError(scheme_id)

    @property
    def covers(self) -> dict[str, str]:
        return {group: g.covers for group, g in self.fact_groups.items()}

    def by_source(self, source_id: str) -> Scheme:
        for scheme in self.schemes:
            if scheme.source_id == source_id:
                return scheme
        raise KeyError(source_id)


def load_schemes(path: Path, sources: list[Source]) -> SchemeCatalog:
    """Load schemes.yaml; each of the 5 sources must map to exactly one scheme (SRC-05)."""
    try:
        catalog = SchemeCatalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except ValidationError as e:
        raise RegistryError(str(e)) from e
    pairs = {(s.scheme_id, s.source_id) for s in catalog.schemes}
    expected = {(s.scheme_id, s.id) for s in sources}
    if pairs != expected or len(catalog.schemes) != len(sources):
        raise RegistryError("schemes.yaml does not match sources.yaml one-to-one")
    for scheme in catalog.schemes:
        unknown = set(scheme.available_facts) - set(catalog.fact_labels)
        if unknown:
            raise RegistryError(f"{scheme.scheme_id}: facts without a label: {sorted(unknown)}")
    return catalog
