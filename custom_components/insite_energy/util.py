"""Shared helpers for Insite Energy."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

# Matches the first signed number in a string, e.g. "14.67p" or "-1.2p".
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

# The portal formats reading dates as "2026/08/04 00:00", in local time.
_READING_DATE_FORMATS = ("%Y/%m/%d %H:%M", "%Y/%m/%d")


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
