"""Concurrency: queries racing with uploads must always see a complete log."""

import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.TemporaryDirectory()
os.environ["TRACE_DB_PATH"] = str(Path(_TMP.name) / "concurrency_trace.db")

from app.main import app  # noqa: E402
from app.database import init_db  # noqa: E402

SAMPLES = ROOT / "samples"


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def _post_file(client, name):
    with open(SAMPLES / name, "rb") as fh:
        return client.post(
            "/uploads", files={"file": (name, fh, "text/csv")}
        )


def test_concurrent_reads_never_see_partial_log(client):
    # Seed the base log (earlier test modules may already have added events;
    # measure the committed baseline instead of assuming it).
    _post_file(client, "events_valid.csv")
    base_count = len(client.get("/events").json())

    errors: list[str] = []
    stop = threading.Event()
    allowed_counts = {base_count}
    lock = threading.Lock()

    def reader():
        while not stop.is_set():
            events = client.get("/events").json()
            # Snapshot must be a complete version: the old baseline or the
            # count after a fully committed upload, never a torn intermediate.
            trace = client.get(
                "/trace",
                params={
                    "child_part_no": "EC-100",
                    "batch": "B20261101",
                    "at_time": "2026-11-01T09:30:00Z",
                },
            ).json()
            n = len(events)
            with lock:
                known = set(allowed_counts)
            if n not in known:
                errors.append(f"torn event count: {n} not in {sorted(known)}")
            if trace["total_affected_products"] not in (0, 1):
                errors.append("impossible trace result")

    t = threading.Thread(target=reader)
    t.start()
    expected = base_count
    try:
        # Accepted uploads add their reported new_events atomically; invalid
        # uploads add nothing.
        for name in (
            "backfill_repair_accepted.csv",
            "invalid_two_parents.csv",
            "invalid_orphan_remove.csv",
            "invalid_cycle.csv",
            "conflict_dup_event_no.csv",
            "events_valid.csv",  # all duplicates
        ):
            resp = _post_file(client, name)
            assert resp.status_code in (201, 422)
            if resp.status_code == 201:
                expected += resp.json()["new_events"]
                with lock:
                    allowed_counts.add(expected)
    finally:
        stop.set()
        t.join()

    assert not errors, errors
    final = client.get("/events").json()
    assert len(final) == expected  # every committed event, nothing torn
