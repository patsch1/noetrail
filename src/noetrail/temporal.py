#!/usr/bin/env python3
"""Valid time on typed relations: intervals, supersession, point-in-time.

Two clocks are kept apart on purpose.

*Valid time* says when an assertion held in the world. It lives on the
individual relation as ``valid_from`` and ``valid_until``, and it is what a
point-in-time query asks about.

*Record time* says when the vault learned something. The entry envelope
already carries it: ``created_at`` is when the vault first held this entry and
``updated_at`` when it last changed. Those two are honest transaction-time
endpoints, but they are endpoints and not a history -- an entry does not keep
what it looked like at an earlier ``updated_at``. A relation that carries an
interval therefore also carries ``recorded_at``, the instant that particular
assertion entered the vault, so "when did this stop being true" and "when did
we find out" stay answerable separately.

Every comparison here runs over instants, never over the stored strings.
``now_iso`` writes local time with an offset, so ``2026-03-01T00:30:00+02:00``
sorts *after* ``2026-02-28T23:30:00+00:00`` lexicographically while naming the
earlier instant. Read paths are deliberately lenient about a value they cannot
parse -- an unparseable bound is treated as absent rather than aborting a
whole-vault read; ``validate`` is what reports it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from noetrail.deadline import check_deadline
from noetrail.errors import Conflict, InvalidRequest, NotFound
from noetrail.frontmatter import metadata_list
from noetrail.normalize import now_iso

RELATION_CORE_FIELDS = frozenset({"predicate", "target"})

RELATION_TEMPORAL_FIELDS: tuple[str, ...] = (
    "valid_from",
    "valid_until",
    "superseded_by",
    "recorded_at",
)
"""Keys a relation may carry in addition to the two it always has.

They are optional, and the overwhelming majority of relations carry none of
them: an entry that states no interval must not grow one, because an invented
interval is a claim about the world that nobody made.
"""

RELATION_INSTANT_FIELDS: tuple[str, ...] = (
    "valid_from",
    "valid_until",
    "recorded_at",
)

RELATION_FIELDS = RELATION_CORE_FIELDS | set(RELATION_TEMPORAL_FIELDS)

Edge = tuple[str, str]


def instant(value: object) -> float | None:
    """Return an ISO-8601 timestamp as a comparable instant, or None.

    None covers all three ways a bound can be unusable: it is not a string, it
    is not ISO 8601, or it has no offset. A naive timestamp is refused rather
    than assumed to be local, because that assumption is exactly what turns a
    correct interval into a wrong one when a vault travels.
    """

    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def require_instant(value: object, field: str) -> float:
    """Parse a caller-supplied instant, or refuse the request.

    ``InvalidRequest`` rather than ``ValidationError``: every caller of this
    passes a value from a CLI option or an MCP argument, so the fault is in the
    request and not in what the vault stores. Stored bounds go through
    ``instant`` and are reported by ``validate``.
    """

    parsed = instant(value)
    if parsed is None:
        raise InvalidRequest(
            f"{field} must be an ISO 8601 timestamp with a timezone"
        )
    return parsed


def resolve_as_of(value: str | None) -> tuple[float, str | None]:
    """Return the instant a query runs at, plus the label to report back.

    An absent ``--as-of`` is "now", which is what makes the current state the
    default answer without a second code path: every read filters against one
    instant either way.

    The label is ``None`` in that case, and callers leave ``as_of`` out of
    their result entirely. Echoing the current clock would put a value in the
    answer that differs between two runs of the identical query, which is what
    the index-versus-scan equivalence checks compare.
    """

    if value is None:
        return datetime.now().astimezone().timestamp(), None
    return require_instant(value, "as_of"), value


def is_temporal(relation: object) -> bool:
    """Report whether a relation states anything about time at all."""

    return isinstance(relation, dict) and any(
        field in relation for field in RELATION_TEMPORAL_FIELDS
    )


def relation_holds_at(relation: object, at: float) -> bool:
    """Whether one relation asserts something at the given instant.

    A relation without bounds holds at every instant. That is what keeps every
    entry written before this existed answering exactly as it did: absence of
    an interval means "not recorded", not "not valid".

    The interval is half-open, ``[valid_from, valid_until)``, so a replacement
    that takes effect at T and the statement it replaces never both hold at T.
    """

    if not isinstance(relation, dict):
        return False
    start = instant(relation.get("valid_from"))
    if start is not None and at < start:
        return False
    end = instant(relation.get("valid_until"))
    return not (end is not None and at >= end)


def relations_at(
    metadata: dict[str, object], at: float
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Split one entry's relations into those in force at `at` and the rest.

    The second list is the point of the whole mechanism: a superseded
    assertion stays in the file and stays readable, it just stops being
    returned as an equal-ranking answer.
    """

    current: list[dict[str, object]] = []
    superseded: list[dict[str, object]] = []
    for relation in metadata_list(metadata, "relations"):
        check_deadline()
        if not isinstance(relation, dict):
            continue
        (current if relation_holds_at(relation, at) else superseded).append(
            relation
        )
    return current, superseded


def temporal_fields(relation: dict[str, object]) -> dict[str, object]:
    """The interval keys a relation actually carries, in a fixed order.

    Only the keys that are present: a relation with no interval must not be
    reported with four null fields, or every reader would have to distinguish
    "unbounded" from "not recorded" by itself.
    """

    return {
        field: relation[field]
        for field in RELATION_TEMPORAL_FIELDS
        if field in relation
    }


def find_edge(
    relations: Iterable[object], predicate: str, target: str
) -> dict[str, object] | None:
    """Locate one relation by its identity, ignoring any interval on it.

    ``(predicate, target)`` is the identity of an assertion inside one entry;
    a dictionary comparison against ``{"predicate": ..., "target": ...}`` stops
    finding a relation the moment that relation carries an interval.
    """

    for relation in relations:
        check_deadline()
        if (
            isinstance(relation, dict)
            and relation.get("predicate") == predicate
            and relation.get("target") == target
        ):
            return relation
    return None


def _successor_map(
    relations: list[object],
) -> tuple[dict[Edge, dict[str, object]], dict[Edge, Edge]]:
    edges: dict[Edge, dict[str, object]] = {}
    for relation in relations:
        check_deadline()
        if not isinstance(relation, dict):
            continue
        predicate = relation.get("predicate")
        target = relation.get("target")
        if isinstance(predicate, str) and isinstance(target, str):
            edges.setdefault((predicate, target), relation)
    successors: dict[Edge, Edge] = {}
    for (predicate, target), relation in edges.items():
        check_deadline()
        successor = relation.get("superseded_by")
        if isinstance(successor, str) and (predicate, successor) in edges:
            successors[(predicate, target)] = (predicate, successor)
    return edges, successors


def _cycle_errors(successors: dict[Edge, Edge]) -> list[str]:
    """Report each supersession loop once.

    Each assertion has at most one successor, so a loop is reachable by
    walking forward. Reporting per node would restate the same loop as many
    times as it has members.
    """

    errors: list[str] = []
    reported: set[Edge] = set()
    for origin in successors:
        check_deadline()
        if origin in reported:
            continue
        path: list[Edge] = []
        node: Edge | None = origin
        while node is not None and node not in path:
            check_deadline()
            path.append(node)
            node = successors.get(node)
        if node is None:
            continue
        cycle = path[path.index(node) :]
        if not set(cycle) & reported:
            listed = " -> ".join(
                f"{predicate}:{target}" for predicate, target in cycle
            )
            errors.append(f"supersession chain contains a cycle: {listed}")
        reported.update(cycle)
    return errors


def supersession_errors(relations: object) -> list[str]:
    """Report the contradictions a supersession chain can carry.

    Three of them are why this exists: an assertion that still runs while its
    replacement already does, a chain that loops back on itself, and a
    replacement dated before the statement it replaces even began. The rest are
    the structural preconditions those three checks need in order to mean
    anything.
    """

    if not isinstance(relations, list):
        return []
    edges, successors = _successor_map(relations)
    errors: list[str] = []
    for index, relation in enumerate(relations):
        check_deadline()
        if not isinstance(relation, dict) or "superseded_by" not in relation:
            continue
        successor_target = relation.get("superseded_by")
        predicate = relation.get("predicate")
        target = relation.get("target")
        if not isinstance(successor_target, str):
            errors.append(
                f"relation {index} superseded_by must be an entry ID"
            )
            continue
        if not isinstance(predicate, str) or not isinstance(target, str):
            continue
        if successor_target == target:
            errors.append(f"relation {index} supersedes itself")
            continue
        successor = edges.get((predicate, successor_target))
        if successor is None:
            # A replacement has to be an assertion the same entry makes, with
            # the same predicate. Otherwise "what replaced this" points into
            # nothing and the chain cannot be walked or checked.
            errors.append(
                f"relation {index} names a successor the entry does not "
                f"assert: {predicate}:{successor_target}"
            )
            continue
        end = instant(relation.get("valid_until"))
        if end is None:
            errors.append(
                f"relation {index} is superseded but records no valid_until"
            )
        successor_start = instant(successor.get("valid_from"))
        if successor_start is None:
            errors.append(
                f"relation {index} is superseded by a relation without "
                f"valid_from"
            )
            continue
        start = instant(relation.get("valid_from"))
        if start is not None and successor_start < start:
            errors.append(
                f"relation {index} is superseded from before it began"
            )
        if end is not None and successor_start < end:
            errors.append(
                f"relation {index} overlaps the successor that replaces it"
            )
    errors.extend(_cycle_errors(successors))
    return errors


def interval_errors(relation: dict[str, object], index: int) -> list[str]:
    """Check one relation's own interval, independent of any chain."""

    errors: list[str] = []
    start = instant(relation.get("valid_from"))
    end = instant(relation.get("valid_until"))
    if start is not None and end is not None and end <= start:
        errors.append(
            f"relation {index} valid_until must be later than valid_from"
        )
    if "valid_until" in relation and "valid_from" not in relation:
        # An end without a beginning cannot be superseded, cannot be checked
        # for overlap, and reads as "always true until then", which is a claim
        # nobody made.
        errors.append(f"relation {index} has valid_until without valid_from")
    return errors


def check_validity_arguments(
    *,
    valid_from: str | None,
    valid_until: str | None,
    supersedes: str | None,
    adding: bool,
) -> None:
    """Reject validity arguments that cannot mean anything where they stand."""

    supplied = [
        name
        for name, value in (
            ("--valid-from", valid_from),
            ("--valid-until", valid_until),
            ("--supersedes", supersedes),
        )
        if value
    ]
    if supplied and not adding:
        raise InvalidRequest(
            f"{', '.join(supplied)} only apply when adding a typed relation"
        )
    if supersedes and not valid_from:
        # Without it there is no instant at which the replacement takes over,
        # so neither interval can be closed and no overlap check is possible.
        raise InvalidRequest(
            "--supersedes requires --valid-from, the instant from which the "
            "replacement holds"
        )


def apply_relation_validity(
    relations: list[object],
    relation: dict[str, object],
    *,
    valid_from: str | None,
    valid_until: str | None,
    supersedes: str | None,
) -> tuple[list[object], dict[str, object]]:
    """Attach an interval to a new relation and close the one it replaces.

    Both halves of a supersession are written in one step and one file write,
    so a vault can never hold the state where the replacement is already
    asserted and the statement it replaced still runs unbounded.
    """

    if not (valid_from or valid_until or supersedes):
        return relations, relation

    predicate = str(relation["predicate"])
    target = str(relation["target"])
    start: float | None = None
    if valid_from is not None:
        start = require_instant(valid_from, "valid_from")
        relation["valid_from"] = valid_from
    if valid_until is not None:
        end = require_instant(valid_until, "valid_until")
        if start is not None and end <= start:
            raise InvalidRequest("--valid-until must be later than --valid-from")
        if start is None:
            raise InvalidRequest("--valid-until requires --valid-from")
        relation["valid_until"] = valid_until
    # Record time of this assertion, kept apart from the valid time above: the
    # entry's `updated_at` says when the file last changed, not when this
    # particular statement entered the vault.
    relation["recorded_at"] = now_iso()

    if supersedes is None:
        return relations, relation
    if supersedes == target:
        raise InvalidRequest("A relation cannot supersede itself")
    assert valid_from is not None and start is not None
    predecessor = find_edge(relations, predicate, supersedes)
    if predecessor is None:
        raise NotFound(
            f"The entry asserts no {predicate} relation to supersede: "
            f"{supersedes}"
        )
    if "superseded_by" in predecessor:
        raise Conflict(
            "That relation has already been superseded; supersede its "
            "replacement instead"
        )
    began = instant(predecessor.get("valid_from"))
    if began is not None and start < began:
        raise InvalidRequest(
            "--valid-from precedes the start of the relation it supersedes"
        )
    ended = instant(predecessor.get("valid_until"))
    if ended is not None and ended > start:
        raise Conflict(
            "The relation being superseded already runs past the instant the "
            "replacement takes effect"
        )
    updated: list[object] = [
        {**item, "valid_until": valid_from, "superseded_by": target}
        if item is predecessor
        else item
        for item in relations
    ]
    return updated, relation


def temporal_counts(entries: Iterable[dict[str, object]], at: float) -> dict[str, int]:
    """Aggregate relation counts for one instant, for inventory and doctor."""

    total = 0
    in_force = 0
    temporal = 0
    not_in_force = 0
    for metadata in entries:
        check_deadline()
        for relation in metadata_list(metadata, "relations"):
            check_deadline()
            if not isinstance(relation, dict):
                continue
            total += 1
            if is_temporal(relation):
                temporal += 1
            if relation_holds_at(relation, at):
                in_force += 1
            else:
                not_in_force += 1
    return {
        "relation_count": in_force,
        "recorded_relation_count": total,
        "temporal_relation_count": temporal,
        "not_in_force_relation_count": not_in_force,
    }
