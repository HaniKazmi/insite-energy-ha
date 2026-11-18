"""Shared helpers for Insite Energy."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

# Matches the first signed number in a string, e.g. "14.67p" or "-1.2p".
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

# The portal formats reading dates as "2026/08/04 00:00", in local time - or,
# on prepay accounts, day first as "27/10/2026 02:53". The two cannot be
# confused: %Y wants four digits, and no month or day has four.
_READING_DATE_FORMATS = (
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)


def parse_pence(value: object) -> float | None:
    """Convert a pence amount like "14.67p" into pounds.

    Returns None when there's no number to read, or when the text holds more
    than one — better an unknown state than a plausible-looking wrong price.
    """
    if value is None:
        return None

    # Thousands separators would otherwise split into two "numbers".
    numbers = _NUMBER_RE.findall(str(value).replace(",", ""))
    if len(numbers) != 1:
        return None

    try:
        # Decimal keeps 14.67p at exactly 0.1467 rather than 0.14670000000000002.
        return float(Decimal(numbers[0]) / Decimal(100))
    except InvalidOperation:
        return None


def prepay_balance(view_model: dict) -> float | None:
    """Total the credit on a prepay ("top-up") account.

    Prepay accounts leave CreditAccountBalance null. Each utility carries its
    own credit instead, as ActiveBalance ("66.27"), and the account's balance
    is their sum.

    Returns None when no utility reports one, or when any reported balance
    can't be read - a total silently missing one meter would look plausible
    and be wrong.
    """
    total = Decimal(0)
    found = False
    for utility in view_model.get("UtilityDetails") or []:
        if not isinstance(utility, dict):
            continue
        value = utility.get("ActiveBalance")
        if value is None:
            continue
        try:
            # Decimal, so 66.27 + 0.1 stays 66.37 rather than 66.36999999999999.
            # str() first: a nested dict or list then fails as InvalidOperation
            # rather than escaping as a TypeError.
            total += Decimal(str(value).strip())
        except InvalidOperation:
            return None
        if not total.is_finite():
            return None
        found = True
    return float(total) if found else None


def parse_reading_date(value: object, tzinfo) -> datetime | None:
    """Parse a meter reading date into an aware datetime.

    The portal reports local dates with no offset, so the caller supplies the
    zone to attach.
    """
    if not value:
        return None

    text = str(value).strip()
    for fmt in _READING_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tzinfo)
        except ValueError:
            continue
    return None


def _name_slug(name: str) -> str:
    """Return an identifier-safe form of a utility's display name."""
    return name.replace(" & ", "_").replace(" ", "_").lower()


def utility_key(utility: dict) -> str:
    """Return a stable identifier for a utility.

    Prefers the portal's own ShortName ("HH", "CO") so that renaming a
    utility upstream doesn't orphan its entities. Falls back to a slug of
    the display name when ShortName is missing.
    """
    short_name = utility.get("ShortName")
    if short_name:
        return str(short_name).strip().lower()
    return _name_slug(str(utility.get("Name") or "unknown"))
