"""Event-time replay, structural validation and trace computation.

The log is an append-only set of INSTALL/REMOVE events. Uploads may arrive out
of order (backfilled repair events), therefore *every* upload is validated by
replaying the whole merged log in event-time order rather than trusting upload
order.

Validation rules (any violation rejects the entire upload):
* REMOVE without a preceding matching INSTALL (无对应装入的拆出);
* INSTALL of a child (part, batch) that is already mounted somewhere else and
  has not been removed first — the child would simultaneously belong to two
  parents (子件同时归属两处);
* REMOVE whose parent does not match the location recorded at INSTALL;
* an edge that would close an assembly cycle (装配环).

Ties on event time are broken by event number so replay is deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import PRODUCT_PREFIX


@dataclass(frozen=True)
class Event:
    event_no: str
    event_us: int
    event_ts: str
    parent_part_no: str
    child_part_no: str
    child_batch: str
    action: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.child_part_no, self.child_batch)

    @property
    def edge(self) -> tuple[str, str, str]:
        """(parent, child part, child batch) edge identity."""
        return (self.parent_part_no, self.child_part_no, self.child_batch)


@dataclass
class MountInfo:
    """Where a child (part, batch) is currently mounted."""

    parent: str
    install_event_no: str
    install_us: int


@dataclass
class ReplayResult:
    errors: list[str]
    # child key -> current mount (empty when validation failed beyond repair)
    mounts: dict[tuple[str, str], MountInfo]
    # edge -> INSTALL event (an edge can be installed/removed repeatedly;
    # the most recent INSTALL is stored, all events remain in the log anyway)
    installed_edges: dict[tuple[str, str, str], Event]


def _ordered(events: list[Event]) -> list[Event]:
    return sorted(events, key=lambda e: (e.event_us, e.event_no))


def _reaches(
    start: str,
    target_parent: str,
    current_children: dict[str, list[tuple[str, str]]],
) -> bool:
    """Return True if ``target_parent`` is reachable downward from ``start``."""
    stack = [start]
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node == target_parent:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(p for p, _b in current_children.get(node, ()))
    return False


def replay(events: list[Event]) -> ReplayResult:
    """Replay all events in event-time order and collect every violation."""
    errors: list[str] = []
    mounts: dict[tuple[str, str], MountInfo] = {}
    current_children: dict[str, list[tuple[str, str]]] = {}
    installed_edges: dict[tuple[str, str, str], Event] = {}

    for ev in _ordered(events):
        if ev.action == "INSTALL":
            existing = mounts.get(ev.key)
            if existing is not None:
                errors.append(
                    f"event {ev.event_no}: {ev.child_part_no} batch {ev.child_batch} "
                    f"is already mounted under {existing.parent} "
                    f"(installed by {existing.install_event_no}); "
                    "installing it again would place the child in two parents at once"
                )
                continue
            # Cycle guard: the parent must not already be a descendant of the
            # child, otherwise adding this edge closes an assembly loop.
            if _reaches(ev.child_part_no, ev.parent_part_no, current_children):
                errors.append(
                    f"event {ev.event_no}: installing {ev.child_part_no} "
                    f"(batch {ev.child_batch}) under {ev.parent_part_no} "
                    "would create an assembly cycle"
                )
                continue
            mounts[ev.key] = MountInfo(ev.parent_part_no, ev.event_no, ev.event_us)
            current_children.setdefault(ev.parent_part_no, []).append(
                (ev.child_part_no, ev.child_batch)
            )
            installed_edges[ev.edge] = ev
        else:  # REMOVE
            existing = mounts.get(ev.key)
            if existing is None:
                errors.append(
                    f"event {ev.event_no}: REMOVE of {ev.child_part_no} batch "
                    f"{ev.child_batch} has no matching active INSTALL"
                )
                continue
            if existing.parent != ev.parent_part_no:
                errors.append(
                    f"event {ev.event_no}: REMOVE names parent {ev.parent_part_no} "
                    f"but the child is mounted under {existing.parent} "
                    f"(installed by {existing.install_event_no})"
                )
                continue
            mounts.pop(ev.key)
            children = current_children.get(ev.parent_part_no, [])
            try:
                children.remove((ev.child_part_no, ev.child_batch))
            except ValueError:
                pass
            installed_edges.pop(ev.edge, None)

    return ReplayResult(errors=errors, mounts=mounts, installed_edges=installed_edges)


# --------------------------------------------------------------------------- #
# Trace at a point in time
# --------------------------------------------------------------------------- #


@dataclass
class ActiveEdge:
    parent: str
    child_part: str
    child_batch: str
    install: Event
    remove: Optional[Event]

    @property
    def edge(self) -> tuple[str, str, str]:
        return (self.parent, self.child_part, self.child_batch)


def active_edges_at(events: list[Event], at_us: int) -> list[ActiveEdge]:
    """Pair INSTALL/REMOVE events and return edges active at ``at_us``.

    An edge is active when its INSTALL happened at or before the query moment
    and it has no REMOVE at or before that moment. Events occurring later do
    not affect the answer, which is what makes backfilled later events
    invisible to an earlier point-in-time query.
    """
    installs: dict[tuple[str, str, str], list[Event]] = {}
    removes: dict[tuple[str, str, str], list[Event]] = {}
    for ev in _ordered(events):
        if ev.event_us > at_us:
            break
        bucket = installs if ev.action == "INSTALL" else removes
        bucket.setdefault(ev.edge, []).append(ev)

    edges: list[ActiveEdge] = []
    for edge, ins_list in installs.items():
        rem_list = removes.get(edge, [])
        # Repeated install/remove of the same edge: walk pairs in order.
        i = j = 0
        while i < len(ins_list):
            ins = ins_list[i]
            i += 1
            rem = rem_list[j] if j < len(rem_list) else None
            if rem is not None:
                j += 1
            if rem is None or rem.event_us > at_us:
                edges.append(
                    ActiveEdge(edge[0], edge[1], edge[2], ins, rem)
                )
    return edges


def _step(ev: Event, action_label: str) -> dict:
    return {
        "event_no": ev.event_no,
        "event_ts": ev.event_ts,
        "action": action_label,
        "parent_part_no": ev.parent_part_no,
        "child_part_no": ev.child_part_no,
        "child_batch": ev.child_batch,
    }


def trace_batch(
    events: list[Event],
    child_part_no: str,
    child_batch: str,
    at_us: int,
) -> dict:
    """Find every finished product containing the queried child batch at
    ``at_us`` and return the multi-level assembly event path for each.

    A path walks upward from the queried batch through every active parent
    edge. Paths ending at a ``P-`` serial are affected finished products; the
    rest are open chains whose top assembly is not (yet) a finished product.
    """
    edges = active_edges_at(events, at_us)

    # parent part -> list of ActiveEdge objects mounted under it
    by_parent: dict[str, list[ActiveEdge]] = {}
    # child part -> list of edges (batch identity is carried per edge)
    by_child_part: dict[str, list[ActiveEdge]] = {}
    for e in edges:
        by_parent.setdefault(e.parent, []).append(e)
        by_child_part.setdefault(e.child_part, []).append(e)

    products: list[dict] = []
    open_chains: list[dict] = []
    seen_product_paths: set[tuple[str, tuple]] = set()

    def walk(part: str, batch: str, steps: list[dict], guard: frozenset) -> None:
        parents = by_child_part.get(part, [])
        # Choose edges whose batch matches when the caller tracks a batch;
        # above the first level the batch of an intermediate assembly is not
        # encoded in the child event, so fall back to all matching edges
        # (over-approximation: better to over-report candidates than miss a
        # unit containing the defective batch).
        candidates = [e for e in parents if e.child_batch == batch] or list(parents)
        if not candidates:
            chain = {
                "root_part_no": part,
                "root_batch": batch,
                "is_finished_product": part.startswith(PRODUCT_PREFIX),
                "path": steps,
            }
            if part.startswith(PRODUCT_PREFIX):
                sig = (part, tuple(s["event_no"] for s in steps))
                if sig not in seen_product_paths:
                    seen_product_paths.add(sig)
                    products.append(chain)
            else:
                open_chains.append(chain)
            return

        for edge in candidates:
            if edge.edge in guard:
                # Defensive: validated data has no cycles, but never loop
                # forever on a snapshot.
                continue
            next_steps = steps + [
                _step(edge.install, "INSTALL"),
                *([_step(edge.remove, "REMOVE")] if edge.remove else []),
            ]
            walk(
                edge.parent,
                edge.child_batch,
                next_steps,
                guard | {edge.edge},
            )

    walk(child_part_no, child_batch, [], frozenset())

    return {
        "child_part_no": child_part_no,
        "child_batch": child_batch,
        "products": products,
        "open_chains": open_chains,
    }
