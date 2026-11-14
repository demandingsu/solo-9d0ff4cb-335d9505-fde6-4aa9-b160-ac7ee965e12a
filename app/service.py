"""Upload orchestration: idempotency, whole-log replay validation and commit."""

from __future__ import annotations

import json
import sqlite3

from .csvparse import content_hash
from .database import get_conn, write_lock
from .replay import Event, replay


class UploadRejected(Exception):
    def __init__(self, reason: str, errors: list[str]):
        super().__init__(reason)
        self.reason = reason
        self.errors = errors


def _row_to_event(r: sqlite3.Row) -> Event:
    return Event(
        event_no=r["event_no"],
        event_us=r["event_us"],
        event_ts=r["event_ts"],
        parent_part_no=r["parent_part_no"],
        child_part_no=r["child_part_no"],
        child_batch=r["child_batch"],
        action=r["action"],
    )


def _insert_event(conn: sqlite3.Connection, ev: Event, batch_id: int) -> None:
    conn.execute(
        """
        INSERT INTO events (event_no, event_ts, event_us, parent_part_no,
                            child_part_no, child_batch, action, content_hash,
                            upload_batch)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ev.event_no,
            ev.event_ts,
            ev.event_us,
            ev.parent_part_no,
            ev.child_part_no,
            ev.child_batch,
            ev.action,
            content_hash(
                ev.event_ts,
                ev.parent_part_no,
                ev.child_part_no,
                ev.child_batch,
                ev.action,
            ),
            batch_id,
        ),
    )


def _record_audit(
    filename: str,
    accepted: bool,
    new_events: int,
    duplicates: int,
    detail: dict,
) -> None:
    """Audit rows are written in their own transaction so a rejected upload
    leaves an audit trail while all event changes are rolled back."""
    with get_conn() as audit_conn:
        audit_conn.execute(
            "INSERT INTO upload_batches (filename, accepted, new_events, "
            "duplicate_events, detail) VALUES (?, ?, ?, ?, ?)",
            (
                filename,
                1 if accepted else 0,
                new_events,
                duplicates,
                json.dumps(detail, ensure_ascii=False),
            ),
        )
        audit_conn.commit()


def upload_events(filename: str, incoming: list[Event]) -> dict:
    """Validate and commit one CSV upload atomically.

    On rejection nothing is written to ``events``, so previously accepted log
    versions stay fully queryable; an audit row is still recorded.
    """
    with write_lock:  # serialise uploads against each other
        with get_conn() as conn:
            # Reserve the writer up front so a second upload queues instead
            # of colliding at commit time.
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing_rows = conn.execute(
                    "SELECT event_no, event_us, event_ts, parent_part_no, "
                    "child_part_no, child_batch, action FROM events"
                ).fetchall()
                existing_events = [_row_to_event(r) for r in existing_rows]
                existing = {r["event_no"]: dict(r) for r in existing_rows}
                for row in existing.values():
                    row.pop("event_us", None)

                # 1) Idempotency / conflict check on event numbers.
                duplicates = 0
                new_events: list[Event] = []
                for ev in incoming:
                    old = existing.get(ev.event_no)
                    if old is None:
                        new_events.append(ev)
                        continue
                    same = (
                        old["event_ts"] == ev.event_ts
                        and old["parent_part_no"] == ev.parent_part_no
                        and old["child_part_no"] == ev.child_part_no
                        and old["child_batch"] == ev.child_batch
                        and old["action"] == ev.action
                    )
                    if not same:
                        conn.rollback()
                        reason = (
                            f"event_no {ev.event_no!r} already exists with "
                            "different content"
                        )
                        errors = [
                            f"stored: {json.dumps(old, ensure_ascii=False)}",
                            "upload: "
                            f"event_ts={ev.event_ts}, parent={ev.parent_part_no}, "
                            f"child={ev.child_part_no}, batch={ev.child_batch}, "
                            f"action={ev.action}",
                        ]
                        _record_audit(
                            filename,
                            False,
                            0,
                            duplicates,
                            {"reason": reason, "errors": errors},
                        )
                        raise UploadRejected(reason, errors)
                    duplicates += 1

                # 2) Replay the *whole merged* log in event-time order; the
                #    same rules apply to backfilled early repair events.
                merged = existing_events + new_events
                result = replay(merged)
                if result.errors:
                    conn.rollback()
                    _record_audit(
                        filename,
                        False,
                        0,
                        duplicates,
                        {"reason": "structural validation failed",
                         "errors": result.errors},
                    )
                    raise UploadRejected(
                        "merged log fails structural validation; upload rejected",
                        result.errors,
                    )

                # 3) Insert the new events and the audit row atomically.
                cur = conn.execute(
                    "INSERT INTO upload_batches (filename, accepted, new_events, "
                    "duplicate_events, detail) VALUES (?, 1, ?, ?, ?)",
                    (
                        filename,
                        len(new_events),
                        duplicates,
                        json.dumps(
                            {
                                "received": len(incoming),
                                "new": len(new_events),
                                "duplicate": duplicates,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                batch_id = cur.lastrowid
                for ev in new_events:
                    _insert_event(conn, ev, batch_id)
                conn.commit()
            except UploadRejected:
                raise
            except Exception:
                conn.rollback()
                raise

    return {
        "accepted": True,
        "filename": filename,
        "received_events": len(incoming),
        "new_events": len(new_events),
        "duplicate_events": duplicates,
        "message": (
            f"{len(new_events)} new event(s) accepted; "
            f"{duplicates} identical retransmission(s) skipped"
        ),
    }
