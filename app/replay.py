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


@dataclass
class _AttributionPath:
    """One complete, log-compatible attribution of the queried batch.

    ``edges`` are the active edges walked from the queried board upwards.  The
    last edge's ``child_batch`` is the batch of the assembly named in
    ``terminal_part`` (the batch that was installed into the parent above it);
    it is what the existing ``root_batch`` field reports.
    """

    terminal_part: str
    terminal_batch: str
    edges: list[ActiveEdge]

    @property
    def is_product(self) -> bool:
        return self.terminal_part.startswith(PRODUCT_PREFIX)

    def steps(self) -> list[dict]:
        steps: list[dict] = []
        for edge in self.edges:
            steps.append(_step(edge.install, "INSTALL"))
            if edge.remove is not None:
                steps.append(_step(edge.remove, "REMOVE"))
        return steps


def _group_by_product(paths: list[_AttributionPath]) -> list[dict]:
    """Collapse paths by terminal part.

    Every compatible path is preserved in ``compatible_paths`` so the
    attribution can be audited.  ``path`` repeats the first path for
    backwards compatibility, and ``root_batch`` is that path's parent-assembly
    batch; when several batches converge, ``root_batches`` lists them all.
    """
    grouped: dict[str, list[_AttributionPath]] = {}
    for path in paths:
        grouped.setdefault(path.terminal_part, []).append(path)

    result: list[dict] = []
    for terminal, group in grouped.items():
        batches: list[str] = []
        for path in group:
            if path.terminal_batch not in batches:
                batches.append(path.terminal_batch)
        paths_out = [path.steps() for path in group]
        result.append(
            {
                "root_part_no": terminal,
                "root_batch": group[0].terminal_batch,
                "root_batches": batches,
                "is_finished_product": group[0].is_product,
                "path": paths_out[0],
                "compatible_paths": paths_out,
            }
        )
    return result


def _enumerate_attributions(
    edges: list[ActiveEdge],
    child_part_no: str,
    child_batch: str,
) -> list[_AttributionPath]:
    """Enumerate every attribution path compatible with the active log.

    The starting point is exact: only edges whose *child* is the queried
    ``(part, batch)`` are considered.  A batch that is not mounted at the
    query moment therefore yields no paths at all (it was never installed, or
    it has already been removed).

    Above the first level the log does not record the batch of the parent
    assembly: an INSTALL event only names ``parent_part_no``.  Consequently
    each active instance of that part number — that is, every active edge whose
    ``child_part`` equals it, one per batch — is a possible attribution, and
    all of them are followed.  If every one of those paths ends at the same
    finished product, the product is still certain; otherwise the result is
    ambiguous and has to be verified manually.
    """
    by_child_part: dict[str, list[ActiveEdge]] = {}
    for e in edges:
        by_child_part.setdefault(e.child_part, []).append(e)

    roots = [
        e
        for e in edges
        if e.child_part == child_part_no and e.child_batch == child_batch
    ]

    paths: list[_AttributionPath] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()

    def record(terminal_part: str, steps: list[ActiveEdge]) -> None:
        terminal_batch = steps[-1].child_batch if steps else child_batch
        signature = (terminal_part, tuple(e.install.event_no for e in steps))
        if signature in seen:
            return
        seen.add(signature)
        paths.append(
            _AttributionPath(terminal_part, terminal_batch, list(steps))
        )

    def visit(part: str, steps: list[ActiveEdge], guard: frozenset) -> None:
        if part.startswith(PRODUCT_PREFIX):
            # A finished product is the top of the chain even if it also
            # appears as a child somewhere else in the log.
            record(part, steps)
            return

        candidates = [
            e for e in by_child_part.get(part, []) if e.edge not in guard
        ]
        if not candidates:
            # In-progress assembly: its own parent is not known yet.
            record(part, steps)
            return

        for edge in candidates:
            visit(
                edge.parent,
                steps + [edge],
                guard | {edge.edge},
            )

    for root in roots:
        visit(root.parent, [root], frozenset({root.edge}))

    return paths


def trace_batch(
    events: list[Event],
    child_part_no: str,
    child_batch: str,
    at_us: int,
) -> dict:
    """Trace the queried child batch at ``at_us``.

    The result separates three kinds of chains:

    * ``products`` (确定受影响): every log-compatible attribution path ends at
      the *same* finished product;
    * ``pending_products`` (待核实): at least one finished product is reached,
      but the attribution is ambiguous — different plausible parent-assembly
      batches lead to different products, or one branch is still in progress;
    * ``open_chains``: paths whose top assembly is not (yet) a ``P-`` serial.

    ``batch_found`` is false when the queried batch has no active INSTALL at
    the query moment; in that case all lists are empty.
    """
    edges = active_edges_at(events, at_us)
    paths = _enumerate_attributions(edges, child_part_no, child_batch)

    product_paths = [p for p in paths if p.is_product]
    open_paths = [p for p in paths if not p.is_product]
    distinct_products = {p.terminal_part for p in product_paths}
    distinct_terminals = {
        (p.terminal_part, p.terminal_batch) for p in paths
    }

    # Certainty rule: the physical board exists exactly once, so a product is
    # definitely affected only when *every* compatible attribution ends at it.
    # Different parent-assembly batches that reconverge on the same product do
    # not create uncertainty.
    confirmed = (
        len(distinct_products) == 1
        and not open_paths
        and bool(product_paths)
    )

    if confirmed:
        products = _group_by_product(product_paths)
        pending_products: list[dict] = []
    else:
        products = []
        pending_products = _group_by_product(product_paths)

    open_chains = _group_by_product(open_paths)

    # Ambiguous means the log admits more than one possible top chain. A lone
    # in-progress chain is not ambiguous — it simply has no product yet.
    ambiguous = not confirmed and len(distinct_terminals) > 1

    return {
        "child_part_no": child_part_no,
        "child_batch": child_batch,
        "batch_found": bool(paths),
        "products": products,
        "pending_products": pending_products,
        "open_chains": open_chains,
        "total_affected_products": len(distinct_products) if confirmed else 0,
        "total_pending_products": (
            0 if confirmed else len(distinct_products)
        ),
        "ambiguous": ambiguous,
        "compatible_path_count": len(paths),
    }
