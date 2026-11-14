import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make the project root importable and point the service at a throwaway DB
# before importing the app.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.TemporaryDirectory()
os.environ["TRACE_DB_PATH"] = str(Path(_TMP.name) / "test_trace.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.database import init_db  # noqa: E402

SAMPLES = ROOT / "samples"


@pytest.fixture(scope="session")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def upload(client, name):
    path = SAMPLES / name
    with open(path, "rb") as fh:
        return client.post(
            "/uploads",
            files={"file": (name, fh, "text/csv")},
        )


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_valid_upload(client):
    r = upload(client, "events_valid.csv")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["accepted"] is True
    assert body["new_events"] == 11
    assert body["duplicate_events"] == 0


def test_trace_board_hit_before_remove(client):
    r = client.get(
        "/trace",
        params={
            "child_part_no": "EC-100",
            "batch": "B20261101",
            "at_time": "2026-11-01T09:30:00Z",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_affected_products"] == 1
    product = body["affected_products"][0]
    assert product["root_part_no"] == "P-1001"
    # Multi-level path: EC-100 -> SA-200 -> P-1001, two INSTALL events.
    assert [s["action"] for s in product["path"]] == ["INSTALL", "INSTALL"]
    assert [s["event_no"] for s in product["path"]] == ["E001", "E002"]


def test_trace_miss_after_remove(client):
    r = client.get(
        "/trace",
        params={
            "child_part_no": "EC-100",
            "batch": "B20261101",
            "at_time": "2026-11-01T10:30:00Z",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_affected_products"] == 0
    assert body["affected_products"] == []


def test_trace_replacement_board_in_two_products_over_time(client):
    # At 11:30 only P-1001 exists with the replacement board.
    r = client.get(
        "/trace",
        params={
            "child_part_no": "EC-100",
            "batch": "B20261102",
            "at_time": "2026-11-01T11:30:00Z",
        },
    )
    roots = {p["root_part_no"] for p in r.json()["affected_products"]}
    assert roots == {"P-1001"}

    # At 13:00 the assembly has moved to P-1002.
    r = client.get(
        "/trace",
        params={
            "child_part_no": "EC-100",
            "batch": "B20261102",
            "at_time": "2026-11-01T13:00:00Z",
        },
    )
    roots = {p["root_part_no"] for p in r.json()["affected_products"]}
    assert roots == {"P-1002"}


def test_trace_multilevel_second_chain(client):
    r = client.get(
        "/trace",
        params={
            "child_part_no": "EC-200",
            "batch": "B20261101",
            "at_time": "2026-11-02T10:00:00Z",
        },
    )
    body = r.json()
    assert body["total_affected_products"] == 1
    product = body["affected_products"][0]
    assert product["root_part_no"] == "P-1003"
    assert [s["event_no"] for s in product["path"]] == ["E007", "E008"]

    # After E009 the board is removed and no longer hits.
    r = client.get(
        "/trace",
        params={
            "child_part_no": "EC-200",
            "batch": "B20261101",
            "at_time": "2026-11-02T12:00:00Z",
        },
    )
    assert r.json()["total_affected_products"] == 0


def test_duplicate_identical_retransmission(client):
    r = upload(client, "events_valid.csv")
    assert r.status_code == 201
    body = r.json()
    assert body["new_events"] == 0
    assert body["duplicate_events"] == 11


def test_conflicting_event_no_rejected(client):
    r = upload(client, "conflict_dup_event_no.csv")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["accepted"] is False
    assert "already exists with different content" in detail["reason"]
    # Old result still queryable.
    r2 = client.get(
        "/trace",
        params={
            "child_part_no": "EC-100",
            "batch": "B20261101",
            "at_time": "2026-11-01T09:30:00Z",
        },
    )
    assert r2.json()["total_affected_products"] == 1


def test_two_parents_rejected(client):
    r = upload(client, "invalid_two_parents.csv")
    assert r.status_code == 422
    errors = " ".join(r.json()["detail"]["errors"])
    assert "two parents" in errors


def test_orphan_remove_rejected(client):
    r = upload(client, "invalid_orphan_remove.csv")
    assert r.status_code == 422
    errors = " ".join(r.json()["detail"]["errors"])
    assert "no matching active INSTALL" in errors


def test_cycle_rejected(client):
    r = upload(client, "invalid_cycle.csv")
    assert r.status_code == 422
    errors = " ".join(r.json()["detail"]["errors"])
    assert "cycle" in errors


def test_backfill_orphan_rejected_and_old_data_intact(client):
    before = client.get("/events").json()
    r = upload(client, "backfill_orphan_rejected.csv")
    assert r.status_code == 422
    after = client.get("/events").json()
    assert before == after  # nothing changed


def test_backfill_valid_repair_accepted(client):
    r = upload(client, "backfill_repair_accepted.csv")
    assert r.status_code == 201, r.text
    assert r.json()["new_events"] == 2

    # Point-in-time query before E001 still reflects the backfilled chain:
    # at 17:30 on 10-31 the board is mounted on SA-200 (open chain, no P- yet).
    r = client.get(
        "/trace",
        params={
            "child_part_no": "EC-100",
            "batch": "B20261101",
            "at_time": "2026-10-31T17:30:00Z",
        },
    )
    body = r.json()
    assert body["total_affected_products"] == 0
    assert len(body["open_chains"]) == 1
    assert body["open_chains"][0]["root_part_no"] == "SA-200"


def test_bad_timestamp_400(client):
    r = client.get(
        "/trace",
        params={
            "child_part_no": "EC-100",
            "batch": "B20261101",
            "at_time": "not-a-timestamp",
        },
    )
    assert r.status_code == 400


def test_missing_column_400(client):
    r = client.post(
        "/uploads",
        files={"file": ("x.csv", b"event_no,action\nE1,INSTALL\n", "text/csv")},
    )
    assert r.status_code == 400
    assert "missing required columns" in r.json()["detail"]["errors"][0]


def test_uploads_audit_listing(client):
    r = client.get("/uploads")
    assert r.status_code == 200
    batches = r.json()
    accepted = [b for b in batches if b["accepted"]]
    rejected = [b for b in batches if not b["accepted"]]
    assert len(accepted) >= 3
    assert len(rejected) >= 4
