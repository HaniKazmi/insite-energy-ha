"""Tests for the parsing helpers."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.insite_energy.util import (
    legacy_utility_slug,
    parse_pence,
    parse_reading_date,
    utility_key,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("14.67p", 0.1467),
        ("86.68p", 0.8668),
        ("24.53", 0.2453),
        ("0p", 0.0),
        # A credit must keep its sign; the old regex stripped the minus.
        ("-1.20p", -0.012),
        # Thousands separators must not truncate the value.
        ("1,234.5p", 12.345),
        # Ambiguous text yields None rather than a plausible wrong price.
        ("Tier 1: 14.67p", None),
        (14.67, 0.1467),
        (None, None),
        ("", None),
        ("n/a", None),
    ],
)
def test_parse_pence(value, expected):
    """Pence strings convert to pounds, sign included."""
    assert parse_pence(value) == expected


def test_parse_pence_avoids_float_artifacts():
    """Decimal division keeps the value exact."""
    assert parse_pence("14.67p") == 0.1467
    assert str(parse_pence("14.67p")) == "0.1467"


def test_parse_reading_date():
    """The portal's slash-separated local date parses to an aware datetime."""
    result = parse_reading_date("2026/08/04 00:00", timezone.utc)
    assert result == datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)
    assert result.tzinfo is not None


def test_parse_reading_date_without_time():
    """A bare date is accepted too."""
    assert parse_reading_date("2026/08/04", timezone.utc) == datetime(
        2026, 8, 4, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("value", [None, "", "not a date", "2026-08-04T00:00:00"])
def test_parse_reading_date_rejects_junk(value):
    """Anything unparseable yields None rather than a bad state."""
    assert parse_reading_date(value, timezone.utc) is None


def test_utility_key_prefers_short_name():
    """ShortName survives an upstream rename, so it wins."""
    assert utility_key({"Name": "Heating & Hot Water", "ShortName": "HH"}) == "hh"
    assert utility_key({"Name": "Cooling", "ShortName": "CO"}) == "co"


def test_utility_key_falls_back_to_slug():
    """Without a ShortName we still produce a stable key."""
    assert utility_key({"Name": "Heating & Hot Water"}) == "heating_hot_water"
    assert utility_key({}) == "unknown"


def test_legacy_utility_slug():
    """The v1 slug is reproduced exactly, so migration can recognise it."""
    assert legacy_utility_slug("Heating & Hot Water") == "heating_hot_water"
    assert legacy_utility_slug("Cooling") == "cooling"
