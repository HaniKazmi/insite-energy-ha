"""Tests for the parsing helpers."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.insite_energy.util import (
    parse_pence,
    parse_reading_date,
    prepay_balance,
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


def test_parse_reading_date_day_first():
    """Prepay accounts report the date day first; it reads the same way."""
    assert parse_reading_date("27/10/2026 02:53", timezone.utc) == datetime(
        2026, 10, 27, 2, 53, tzinfo=timezone.utc
    )
    assert parse_reading_date("27/10/2026", timezone.utc) == datetime(
        2026, 10, 27, tzinfo=timezone.utc
    )


def test_parse_reading_date_day_first_is_not_month_first():
    """A day-first date that would also be a valid month-first one is not swapped."""
    assert parse_reading_date("04/08/2026 00:00", timezone.utc) == datetime(
        2026, 8, 4, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    "value", [None, "", "not a date", "2026-08-04T00:00:00", "32/10/2026 02:53"]
)
def test_parse_reading_date_rejects_junk(value):
    """Anything unparseable yields None rather than a bad state."""
    assert parse_reading_date(value, timezone.utc) is None


def _utilities(*balances):
    return {"UtilityDetails": [{"ActiveBalance": b} for b in balances]}


@pytest.mark.parametrize(
    ("view_model", "expected"),
    [
        (_utilities("66.27"), 66.27),
        # Two prepay meters on one account: the account holds both credits.
        (_utilities("66.27", "10.10"), 76.37),
        # In debt after emergency credit runs out.
        (_utilities("-3.50"), -3.5),
        (_utilities("0.0000"), 0.0),
        # A utility without a prepay balance is not one to total.
        ({"UtilityDetails": [{"ActiveBalance": "5"}, {"Name": "Cooling"}]}, 5.0),
        # Nothing reports a balance: unknown, not a fabricated zero.
        ({"UtilityDetails": [{"Name": "Cooling"}]}, None),
        ({"UtilityDetails": []}, None),
        ({"UtilityDetails": None}, None),
        ({}, None),
        # One unreadable balance spoils the total rather than being skipped.
        (_utilities("66.27", "n/a"), None),
        (_utilities("66.27", {"amount": 1}), None),
        (_utilities("NaN"), None),
        (_utilities("Infinity"), None),
        ({"UtilityDetails": ["junk", {"ActiveBalance": "1.5"}]}, 1.5),
    ],
)
def test_prepay_balance(view_model, expected):
    """Per-utility prepay credit totals to the account balance."""
    assert prepay_balance(view_model) == expected


def test_prepay_balance_avoids_float_artifacts():
    """Summed with Decimal, so pennies do not drift."""
    assert str(prepay_balance(_utilities("66.27", "0.1"))) == "66.37"


def test_utility_key_prefers_short_name():
    """ShortName survives an upstream rename, so it wins."""
    assert utility_key({"Name": "Heating & Hot Water", "ShortName": "HH"}) == "hh"
    assert utility_key({"Name": "Cooling", "ShortName": "CO"}) == "co"


def test_utility_key_falls_back_to_slug():
    """Without a ShortName we still produce a stable key."""
    assert utility_key({"Name": "Heating & Hot Water"}) == "heating_hot_water"
    assert utility_key({"Name": "Cooling"}) == "cooling"
    assert utility_key({}) == "unknown"
