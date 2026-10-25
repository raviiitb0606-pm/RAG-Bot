from pathlib import Path

import pytest
import yaml

from mf_faq.ingest.registry import (
    ALLOWED_URLS,
    RegistryError,
    is_allowed_url,
    load_schemes,
    load_sources,
    normalize_url,
    validate_sources,
)

SOURCES_YAML = Path(__file__).resolve().parents[2] / "corpus" / "sources.yaml"


def _entries():
    return yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))


def test_real_sources_file_loads():
    sources = load_sources(SOURCES_YAML)
    assert {s.url for s in sources} == ALLOWED_URLS
    assert next(s for s in sources if s.id == "groww-hdfc-mid-cap").slug == (
        "hdfc-mid-cap-fund-direct-growth"
    )


def test_four_entries_rejected():
    with pytest.raises(RegistryError, match="exactly 5"):
        validate_sources(_entries()[:4])


def test_six_entries_rejected():
    entries = _entries()
    extra = dict(entries[0], id="groww-extra", scheme_id="extra")
    with pytest.raises(RegistryError):
        validate_sources([*entries, extra])


def test_other_groww_url_rejected():
    entries = _entries()
    entries[0] = dict(
        entries[0], url="https://groww.in/mutual-funds/hdfc-flexi-cap-fund-direct-growth"
    )
    with pytest.raises(RegistryError, match="ALLOWED_URLS"):
        validate_sources(entries)


@pytest.mark.parametrize("field", ["id", "url", "scheme_id"])
def test_duplicates_rejected(field):
    entries = _entries()
    entries[1] = dict(entries[1], **{field: entries[0][field]})
    with pytest.raises(RegistryError):
        validate_sources(entries)


def test_unknown_field_and_wrong_publisher_rejected():
    for change in ({"use_as_refusal_link": True}, {"publisher": "AMFI"}):
        entries = _entries()
        entries[0] = dict(entries[0], **change)
        with pytest.raises(RegistryError):
            validate_sources(entries)


def test_exactly_five_allowed_urls():
    assert len(ALLOWED_URLS) == 5


@pytest.mark.parametrize("url", sorted(ALLOWED_URLS))
def test_each_allowed_url_accepted(url):
    assert is_allowed_url(url)


@pytest.mark.parametrize(
    "variant",
    [
        "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth/",
        "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth?utm_source=x",
        "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth#returns",
        "HTTPS://GROWW.IN/mutual-funds/hdfc-mid-cap-fund-direct-growth",
        "  https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth  ",
    ],
)
def test_normalised_variants_accepted(variant):
    assert normalize_url(variant) == "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth"
    assert is_allowed_url(variant)


@pytest.mark.parametrize(
    "url",
    [
        "https://groww.in/mutual-funds/hdfc-flexi-cap-fund-direct-growth",  # other groww.in scheme
        "https://groww.in/mutual-funds/hdfc-mid-cap-fund-regular-growth",  # other plan
        "https://groww.in/mutual-funds",
        "http://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth",  # not https
        "https://www.groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth",  # other host
        "https://groww.in.evil.example/mutual-funds/hdfc-mid-cap-fund-direct-growth",
        "https://www.hdfcfund.com/",
        "https://www.amfiindia.com/",
    ],
)
def test_other_urls_rejected(url):
    assert not is_allowed_url(url)


def test_schemes_file_loads_and_matches_sources():
    sources = load_sources(SOURCES_YAML)
    catalog = load_schemes(SOURCES_YAML.with_name("schemes.yaml"), sources)
    assert catalog.by_source("groww-hdfc-mid-cap").short_name == "HDFC Mid Cap Fund"
    assert "expense_ratio" in catalog.fact_labels


def test_schemes_must_match_sources_one_to_one(tmp_path):
    sources = load_sources(SOURCES_YAML)
    data = yaml.safe_load(SOURCES_YAML.with_name("schemes.yaml").read_text(encoding="utf-8"))
    data["schemes"] = data["schemes"][:4]
    path = tmp_path / "schemes.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(RegistryError, match="one-to-one"):
        load_schemes(path, sources)
