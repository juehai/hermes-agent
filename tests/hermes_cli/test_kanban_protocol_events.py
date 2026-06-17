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
