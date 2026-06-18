"""Tests for the pure Kanban event projector (hermes_cli.kanban_projector).

The projector is a pure function: ``(task_id, events) -> ProjectionResult``.
It is version-aware (legacy schema_version 0 events project on kind alone;
schema_version >= 1 critical lifecycle events require a valid transition) and
deterministic (same events -> same state_hash). Malformed v1 critical events
are durably quarantined and skipped without wedging the rest of the replay.
"""

from __future__ import annotations

from hermes_cli import kanban_projector as kp
from hermes_cli.kanban_db import Event


def _ev(seq, kind, *, schema_version=0, transition=None, payload=None, eid=None):
    """Build an Event the way the store would hand it to the projector.

    ``id`` is the monotonic task_events row id; we keep it aligned with seq so
    the co-monotonic replay order is unambiguous in tests.
    """
    return Event(
        id=seq,
        task_id="t_demo",
        kind=kind,
        payload=payload,
        created_at=1000 + seq,
        seq=seq,
        event_id=eid or f"evt_{seq}",
        schema_version=schema_version,
        transition=transition,
    )


# ---------------------------------------------------------------------------
# Legacy projection (schema_version 0)
# ---------------------------------------------------------------------------

def test_legacy_lifecycle_projects_without_transition():
    """schema_version 0 events drive status on kind alone — no transition."""
    events = [
        _ev(1, "created", payload={"status": "ready"}),
        _ev(2, "claimed"),
        _ev(3, "completed"),
    ]
    result = kp.project_events("t_demo", events)
    assert result.state.status == "done"
    assert result.state.applied_count == 3
    assert result.quarantined == []


def test_legacy_unknown_kind_is_noop_not_quarantined():
    """Non-lifecycle kinds (commented/assigned) apply but never quarantine."""
    events = [
        _ev(1, "created", payload={"status": "ready"}),
        _ev(2, "commented"),
        _ev(3, "assigned"),
    ]
    result = kp.project_events("t_demo", events)
    assert result.state.status == "ready"
    assert result.quarantined == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_replay_is_deterministic():
    """Same events -> same state_hash, every time."""
    events = [
        _ev(1, "created", payload={"status": "ready"}),
        _ev(2, "claimed"),
        _ev(3, "blocked"),
        _ev(4, "unblocked"),
        _ev(5, "completed"),
    ]
    a = kp.project_events("t_demo", events)
    b = kp.project_events("t_demo", list(events))
    assert a.state.state_hash == b.state.state_hash
    assert a.state.to_canonical() == b.state.to_canonical()


def test_state_hash_changes_with_state():
    blocked = kp.project_events("t_demo", [_ev(1, "blocked")])
    done = kp.project_events("t_demo", [_ev(1, "completed")])
    assert blocked.state.state_hash != done.state.state_hash


# ---------------------------------------------------------------------------
# v1 critical-transition validation + poison quarantine
# ---------------------------------------------------------------------------

def test_v1_critical_event_with_valid_transition_applies():
    events = [_ev(1, "completed", schema_version=1, transition="done")]
    result = kp.project_events("t_demo", events)
    assert result.state.status == "done"
    assert result.quarantined == []


def test_v1_critical_event_missing_transition_is_quarantined_and_skipped():
    events = [
        _ev(1, "created", payload={"status": "ready"}),
        _ev(2, "completed", schema_version=1, transition=None),  # malformed
        _ev(3, "commented", schema_version=1),                   # still processed
    ]
    result = kp.project_events("t_demo", events)
    # The poison event is skipped: status stays at the pre-poison value.
    assert result.state.status == "ready"
    assert len(result.quarantined) == 1
    poison = result.quarantined[0]
    assert poison.seq == 2
    assert poison.kind == "completed"
    assert poison.reason == "missing_transition"
    # The replay did not wedge — the later non-critical event still applied.
    assert result.state.applied_count == 2


def test_v1_critical_event_invalid_target_is_quarantined():
    events = [_ev(1, "completed", schema_version=1, transition="not_a_state")]
    result = kp.project_events("t_demo", events)
    assert result.state.status is None
    assert len(result.quarantined) == 1
    assert result.quarantined[0].reason == "invalid_transition_target"


def test_poison_decision_is_deterministic():
    """Identical malformed events yield identical quarantine decisions."""
    events = [_ev(7, "blocked", schema_version=1, transition=None)]
    a = kp.project_events("t_demo", events)
    b = kp.project_events("t_demo", list(events))
    assert [
        (q.seq, q.kind, q.reason, q.event_id) for q in a.quarantined
    ] == [
        (q.seq, q.kind, q.reason, q.event_id) for q in b.quarantined
    ]


def test_poison_is_isolated_to_its_own_task():
    """One task's poison event does not affect another task's projection."""
    poisoned = kp.project_events(
        "t_a", [_ev(1, "completed", schema_version=1, transition=None)]
    )
    healthy = kp.project_events(
        "t_b", [_ev(1, "completed", schema_version=1, transition="done")]
    )
    assert poisoned.state.status is None
    assert len(poisoned.quarantined) == 1
    assert healthy.state.status == "done"
    assert healthy.quarantined == []
