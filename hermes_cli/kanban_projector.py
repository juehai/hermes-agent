"""Pure, version-aware projector for the Hermes Kanban Protocol v1 event store.

This module is deliberately free of any database or I/O dependency: it turns an
ordered list of :class:`hermes_cli.kanban_db.Event` rows into a deterministic
projected state plus a list of quarantine decisions for malformed events. The
storage-side rebuild harness (``kanban_db.rebuild_task_projection``) reads
events, calls :func:`project_events`, and persists the result; keeping the
projection logic here means it can be unit-tested and replayed without a DB.

Version awareness:

* ``schema_version`` 0 (or ``None``) — legacy events. They predate the
  protocol and never carry a ``transition``; the projector maps known
  lifecycle *kinds* straight to a status. This is what every event written by
  the existing ``_append_event`` call sites looks like.
* ``schema_version`` >= 1 — protocol events. A *critical lifecycle* event
  (one that drives a status change) MUST carry a valid ``transition`` naming a
  known lifecycle state. If it does not, the event is a poison event: the
  projector records a deterministic quarantine decision and skips it, so one
  malformed transition cannot wedge the rest of a task's replay (and, because
  projection is per-task, cannot affect any other task).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

# Legacy events carry schema_version 0 (or None on un-backfilled rows);
# protocol events are >= 1.
SCHEMA_VERSION_LEGACY = 0
PROTOCOL_SCHEMA_VERSION = 1

# Lifecycle states a transition may legally target. Mirrors
# ``kanban_db.VALID_STATUSES``; duplicated here as a frozenset so the projector
# stays import-free of the DB layer (kanban_db imports the projector, not the
# other way around).
LIFECYCLE_STATES = frozenset(
    {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"}
)

# Critical lifecycle kinds: events that drive a status transition. Under
# schema_version >= 1 these MUST carry a valid ``transition``; under legacy
# schema_version 0 they are projected on the kind -> status map below.
CRITICAL_KINDS = frozenset(
    {
        "promoted",
        "scheduled",
        "claimed",
        "spawned",
        "started",
        "blocked",
        "unblocked",
        "completed",
        "archived",
        "reopened",
    }
)

# Legacy kind -> resulting status. ``created`` is special-cased (it carries the
# initial status in its payload). Kinds absent from this map (commented,
# assigned, edited, linked, heartbeat, ...) are non-lifecycle: they count as
# applied but do not change the projected status and are never quarantined.
_LEGACY_KIND_STATUS = {
    "promoted": "ready",
    "scheduled": "scheduled",
    "claimed": "running",
    "spawned": "running",
    "started": "running",
    "blocked": "blocked",
    "unblocked": "ready",
    "completed": "done",
    "archived": "archived",
    "reopened": "ready",
}

# Deterministic quarantine reason codes.
REASON_MISSING_TRANSITION = "missing_transition"
REASON_INVALID_TARGET = "invalid_transition_target"


@dataclass
class QuarantineDecision:
    """One poison event the projector refused to apply.

    ``event_row_id`` is the ``task_events.id`` (the durable key the rebuild
    harness uses for idempotent quarantine persistence). ``event_id`` is the
    protocol event id when present.
    """

    event_row_id: Optional[int]
    event_id: Optional[str]
    seq: Optional[int]
    kind: str
    reason: str


@dataclass
class ProjectedState:
    """The deterministic projection of a task's event stream.

    The projection is intentionally small in this slice: the lifecycle
    ``status`` plus bookkeeping counters. ``to_canonical`` / ``state_hash`` give
    a stable, order-independent-of-dict-insertion serialization so that a live
    projection and a rebuilt-from-scratch projection of the same events hash
    identically.
    """

    task_id: str
    status: Optional[str] = None
    last_seq: Optional[int] = None
    last_event_id: Optional[int] = None
    applied_count: int = 0
    quarantined_count: int = 0

    def to_canonical(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "last_seq": self.last_seq,
            "applied_count": self.applied_count,
            "quarantined_count": self.quarantined_count,
        }

    def state_json(self) -> str:
        return json.dumps(
            self.to_canonical(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    @property
    def state_hash(self) -> str:
        return hashlib.sha256(self.state_json().encode("utf-8")).hexdigest()


@dataclass
class ProjectionResult:
    state: ProjectedState
    quarantined: list[QuarantineDecision] = field(default_factory=list)


def _resolve_transition_target(event) -> tuple[Optional[str], Optional[str]]:
    """Return ``(to_state, error_reason)`` for a critical protocol event.

    The target state may be carried either by ``event.transition`` (a bare
    state name, or a ``"from->to"`` form) or by ``payload["to_state"]``. Exactly
    one of the two return slots is non-None: a resolved valid state, or a
    deterministic error-reason code.
    """
    raw: Optional[str] = None
    transition = getattr(event, "transition", None)
    if isinstance(transition, str) and transition.strip():
        raw = transition.strip()
        if "->" in raw:
            raw = raw.split("->")[-1].strip()
    if raw is None:
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict):
            to_state = payload.get("to_state")
            if isinstance(to_state, str) and to_state.strip():
                raw = to_state.strip()
    if not raw:
        return None, REASON_MISSING_TRANSITION
    if raw not in LIFECYCLE_STATES:
        return None, REASON_INVALID_TARGET
    return raw, None


def project_events(task_id: str, events) -> ProjectionResult:
    """Project an ordered event stream into a deterministic state.

    ``events`` must already be in replay order (the store reads them ordered by
    the monotonic ``task_events.id``, which is co-monotonic with ``seq``).
    """
    state = ProjectedState(task_id=task_id)
    quarantined: list[QuarantineDecision] = []

    for ev in events:
        schema_version = ev.schema_version
        if schema_version is None:
            schema_version = SCHEMA_VERSION_LEGACY
        kind = ev.kind

        if schema_version >= PROTOCOL_SCHEMA_VERSION and kind in CRITICAL_KINDS:
            to_state, reason = _resolve_transition_target(ev)
            if reason is not None:
                # Poison event: record a durable, deterministic decision and
                # skip it. Do not advance status; do not wedge the replay.
                quarantined.append(
                    QuarantineDecision(
                        event_row_id=ev.id,
                        event_id=ev.event_id,
                        seq=ev.seq,
                        kind=kind,
                        reason=reason,
                    )
                )
                state.quarantined_count += 1
                continue
            state.status = to_state
        elif kind == "created":
            payload = ev.payload if isinstance(ev.payload, dict) else None
            initial = payload.get("status") if payload else None
            if isinstance(initial, str) and initial in LIFECYCLE_STATES:
                state.status = initial
        elif kind in _LEGACY_KIND_STATUS:
            state.status = _LEGACY_KIND_STATUS[kind]
        # else: non-lifecycle kind — applied, but no status change.

        state.applied_count += 1
        if ev.seq is not None:
            state.last_seq = ev.seq
        if ev.id is not None:
            state.last_event_id = ev.id

    return ProjectionResult(state=state, quarantined=quarantined)
