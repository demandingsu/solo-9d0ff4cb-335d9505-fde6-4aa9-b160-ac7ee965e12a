"""Point-in-time trace correctness under parent-batch ambiguity.

These tests exercise ``trace_batch`` directly, so they need no database and
cannot interfere with the API test fixtures.

Scenario shape
--------------
The defect report names a board batch, e.g. ``EC-600 / B600``.  The log
records the board being installed into ``SA-600``, and records *several*
batches of ``SA-600`` being installed into different finished products.  The
board install event says nothing about which ``SA-600`` instance physically
received it, so every active ``SA-600`` instance is a log-compatible
attribution:

* different products remain possible  -> 待核实 (pending), not confirmed;
* all plausible instances end in the *same* product  -> 确定受影响 (confirmed).

A batch that was never installed (or was already removed at the query time)
must produce an empty result, never a false positive.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.csvparse import parse_csv  # noqa: E402
from app.replay import trace_batch  # noqa: E402
from app.timeutil import parse_utc, to_micros  # noqa: E402

SAMPLES = ROOT / "samples"


def load(name: str):
    return parse_csv((SAMPLES / name).read_bytes())


def trace(events, part: str, batch: str, at: str) -> dict:
    return trace_batch(events, part, batch, to_micros(parse_utc(at)))


def roots(entries: list[dict]) -> list[tuple[str, str]]:
    return [(e["root_part_no"], e["root_batch"]) for e in entries]


def event_nos(entries: list[dict]) -> list[list[str]]:
    return [[s["event_no"] for s in e["path"]] for e in entries]


def confirmed(trace_result: dict) -> list[dict]:
    """Confirmed products, under the trace_batch internal key."""
    return trace_result["products"]


def pending(trace_result: dict) -> list[dict]:
    """Products awaiting verification, under the internal key."""
    return trace_result["pending_products"]


# --------------------------------------------------------------------------- #
# Starting point must be the real target batch
# --------------------------------------------------------------------------- #


def test_never_installed_batch_reports_nothing():
    events = load("events_valid.csv")
    result = trace(events, "EC-100", "B-DOES-NOT-EXIST", "2026-11-01T09:30:00Z")

    assert result["batch_found"] is False
    assert confirmed(result) == []
    assert pending(result) == []
    assert result["open_chains"] == []
    assert result["total_affected_products"] == 0
    assert result["compatible_path_count"] == 0
    assert result["ambiguous"] is False


def test_other_batch_of_same_part_is_not_used_as_starting_point():
    # B20261102 is installed into the same SA-200 but at a later time; a query
    # for the older batch at 09:30 must not borrow the newer install.
    events = load("events_valid.csv")
    result = trace(events, "EC-100", "B20261102", "2026-11-01T09:30:00Z")

    assert result["batch_found"] is False
    assert confirmed(result) == []


def test_batch_already_removed_is_not_found_at_later_query():
    events = load("events_valid.csv")
    result = trace(events, "EC-100", "B20261101", "2026-11-01T10:30:00Z")

    assert result["batch_found"] is False
    assert result["total_affected_products"] == 0
    assert result["open_chains"] == []


# --------------------------------------------------------------------------- #
# Ambiguity: same intermediate part number, different batches, two products
# --------------------------------------------------------------------------- #


def test_same_middleware_part_in_two_products_is_pending_not_confirmed():
    events = load("ambiguous_same_part_two_products.csv")
    result = trace(events, "EC-600", "B600", "2026-11-06T09:30:00Z")

    assert result["batch_found"] is True
    assert result["ambiguous"] is True
    assert result["total_affected_products"] == 0
    assert confirmed(result) == []

    # Both products are reported as candidates, each with its own event path
    # and the intermediate batch that makes the attribution possible.
    assert set(roots(pending(result))) == {
        ("P-6001", "LOT-A"),
        ("P-6002", "LOT-B"),
    }
    assert result["total_pending_products"] == 2
    paths = event_nos(pending(result))
    assert sorted(paths) == [["A003", "A001"], ["A003", "A002"]]
    # No path invents a REMOVE event; only active installs are walked.
    for entry in pending(result):
        assert all(s["action"] == "INSTALL" for s in entry["path"])


def test_removal_resolves_ambiguity_at_query_time():
    # After A004 removes the LOT-B instance, only LOT-A -> P-6001 remains.
    events = load("ambiguous_same_part_two_products.csv") + load(
        "ambiguity_remove_later.csv"
    )

    before = trace(events, "EC-600", "B600", "2026-11-06T09:30:00Z")
    assert before["ambiguous"] is True
    assert before["total_affected_products"] == 0

    after = trace(events, "EC-600", "B600", "2026-11-06T12:00:00Z")
    assert after["ambiguous"] is False
    assert after["total_affected_products"] == 1
    assert after["total_pending_products"] == 0
    assert roots(confirmed(after)) == [("P-6001", "LOT-A")]
    assert event_nos(confirmed(after)) == [["A003", "A001"]]


# --------------------------------------------------------------------------- #
# Convergence: ambiguous instances that all end in one product
# --------------------------------------------------------------------------- #


def test_convergent_instance_batches_count_as_confirmed():
    events = load("convergent_same_product.csv")
    result = trace(events, "EC-601", "B601", "2026-11-07T09:30:00Z")

    assert result["batch_found"] is True
    # Two log-compatible attributions (LOT-C and LOT-D), both ending in P-6010.
    assert result["compatible_path_count"] == 2
    assert result["ambiguous"] is False
    assert result["total_pending_products"] == 0
    assert result["total_affected_products"] == 1

    # The product is reported once, carrying both compatible event paths and
    # both plausible intermediate batches for auditing; it still counts as a
    # single affected product.
    assert {p["root_part_no"] for p in confirmed(result)} == {"P-6010"}
    entry = confirmed(result)[0]
    assert entry["root_batches"] == ["LOT-C", "LOT-D"]
    assert sorted(
        [[s["event_no"] for s in p] for p in entry["compatible_paths"]]
    ) == [["V003", "V001"], ["V003", "V002"]]


def test_backfilled_extra_parent_instance_splits_confirmation():
    # A backfilled earlier event mounts a third SA-601 instance under P-6011.
    events = load("convergent_same_product.csv") + load(
        "backfill_split_parent_instance.csv"
    )
    result = trace(events, "EC-601", "B601", "2026-11-07T09:30:00Z")

    # Now P-6011 is also plausible, so nothing is certain. P-6010 still
    # carries its two convergent paths but is only one pending product.
    assert result["ambiguous"] is True
    assert result["total_affected_products"] == 0
    assert {p["root_part_no"] for p in pending(result)} == {"P-6010", "P-6011"}
    assert result["total_pending_products"] == 2
    p6010 = next(p for p in pending(result) if p["root_part_no"] == "P-6010")
    assert p6010["root_batches"] == ["LOT-C", "LOT-D"]


# --------------------------------------------------------------------------- #
# Open (in-progress) chains stay separate and are never "confirmed"
# --------------------------------------------------------------------------- #


def test_in_progress_chain_is_open_and_not_confirmed():
    events = load("convergent_same_product.csv")[:3]  # V001,V002,V003 only
    # At 08:30 the SA-601 instances are mounted but the board is not yet.
    result = trace(events, "EC-601", "B601", "2026-11-07T08:30:00Z")
    assert result["batch_found"] is False
    assert result["open_chains"] == []


def test_unrelated_open_chain_does_not_block_confirmation():
    # W003 opens an unrelated SA-799/EC-799 chain; it is not an attribution of
    # EC-701/B702 and must not make the result ambiguous.
    fresh = parse_csv(
        b"event_no,event_ts,parent_part_no,child_part_no,child_batch,action\n"
        b"W001,2026-11-08T08:00:00Z,P-7001,SA-701,LOT-P,INSTALL\n"
        b"W002,2026-11-08T08:10:00Z,SA-701,EC-701,B701,INSTALL\n"
        b"W003,2026-11-08T08:20:00Z,SA-799,EC-799,B799,INSTALL\n"
        b"W004,2026-11-08T08:30:00Z,SA-701,EC-701,B702,INSTALL\n"
    )
    result = trace(fresh, "EC-701", "B702", "2026-11-08T09:00:00Z")
    assert result["ambiguous"] is False
    assert result["total_affected_products"] == 1
    assert roots(confirmed(result)) == [("P-7001", "LOT-P")]
    assert result["open_chains"] == []


def test_product_reached_and_open_branch_means_pending():
    # The same part number appears twice: one instance inside a product, one
    # still in progress.  The board could be in either, so it is not certain.
    fresh = parse_csv(
        b"event_no,event_ts,parent_part_no,child_part_no,child_batch,action\n"
        b"W101,2026-11-09T08:00:00Z,P-7101,SA-711,LOT-1,INSTALL\n"
        b"W102,2026-11-09T08:10:00Z,EC-711,SA-711,LOT-2,INSTALL\n"
        b"W103,2026-11-09T08:20:00Z,SA-711,EC-711,B711,INSTALL\n"
    )
    result = trace(fresh, "EC-711", "B711", "2026-11-09T09:00:00Z")

    assert result["batch_found"] is True
    assert result["ambiguous"] is True
    assert result["total_affected_products"] == 0
    assert roots(pending(result)) == [("P-7101", "LOT-1")]
    assert roots(result["open_chains"]) == [("EC-711", "LOT-2")]
