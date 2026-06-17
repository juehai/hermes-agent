"""Tests for the Kanban Protocol v1 (slice 2) structured ``append_event`` API.

Covers the three protocol guarantees layered on the slice 1 schema foundation:

* per-task ``seq`` allocation with stable protocol fields,
* ``expected_seq`` compare-and-swap (one writer wins a contended head), and
* ``(task_id, message_id)`` idempotency — duplicate-same returns the existing
  event, duplicate-different raises a typed domain error.

The legacy ``_append_event`` / ``list_events`` path must keep working unchanged.
"""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _protocol_seqs(task_id):
    with kb.connect() as conn:
        return [e.seq for e in kb.list_events(conn, task_id) if e.seq is not None]


# ---------------------------------------------------------------------------
# 1. successful append assigns next per-task seq and stable protocol fields
# ---------------------------------------------------------------------------

def test_protocol_append_assigns_next_seq_and_stable_fields(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")

    with kb.connect() as conn:
        res = kb.append_event(
            conn,
            t,
            "transitioned",
            payload={"to": "running"},
            expected_seq=0,
            message_id="m1",
            actor="alice",
            source="cli",
            transition={"from": "ready", "to": "running"},
        )

    assert res.inserted is True
    assert res.current_seq == 1

    ev = res.event
    assert ev.seq == 1
    assert ev.kind == "transitioned"
    assert ev.payload == {"to": "running"}
    assert ev.message_id == "m1"
    assert ev.actor == "alice"
    assert ev.source == "cli"
    assert ev.transition == {"from": "ready", "to": "running"}
    assert ev.schema_version == 1
    assert ev.protocol == "hermes-kanban/1"
    assert ev.event_id  # server-generated when not supplied
    assert ev.id > 0  # global autoincrement id (notifier/dashboard cursor) preserved

    # A second protocol append advances the per-task seq to 2.
    with kb.connect() as conn:
        res2 = kb.append_event(
            conn, t, "transitioned", payload={"to": "done"},
            expected_seq=res.current_seq, message_id="m2",
        )
    assert res2.event.seq == 2
    assert res2.current_seq == 2
    assert res2.event.id > ev.id  # global id strictly increases


# ---------------------------------------------------------------------------
# 2. expected_seq CAS — exactly one winner under concurrent append attempts
# ---------------------------------------------------------------------------

def test_expected_seq_cas_one_winner_under_concurrency(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="race")

    n_workers = 8

    def attempt(i):
        # Distinct message_ids so idempotency never merges these — the CAS on
        # expected_seq=0 is the only thing that may let a writer through.
        with kb.connect() as c:
            try:
                return kb.append_event(
                    c, t, "transitioned", payload={"i": i},
                    expected_seq=0, message_id=f"m{i}",
                )
            except kb.ExpectedSeqMismatch:
                return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as ex:
        results = list(ex.map(attempt, range(n_workers)))

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].inserted is True
    assert winners[0].event.seq == 1
    # Exactly one seq-bearing row exists for the task.
    assert _protocol_seqs(t) == [1]


# ---------------------------------------------------------------------------
# 3. duplicate message_id idempotency returns the existing event, no 2nd row
# ---------------------------------------------------------------------------

def test_duplicate_message_id_same_request_is_idempotent(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")

    with kb.connect() as conn:
        first = kb.append_event(
            conn, t, "transitioned", payload={"to": "running"},
            expected_seq=0, message_id="dup", actor="a",
        )

    # Replay the SAME logical request. expected_seq=0 is now stale (head is 1),
    # but idempotency must short-circuit BEFORE the CAS check so the retry does
    # not raise — it returns the original event.
    with kb.connect() as conn:
        second = kb.append_event(
            conn, t, "transitioned", payload={"to": "running"},
            expected_seq=0, message_id="dup", actor="a",
        )

    assert first.inserted is True
    assert second.inserted is False
    assert second.event.id == first.event.id
    assert second.event.seq == first.event.seq == 1
    assert second.event.event_id == first.event.event_id
    assert second.current_seq == 1

    with kb.connect() as conn:
        dup_rows = [e for e in kb.list_events(conn, t) if e.message_id == "dup"]
    assert len(dup_rows) == 1  # no second row appended


def test_duplicate_message_id_conflicting_request_raises_typed_error(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")

    with kb.connect() as conn:
        first = kb.append_event(
            conn, t, "transitioned", payload={"to": "running"},
            expected_seq=0, message_id="dup",
        )

    # Same message_id, different logical payload → typed conflict, not a raw
    # sqlite uniqueness error, and nothing is written.
    with kb.connect() as conn:
        with pytest.raises(kb.EventIdempotencyConflict) as ei:
            kb.append_event(
                conn, t, "transitioned", payload={"to": "done"},
                expected_seq=0, message_id="dup",
            )
        assert ei.value.task_id == t
        assert ei.value.message_id == "dup"
        assert ei.value.existing_event_id == first.event.event_id

    with kb.connect() as conn:
        dup_rows = [e for e in kb.list_events(conn, t) if e.message_id == "dup"]
    assert len(dup_rows) == 1
    assert dup_rows[0].payload == {"to": "running"}


# ---------------------------------------------------------------------------
# 4. CAS loser fails/returns deterministically without blind retry
# ---------------------------------------------------------------------------

def test_cas_mismatch_is_deterministic_and_writes_nothing(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")

    with kb.connect() as conn:
        kb.append_event(conn, t, "transitioned", expected_seq=0, message_id="m1")

    # Head is now 1. A stale expected_seq=0 must mismatch and surface the
    # observed current_seq so the caller can re-read deterministically.
    with kb.connect() as conn:
        with pytest.raises(kb.ExpectedSeqMismatch) as ei:
            kb.append_event(conn, t, "transitioned", expected_seq=0, message_id="m2")
        assert ei.value.task_id == t
        assert ei.value.expected_seq == 0
        assert ei.value.current_seq == 1

    # Re-attempting the same stale write fails the same way — no blind retry
    # silently succeeded, and no row was appended.
    with kb.connect() as conn:
        with pytest.raises(kb.ExpectedSeqMismatch):
            kb.append_event(conn, t, "transitioned", expected_seq=0, message_id="m2")

    assert _protocol_seqs(t) == [1]

    # Re-reading the head and supplying the correct expected_seq now succeeds.
    with kb.connect() as conn:
        ok = kb.append_event(conn, t, "transitioned", expected_seq=1, message_id="m3")
    assert ok.inserted is True
    assert ok.event.seq == 2


# ---------------------------------------------------------------------------
# 5. legacy _append_event / list_events stay compatible with slice 1 rows
# ---------------------------------------------------------------------------

def test_legacy_append_event_and_list_events_remain_compatible(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")

        # Legacy compatibility writer: no seq/message_id/protocol fields.
        with kb.write_txn(conn):
            kb._append_event(conn, t, "commented", {"author": "a", "len": 3})

        # A protocol append coexists with the seq-less legacy rows.
        kb.append_event(conn, t, "transitioned", expected_seq=0, message_id="p1")

        events = kb.list_events(conn, t)

    by_kind = {e.kind: e for e in events}
    legacy = by_kind["commented"]
    assert legacy.seq is None
    assert legacy.message_id is None
    assert legacy.protocol is None
    assert legacy.payload == {"author": "a", "len": 3}

    proto = by_kind["transitioned"]
    assert proto.seq == 1
    assert proto.protocol == "hermes-kanban/1"

    # current_seq counts only seq-bearing rows; the legacy row does not perturb
    # the protocol head.
    assert _protocol_seqs(t) == [1]


# ===========================================================================
# Slice 3: deterministic pure projector + rebuild / quarantine harness.
# ===========================================================================

import time as _time

from hermes_cli import kanban_projector as kp


def _transition(conn, task_id, to_status, *, from_status, expected_seq, mid):
    """Append a well-formed v1 lifecycle transition event."""
    return kb.append_event(
        conn,
        task_id,
        "transitioned",
        payload={"to": to_status},
        expected_seq=expected_seq,
        message_id=mid,
        transition={"from": from_status, "to": to_status},
    )


def _raw_event(
    conn,
    task_id,
    *,
    seq,
    kind="transitioned",
    schema_version,
    transition=None,
    payload=None,
    event_id=None,
    message_id=None,
):
    """Insert a hand-crafted task_events row, bypassing the strict ``append_event``
    validation so legacy / poison / future-schema rows can be staged for the
    projector. Must run inside a ``write_txn``.
    """
    conn.execute(
        "INSERT INTO task_events ("
        "task_id, kind, payload, created_at, seq, event_id, message_id, "
        "schema_version, transition, protocol"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            kind,
            json.dumps(payload) if payload else None,
            int(_time.time()),
            seq,
            event_id,
            message_id,
            schema_version,
            json.dumps(transition) if transition else None,
            "hermes-kanban/1",
        ),
    )


# 1. rebuild projection matches the live shadow state for a basic lifecycle
def test_rebuild_matches_live_shadow_for_basic_lifecycle(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")
    with kb.connect() as conn:
        _transition(conn, t, "review", from_status="running", expected_seq=0, mid="m1")
        _transition(conn, t, "done", from_status="review", expected_seq=1, mid="m2")

    with kb.connect() as conn:
        events = kb.list_events(conn, t)
        live = kp.project_events(t, events)
        rebuilt = kb.rebuild_task_projection(conn, t)
        persisted = kb.get_task_projection(conn, t)

    assert live.state["status"] == "done"
    assert rebuilt.state_json == live.state_json
    assert rebuilt.state_hash == live.state_hash
    assert persisted.state_json == live.state_json
    assert persisted.state_hash == live.state_hash
    assert persisted.up_to_seq == rebuilt.up_to_seq == 2
    assert rebuilt.poison_count == 0


# 2. replay determinism: same events → same projection, no duplicate quarantine
def test_replay_is_deterministic_and_quarantine_idempotent(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")
        _transition(conn, t, "review", from_status="running", expected_seq=0, mid="m1")
        with kb.write_txn(conn):
            _raw_event(
                conn, t, seq=2, schema_version=1, transition=None,
                payload={"foo": "bar"}, event_id="ev_poison",
            )

    with kb.connect() as conn:
        r1 = kb.rebuild_task_projection(conn, t)
        r2 = kb.rebuild_task_projection(conn, t)

    assert r1.state_json == r2.state_json
    assert r1.state_hash == r2.state_hash
    assert r1.up_to_seq == r2.up_to_seq == 2
    assert r1.poison_count == r2.poison_count == 1

    with kb.connect() as conn:
        rows = kb.list_quarantined_events(conn, t)
    assert len(rows) == 1  # two rebuilds, still exactly one durable quarantine row


# 3. malformed v1 critical transition is quarantined with a stable decision_key
def test_malformed_v1_transition_is_quarantined_with_stable_decision_key(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")
        with kb.write_txn(conn):
            # schema_version 1 transitioned with no valid ``to`` → malformed.
            _raw_event(
                conn, t, seq=1, schema_version=1, kind="transitioned",
                transition={"from": "running"}, event_id="ev_bad",
            )

    with kb.connect() as conn:
        r1 = kb.rebuild_task_projection(conn, t)
    assert r1.poison_count == 1
    decision = r1.quarantined[0]
    assert decision.reason == kp.REASON_MALFORMED_TRANSITION
    key = decision.decision_key

    with kb.connect() as conn:
        r2 = kb.rebuild_task_projection(conn, t)
    assert r2.quarantined[0].decision_key == key  # stable across rebuilds

    with kb.connect() as conn:
        rows = kb.list_quarantined_events(conn, t)
    assert len(rows) == 1
    assert rows[0]["decision_key"] == key
    assert rows[0]["reason"] == kp.REASON_MALFORMED_TRANSITION


# 4. poison present: live, persisted and rebuild projections agree; poison isolated
def test_poison_event_isolated_live_and_rebuild_agree(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")
        _transition(conn, t, "review", from_status="running", expected_seq=0, mid="m1")
        with kb.write_txn(conn):
            _raw_event(conn, t, seq=2, schema_version=1, transition=None, event_id="ev_poison")
        # Head is now 2 (the raw row); the next good transition lands at seq 3.
        _transition(conn, t, "done", from_status="review", expected_seq=2, mid="m3")

    with kb.connect() as conn:
        events = kb.list_events(conn, t)
        live = kp.project_events(t, events)
        rebuilt = kb.rebuild_task_projection(conn, t)
        persisted = kb.get_task_projection(conn, t)

    # Poison is skipped; the two well-formed transitions still apply.
    assert live.state["status"] == "done"
    assert live.poison_count == 1
    assert rebuilt.state_json == live.state_json == persisted.state_json
    assert rebuilt.state_hash == live.state_hash == persisted.state_hash
    assert rebuilt.up_to_seq == live.up_to_seq == 3


# 5. poison in task A does not affect the projection for task B
def test_poison_in_task_a_does_not_affect_task_b(kanban_home):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
        with kb.write_txn(conn):
            _raw_event(conn, a, seq=1, schema_version=1, transition=None, event_id="ev_poison_a")
        _transition(conn, b, "done", from_status="running", expected_seq=0, mid="mb")

    with kb.connect() as conn:
        ra = kb.rebuild_task_projection(conn, a)
        rb = kb.rebuild_task_projection(conn, b)

    assert ra.poison_count == 1
    assert rb.poison_count == 0
    assert rb.state["status"] == "done"

    with kb.connect() as conn:
        assert len(kb.list_quarantined_events(conn, a)) == 1
        assert len(kb.list_quarantined_events(conn, b)) == 0


# 6. legacy schema_version 0 events project without v1 transition fields
def test_legacy_schema_version_0_projects_without_v1_fields(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")
        with kb.write_txn(conn):
            _raw_event(
                conn, t, seq=1, schema_version=0, kind="transitioned",
                transition=None, payload={"to": "done"}, event_id="ev_legacy",
            )

    with kb.connect() as conn:
        r = kb.rebuild_task_projection(conn, t)

    # Legacy events lack v1 transition fields but must NOT be quarantined; the
    # target status is read from the legacy payload via the compat adapter.
    assert r.poison_count == 0
    assert r.state["status"] == "done"


# 7. unknown/future critical schema versions are quarantined conservatively
def test_unknown_future_schema_critical_is_quarantined_deterministically(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")
        with kb.write_txn(conn):
            _raw_event(
                conn, t, seq=1, schema_version=99, kind="transitioned",
                transition={"from": "running", "to": "done"}, event_id="ev_future",
            )

    with kb.connect() as conn:
        r1 = kb.rebuild_task_projection(conn, t)
        r2 = kb.rebuild_task_projection(conn, t)

    assert r1.poison_count == 1
    assert r1.quarantined[0].reason == kp.REASON_UNKNOWN_SCHEMA
    # A future critical transition is NOT applied — conservatively quarantined.
    assert r1.state.get("status") != "done"
    assert r1.state_json == r2.state_json
    assert r1.state_hash == r2.state_hash

    with kb.connect() as conn:
        assert len(kb.list_quarantined_events(conn, t)) == 1


# Cursor / runtime-state invariants: rebuild must not mutate task_events ids or
# the live ``tasks`` row (projection stays shadow / non-enforcing).
def test_rebuild_does_not_mutate_events_or_task_state(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x")
        _transition(conn, t, "done", from_status="running", expected_seq=0, mid="m1")

    with kb.connect() as conn:
        before_events = [(e.id, e.seq, e.kind) for e in kb.list_events(conn, t)]
        before_status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (t,)
        ).fetchone()["status"]

        kb.rebuild_task_projection(conn, t)

        after_events = [(e.id, e.seq, e.kind) for e in kb.list_events(conn, t)]
        after_status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (t,)
        ).fetchone()["status"]

    assert before_events == after_events  # global id cursor untouched
    assert before_status == after_status  # runtime task state untouched by replay
