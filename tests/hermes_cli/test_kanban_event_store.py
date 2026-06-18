"""Tests for the Kanban Protocol v1 event store (hermes_cli.kanban_db).

Covers the storage half of the slice: the additive migration that backfills
protocol columns on legacy boards, the ``append_event`` compare-and-set +
``(task_id, message_id)`` idempotency contract, and the
``rebuild_task_projection`` harness (including poison-event quarantine and the
rebuild == live invariant).
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_projector as kp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Migration: backfill protocol columns on a legacy board
# ---------------------------------------------------------------------------

def test_legacy_migration_backfills_protocol_columns_and_preserves_list_events(
    tmp_path, monkeypatch
):
    """A pre-protocol board gains the columns, legacy events are backfilled
    deterministically, and the existing ``list_events`` reader still works."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="legacy")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    # Build the full current schema, then replace task_events with its
    # pre-slice shape (INTEGER PK + run_id, but no protocol columns) so the
    # additive migration runs on top while every other table stays current.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(kb.SCHEMA_SQL)
    conn.execute("DROP TABLE task_events")
    conn.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL, "
        "payload TEXT, created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('task-1', 'T', 'done', 1)"
    )
    # Two events for the same task with distinct created_at -> deterministic
    # backfill order.
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) "
                 "VALUES ('task-1', 'created', NULL, 1000)")
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) "
                 "VALUES ('task-1', 'completed', NULL, 1100)")
    conn.commit()
    conn.close()

    with kb.connect(db_path) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_events)")}
        for c in ("seq", "event_id", "message_id", "schema_version",
                  "actor", "source", "transition", "protocol"):
            assert c in cols, c

        rows = conn.execute(
            "SELECT id, kind, seq, event_id, schema_version "
            "FROM task_events ORDER BY created_at ASC, id ASC"
        ).fetchall()
        assert [r["seq"] for r in rows] == [1, 2]
        assert [r["event_id"] for r in rows] == [f"legacy:{rows[0]['id']}",
                                                  f"legacy:{rows[1]['id']}"]
        assert [r["schema_version"] for r in rows] == [0, 0]

        # The existing reader still returns the legacy events, in order.
        events = kb.list_events(conn, "task-1")
        assert [e.kind for e in events] == ["created", "completed"]
        # And it now surfaces the backfilled protocol fields.
        assert [e.seq for e in events] == [1, 2]


# ---------------------------------------------------------------------------
# append_event: compare-and-set + idempotency
# ---------------------------------------------------------------------------

def test_append_event_allocates_sequential_seq(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")
        r1 = kb.append_event(conn, tid, "promoted", expected_seq=1, message_id="m1")
        r2 = kb.append_event(conn, tid, "claimed", expected_seq=2, message_id="m2")
    assert (r1.inserted, r1.event.seq, r1.current_seq) == (True, 2, 2)
    assert (r2.inserted, r2.event.seq, r2.current_seq) == (True, 3, 3)
    assert r1.event.event_id  # auto-generated when not supplied


def test_duplicate_message_id_exact_replay_returns_existing(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")
        first = kb.append_event(
            conn, tid, "promoted", {"k": "v"}, expected_seq=1, message_id="dup"
        )
        # Same (task_id, message_id) + identical content -> idempotent replay.
        again = kb.append_event(
            conn, tid, "promoted", {"k": "v"}, expected_seq=1, message_id="dup"
        )
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM task_events WHERE task_id=?", (tid,)
        ).fetchone()["c"]
    assert first.inserted is True
    assert again.inserted is False
    assert again.event.seq == first.event.seq
    assert again.event.event_id == first.event.event_id
    assert again.current_seq == first.current_seq
    # created (1, via _append_event) + one appended event = 2 rows, no dup.
    assert count == 2


def test_duplicate_message_id_different_content_is_rejected(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")
        kb.append_event(conn, tid, "promoted", {"k": "v"}, expected_seq=1, message_id="dup")
        with pytest.raises(kb.MessageIdConflict):
            kb.append_event(
                conn, tid, "promoted", {"k": "DIFFERENT"}, expected_seq=1, message_id="dup"
            )


def test_expected_seq_mismatch_exposes_current_seq_and_writes_nothing(kanban_home):
    """CAS-loser semantics: the mismatch carries current_seq, no row is written,
    and re-reading + revalidating with the fresh seq succeeds."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")
        kb.append_event(conn, tid, "promoted", expected_seq=1, message_id="m1")

        before = conn.execute(
            "SELECT COUNT(*) AS c FROM task_events WHERE task_id=? AND seq IS NOT NULL",
            (tid,),
        ).fetchone()["c"]

        with pytest.raises(kb.ExpectedSeqMismatch) as exc:
            kb.append_event(conn, tid, "claimed", expected_seq=1, message_id="m2")
        assert exc.value.current_seq == 2

        after = conn.execute(
            "SELECT COUNT(*) AS c FROM task_events WHERE task_id=? AND seq IS NOT NULL",
            (tid,),
        ).fetchone()["c"]
        assert after == before  # the CAS loser wrote nothing

        # Re-read the head and revalidate — do NOT blindly retry the old seq.
        retried = kb.append_event(
            conn, tid, "claimed", expected_seq=exc.value.current_seq, message_id="m2"
        )
    assert retried.inserted is True
    assert retried.event.seq == 3


def test_concurrent_append_same_seq_has_exactly_one_winner(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")

    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def worker(mid: str) -> None:
        conn = kb.connect()
        try:
            barrier.wait(timeout=5)
            try:
                res = kb.append_event(conn, tid, "promoted", expected_seq=1, message_id=mid)
                with lock:
                    results.append(("win", res.event.seq))
            except kb.ExpectedSeqMismatch as exc:
                with lock:
                    results.append(("mismatch", exc.current_seq))
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(worker, ["a", "b"]))

    kinds = sorted(r[0] for r in results)
    assert kinds == ["mismatch", "win"]
    win = next(r for r in results if r[0] == "win")
    miss = next(r for r in results if r[0] == "mismatch")
    assert win[1] == 2          # the winner appended after the created event
    assert miss[1] == 2         # the loser sees current_seq == 2

    with kb.connect() as conn:
        seqd = conn.execute(
            "SELECT COUNT(*) AS c FROM task_events WHERE task_id=? AND seq IS NOT NULL",
            (tid,),
        ).fetchone()["c"]
    assert seqd == 2            # created event + exactly one protocol row


# ---------------------------------------------------------------------------
# rebuild_task_projection: determinism, poison quarantine, rebuild == live
# ---------------------------------------------------------------------------

def _seed_task_with_poison(conn, tid):
    """created (legacy) -> a v1 'completed' event with a missing transition."""
    kb.append_event(conn, tid, "promoted", expected_seq=1, message_id="c",
                    schema_version=0)
    kb.append_event(conn, tid, "completed", expected_seq=2, message_id="p",
                    schema_version=1, transition=None)


def test_rebuild_matches_live_projection_with_poison_event(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")
        _seed_task_with_poison(conn, tid)

        live = kb.rebuild_task_projection(conn, tid, persist=False)
        kb.rebuild_task_projection(conn, tid, persist=True)

        snap = conn.execute(
            "SELECT status, state_hash, quarantined_count "
            "FROM task_event_projections WHERE task_id=?",
            (tid,),
        ).fetchone()

    # The poison event was skipped: status stays at the pre-poison value.
    assert live.state.status == "ready"
    assert len(live.quarantined) == 1
    # Persisted snapshot is byte-for-byte the same projection as the live one.
    assert snap["state_hash"] == live.state.state_hash
    assert snap["status"] == "ready"
    assert snap["quarantined_count"] == 1


def test_poison_quarantine_is_deterministic_idempotent_and_isolated(kanban_home):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="poisoned")
        b = kb.create_task(conn, title="healthy")
        _seed_task_with_poison(conn, a)
        # Healthy task: a well-formed v1 transition, no poison.
        kb.append_event(conn, b, "promoted", expected_seq=1, message_id="bc",
                        schema_version=0)
        kb.append_event(conn, b, "completed", expected_seq=2, message_id="bp",
                        schema_version=1, transition="done")

        # Rebuild the poisoned task twice — quarantine must not duplicate.
        kb.rebuild_task_projection(conn, a, persist=True)
        kb.rebuild_task_projection(conn, a, persist=True)
        kb.rebuild_task_projection(conn, b, persist=True)

        a_q = conn.execute(
            "SELECT reason, kind, seq FROM task_event_quarantine WHERE task_id=?", (a,)
        ).fetchall()
        b_q = conn.execute(
            "SELECT COUNT(*) AS c FROM task_event_quarantine WHERE task_id=?", (b,)
        ).fetchone()["c"]
        b_status = conn.execute(
            "SELECT status FROM task_event_projections WHERE task_id=?", (b,)
        ).fetchone()["status"]

    assert len(a_q) == 1                      # idempotent across re-runs
    assert a_q[0]["reason"] == kp.REASON_MISSING_TRANSITION
    assert a_q[0]["kind"] == "completed"
    assert b_q == 0                           # poison isolated to task A
    assert b_status == "done"                 # B projected cleanly


def test_rebuild_does_not_mutate_tasks(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")
        before = kb.get_task(conn, tid)
        _seed_task_with_poison(conn, tid)
        kb.rebuild_task_projection(conn, tid, persist=True)
        after = kb.get_task(conn, tid)
    # This slice's rebuild persists a projection snapshot only; the canonical
    # tasks row is untouched.
    assert after.status == before.status


def test_delete_task_cleans_projection_and_quarantine(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")
        _seed_task_with_poison(conn, tid)
        kb.rebuild_task_projection(conn, tid, persist=True)
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM task_event_projections WHERE task_id=?", (tid,)
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM task_event_quarantine WHERE task_id=?", (tid,)
        ).fetchone()["c"] == 1

        assert kb.delete_task(conn, tid) is True

        assert conn.execute(
            "SELECT COUNT(*) AS c FROM task_event_projections WHERE task_id=?", (tid,)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM task_event_quarantine WHERE task_id=?", (tid,)
        ).fetchone()["c"] == 0
