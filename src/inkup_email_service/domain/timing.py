"""Parse Shiprocket delivery timestamps.

Shiprocket sends timestamps without a timezone, in Indian Standard Time, in a
few different formats, e.g. ``"23 05 2023 11:43:52"`` (``current_timestamp``)
and ``"2023-05-23 11:43:46"`` (scan ``date``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_IST = timezone(timedelta(hours=5, minutes=30), "IST")

_FORMATS = (
    "%d %m %Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d %b %Y %H:%M:%S",
    "%d %B %Y %H:%M:%S",
    "%Y-%m-%d",
)


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # Slim containers may lack tzdata; Shiprocket is IST in practice.
        return _IST


def parse_delivery_time(value: str | None, tz_name: str = "Asia/Kolkata") -> datetime | None:
    """Return the delivery time as an aware UTC datetime, or ``None`` if unparseable."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None

    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in _FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_zone(tz_name))
    return parsed.astimezone(UTC)
