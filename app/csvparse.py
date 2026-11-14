"""CSV parsing and normalisation.

Accepted headers (Chinese or English, order irrelevant, extra columns
ignored):

    event_no        事件号
    event_ts        UTC时间 / 时间
    parent_part_no  父件号
    child_part_no   子件号
    child_batch     子件批次
    action          动作 (INSTALL/装入  REMOVE/拆出)
"""

from __future__ import annotations

import csv
import hashlib
import io

from .replay import Event
from .timeutil import TimeParseError, parse_utc, to_iso, to_micros

HEADER_ALIASES = {
    "event_no": "event_no",
    "事件号": "event_no",
    "eventno": "event_no",
    "event_ts": "event_ts",
    "eventtime": "event_ts",
    "utc时间": "event_ts",
    "时间": "event_ts",
    "utc_time": "event_ts",
    "parent_part_no": "parent_part_no",
    "父件号": "parent_part_no",
    "parentpartno": "parent_part_no",
    "parent": "parent_part_no",
    "child_part_no": "child_part_no",
    "子件号": "child_part_no",
    "childpartno": "child_part_no",
    "child": "child_part_no",
    "child_batch": "child_batch",
    "子件批次": "child_batch",
    "childbatch": "child_batch",
    "batch": "child_batch",
    "批次": "child_batch",
    "action": "action",
    "动作": "action",
}

ACTION_ALIASES = {
    "INSTALL": "INSTALL",
    "INSTALLED": "INSTALL",
    "装入": "INSTALL",
    "安装": "INSTALL",
    "装配": "INSTALL",
    "REMOVE": "REMOVE",
    "REMOVED": "REMOVE",
    "拆出": "REMOVE",
    "拆卸": "REMOVE",
    "拆除": "REMOVE",
}

REQUIRED_FIELDS = (
    "event_no",
    "event_ts",
    "parent_part_no",
    "child_part_no",
    "child_batch",
    "action",
)


class CsvError(ValueError):
    pass


def _norm_header(name: str) -> str:
    return (name or "").strip().lstrip("﻿").lower().replace(" ", "_")


def content_hash(
    event_ts: str,
    parent_part_no: str,
    child_part_no: str,
    child_batch: str,
    action: str,
) -> str:
    payload = "|".join(
        [event_ts, parent_part_no, child_part_no, child_batch, action]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_csv(raw: bytes) -> list[Event]:
    """Parse uploaded bytes into validated, normalised Event objects."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvError("file must be UTF-8 encoded text") from exc

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CsvError("CSV file is empty or has no header row")

    mapping: dict[str, str] = {}
    for raw_name in reader.fieldnames:
        key = _norm_header(raw_name)
        canonical = HEADER_ALIASES.get(key)
        if canonical:
            mapping[canonical] = raw_name
    missing = [f for f in REQUIRED_FIELDS if f not in mapping]
    if missing:
        raise CsvError(
            "missing required columns: "
            + ", ".join(missing)
            + f"; detected columns: {reader.fieldnames}"
        )

    events: list[Event] = []
    seen_in_file: dict[str, int] = {}
    for line_no, row in enumerate(reader, start=2):
        if row is None:
            continue
        # Skip completely blank lines.
        if not any((v or "").strip() for v in row.values()):
            continue

        def cell(field: str) -> str:
            return (row.get(mapping[field]) or "").strip()

        event_no = cell("event_no")
        if not event_no:
            raise CsvError(f"line {line_no}: event_no is empty")
        if event_no in seen_in_file:
            raise CsvError(
                f"line {line_no}: duplicate event_no {event_no!r} inside the file "
                f"(first seen on line {seen_in_file[event_no]})"
            )
        seen_in_file[event_no] = line_no

        for field in ("parent_part_no", "child_part_no", "child_batch"):
            if not cell(field):
                raise CsvError(f"line {line_no} (event {event_no}): {field} is empty")

        raw_action = cell("action").upper()
        action = ACTION_ALIASES.get(raw_action)
        if action is None:
            raise CsvError(
                f"line {line_no} (event {event_no}): unknown action {cell('action')!r}; "
                "use INSTALL/装入 or REMOVE/拆出"
            )

        try:
            dt = parse_utc(cell("event_ts"), field=f"event_ts (event {event_no})")
        except TimeParseError as exc:
            raise CsvError(f"line {line_no}: {exc}") from exc

        events.append(
            Event(
                event_no=event_no,
                event_us=to_micros(dt),
                event_ts=to_iso(dt),
                parent_part_no=cell("parent_part_no"),
                child_part_no=cell("child_part_no"),
                child_batch=cell("child_batch"),
                action=action,
            )
        )

    if not events:
        raise CsvError("CSV file contains no data rows")
    return events
