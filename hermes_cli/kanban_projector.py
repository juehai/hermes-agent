"""Kanban Protocol v1 (slice 3): deterministic, pure event projector.

This module is intentionally free of any database dependency. It turns an
ordered stream of ``task_events`` (the :class:`hermes_cli.kanban_db.Event`
dataclass, or any object exposing the same attributes) into a deterministic
:class:`ProjectionResult`:

* ``state`` / ``state_json`` / ``state_hash`` — the canonical shadow read-model
  for one task. ``state_json`` is canonical JSON (sorted keys, stable
  separators) so a live projection and a rebuilt-from-disk projection are
  byte-for-byte comparable and the hash is stable.
* ``up_to_seq`` — the highest protocol ``seq`` the projection has consumed
  (poison events still advance the cursor; they are isolated, not replayed).
* ``poison_count`` / ``quarantined`` — the stable, deterministic quarantine
  decisions for malformed-critical or unknown-future-schema events.

The projector never mutates anything and never raises on malformed input: a
bad event becomes a quarantine *decision*, it does not crash the replay. The
persistence harness in :mod:`hermes_cli.kanban_db` calls this function and
durably materializes the result.

Schema-version policy (deliberate, conservative):

* ``schema_version`` 0 or ``None`` → **legacy**. Projected through a
  compatibility adapter that reads the target status from ``transition.to`` or
  the legacy ``payload.to``. Legacy events are *never* quarantined merely for
  lacking v1 transition fields.
* ``schema_version`` 1 → **v1**. A critical lifecycle transition must carry a
  structurally valid ``transition`` (a dict whose ``to`` is a known status);
  otherwise it is quarantined as ``malformed_v1_transition``.
* ``schema_version`` >= 2 (unknown / future) → critical events are quarantined
  as ``unknown_schema_version`` rather than applied or crashed; non-critical
  events are skipped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

# Bumping this invalidates persisted projections/quarantine decisions and is a
# deliberate, separate change. It is part of every quarantine ``decision_key``
# and the durable quarantine UNIQUE key, so a bump re-quarantines cleanly
# instead of colliding with prior rows.
PROJECTOR_VERSION = 1

# The protocol schema version this projector materializes state for. Stored on
# the projection row; distinct from a *event's* schema_version.
PROJECTION_SCHEMA_VERSION = 1

# Event ``schema_version`` values this projector understands. Anything outside
# this set is treated as unknown/future and handled conservatively.
KNOWN_SCHEMA_VERSIONS = frozenset({0, 1})

# Kinds that change a task's lifecycle state. Only critical events are
# quarantined when malformed or carrying an unknown schema; non-critical events
# (comments, etc.) are simply ignored by the shadow state model.
CRITICAL_KINDS = frozenset({"transitioned"})

# Stable quarantine reason codes (also stored in the durable quarantine row).
REASON_MALFORMED_TRANSITION = "malformed_v1_transition"
REASON_UNKNOWN_SCHEMA = "unknown_schema_version"


@dataclass(frozen=True)
class QuarantineDecision:
    """A single, stable decision to isolate a poison event.

    ``decision_key`` is deterministic for a given (projector_version, reason,
    event identity) triple so repeated rebuilds produce the same key and the
    durable quarantine write is idempotent.
    """

    task_id: str
    event_row_id: int
    event_id: Optional[str]
    seq: Optional[int]
    kind: str
    schema_version: Optional[int]
    reason: str
    decision_key: str
    payload: Optional[dict]


@dataclass
class ProjectionResult:
    """Deterministic output of :func:`project_events` for one task."""

    task_id: str
    state: dict
    state_json: str
    state_hash: str
    up_to_seq: int
    poison_count: int
    projector_version: int
    quarantined: list = field(default_factory=list)


def _canonical_json(obj: Any) -> str:
    """Canonical JSON: sorted keys, stable separators, UTF-8 preserved."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _state_hash(state_json: str) -> str:
    """Stable content hash of the canonical state JSON."""
    return "sha256:" + hashlib.sha256(state_json.encode("utf-8")).hexdigest()


def _decision_key(projector_version: int, reason: str, event: Any) -> str:
    """Build a stable decision key from the event's most durable identity.

    Prefers the protocol ``event_id`` (assigned once, never reused); falls back
    to the ``task_events.id`` row id for legacy rows that predate event_ids.
    """
    event_id = getattr(event, "event_id", None)
    ident = event_id if event_id else f"row:{getattr(event, 'id', None)}"
    return f"v{projector_version}:{reason}:{ident}"


def _effective_schema_version(event: Any) -> int:
    """Treat a missing schema_version as legacy (0).

    Compatibility ``_append_event`` rows carry NULL ``schema_version``; the
    slice-1 migration backfills legacy history to 0. Either way, ``None`` maps
    to the legacy adapter rather than being flagged unknown.
    """
    sv = getattr(event, "schema_version", None)
    return 0 if sv is None else int(sv)


def _derive_target_status(event: Any, valid_statuses: frozenset) -> Optional[str]:
    """Pull a valid target status from ``transition.to`` then ``payload.to``.

    Returns ``None`` when neither yields a recognized status.
    """
    transition = getattr(event, "transition", None)
    if isinstance(transition, dict):
        to = transition.get("to")
        if to in valid_statuses:
            return to
    payload = getattr(event, "payload", None)
    if isinstance(payload, dict):
        to = payload.get("to")
        if to in valid_statuses:
            return to
    return None


def project_events(
    task_id: str,
    events: Sequence[Any],
    *,
    projector_version: int = PROJECTOR_VERSION,
    valid_statuses: Optional[frozenset] = None,
) -> ProjectionResult:
    """Project an ordered event stream into a deterministic shadow state.

    ``events`` must already be ordered for replay (protocol ``seq`` then row
    ``id``); the caller supplies them in that order. The function is pure: it
    reads only event attributes and returns a fresh result. Poison events are
    isolated into ``quarantined`` and advance ``up_to_seq`` but never mutate
    the state. Determinism: the same event sequence always yields identical
    ``state_json`` / ``state_hash`` / ``up_to_seq`` and identical decision keys.
    """
    if valid_statuses is None:
        # Lazy import keeps this module DB-free at import time while staying the
        # single source of truth for the status vocabulary.
        from hermes_cli.kanban_db import VALID_STATUSES

        valid_statuses = frozenset(VALID_STATUSES)

    status: Optional[str] = None
    transitions_applied = 0
    up_to_seq = 0
    quarantined: list = []

    for event in events:
        seq = getattr(event, "seq", None)
        if seq is not None:
            up_to_seq = max(up_to_seq, int(seq))

        kind = getattr(event, "kind", None)
        is_critical = kind in CRITICAL_KINDS
        sv = _effective_schema_version(event)

        # Unknown / future schema. Only critical events are quarantined; other
        # kinds are skipped (the shadow model simply doesn't understand them).
        if sv not in KNOWN_SCHEMA_VERSIONS:
            if is_critical:
                quarantined.append(
                    _make_decision(task_id, event, REASON_UNKNOWN_SCHEMA, projector_version)
                )
            continue

        if not is_critical:
            # Non-critical events (created/commented/...) don't move shadow state.
            continue

        target = _derive_target_status(event, valid_statuses)

        if sv == 1:
            # v1 critical transition must be structurally well-formed.
            transition = getattr(event, "transition", None)
            well_formed = isinstance(transition, dict) and transition.get("to") in valid_statuses
            if not well_formed:
                quarantined.append(
                    _make_decision(
                        task_id, event, REASON_MALFORMED_TRANSITION, projector_version
                    )
                )
                continue
            status = target
            transitions_applied += 1
        else:
            # Legacy (schema 0 / None): apply via the compat adapter. Missing v1
            # transition fields are tolerated, never quarantined; an event with
            # no derivable target is a no-op (state unchanged) but still counts
            # as consumed.
            if target is not None:
                status = target
                transitions_applied += 1

    state = {"status": status, "transitions_applied": transitions_applied}
    state_json = _canonical_json(state)
    return ProjectionResult(
        task_id=task_id,
        state=state,
        state_json=state_json,
        state_hash=_state_hash(state_json),
        up_to_seq=up_to_seq,
        poison_count=len(quarantined),
        projector_version=projector_version,
        quarantined=quarantined,
    )


def _make_decision(
    task_id: str, event: Any, reason: str, projector_version: int
) -> QuarantineDecision:
    return QuarantineDecision(
        task_id=task_id,
        event_row_id=int(getattr(event, "id")),
        event_id=getattr(event, "event_id", None),
        seq=getattr(event, "seq", None),
        kind=getattr(event, "kind", None),
        schema_version=getattr(event, "schema_version", None),
        reason=reason,
        decision_key=_decision_key(projector_version, reason, event),
        payload=getattr(event, "payload", None),
    )
