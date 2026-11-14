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
    # Seed the base log.
    r = _post_file(client, "events_valid.csv")
    assert r.status_code == 201
    base_count = 11

    errors: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            events = client.get("/events").json()
            # Snapshot must be a complete version: either the old 11/13 rows
            # or the whole accepted set, never a torn intermediate count.
            trace = client.get(
                "/trace",
                params={
                    "child_part_no": "EC-100",
                    "batch": "B20261101",
                    "at_time": "2026-11-01T09:30:00Z",
                },
            ).json()
            n = len(events)
            if n not in (base_count, base_count + 2):
                errors.append(f"torn event count: {n}")
            if trace["total_affected_products"] not in (0, 1):
                errors.append("impossible trace result")

    t = threading.Thread(target=reader)
    t.start()
    try:
        # Accepted backfill adds 2 events; invalid uploads add 0.
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
    finally:
        stop.set()
        t.join()

    assert not errors, errors
    final = client.get("/events").json()
    assert len(final) == base_count + 2  # only the valid backfill was added
