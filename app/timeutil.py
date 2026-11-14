"""Time helpers.

All event times are UTC. CSV values may carry an explicit offset or ``Z``;
naive timestamps are interpreted as UTC. Internally we keep both an ISO-8601
string and microseconds since the epoch, the latter giving a total ordering
for event-time replay (ties broken by event number).
"""

from __future__ import annotations

from datetime import datetime, timezone
from fastapi import HTTPException


class TimeParseError(ValueError):
    pass


def parse_utc(value: str, *, field: str = "event_ts") -> datetime:
    text = (value or "").strip()
    if not text:
        raise TimeParseError(f"{field} is empty")
    iso = text.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise TimeParseError(f"{field}={value!r} is not a valid ISO-8601 timestamp") from exc
    if dt.tzinfo is None:
        # Naive timestamps are assumed to already be UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_utc_or_400(value: str, *, field: str = "at_time") -> datetime:
    try:
        return parse_utc(value, field=field)
    except TimeParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def to_micros(dt: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return int((dt - epoch).total_seconds() * 1_000_000)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
