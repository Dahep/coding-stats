"""Watermark resume + merge-date per-increment PR backfill (ticket #36,
ADR-0007 clause 2, re-keyed per the 2026-10-30 ruling).

The bounded sweep becomes watermark-driven run machinery: the resume marker
— the (merged_at, number) tuple of the last INCORPORATED PR — advances
after each PR's evidence is recorded (never per page), so every interruption
leaves a contiguous processed prefix in merge-date order. Resume starts at
the first PR past the marker: zero skipped PRs, zero duplicated rows
(replace-not-duplicate applies on resume). Full-history mode walks the whole
numbering sequence the same way and shares the
watermark; later push runs continue after it. Stop conditions (rate limit,
per-run cap, interruption) end a run cleanly with a notice, never a failure —
the next run resumes automatically. Closed-unmerged and other-base PRs are
never incorporated and never advance the watermark. Keyed by merge date,
ties by PR number: out-of-order merges are never lost (the 2026-10-30
ruling replaces the PR-number key of ticket #36).

This suite owns the run-machinery scenarios (interrupt/resume, disjoint
increments, clean stops, the shared full-history watermark, the contiguous-
claim invariant, replace-not-duplicate on resume) and the CLI faces (the
stop-notice face and the face flags); the merge-date marker's own semantics
live in test_pr_watermark.py. Builders live in conftest.py.
"""

import sqlite3
import subprocess
import urllib.error

import pytest

from coding_stats import aiattr, cli, gitwalk, prsource, prsweep
from coding_stats.db import connect
from conftest import (
    OBSERVED_AT,
    FakeTransport,
    cli_sweep_db,
    commit_files,
    commit_id,
    githubize,
    land_squash_prs,
    make_db,
    pr,
    pr_list_rows,
    pr_rows,
    primed_sweep_db,
    run,
)


def rows_per_pr(conn):
    return {
        int(r["pr_number"]): int(r["c"])
        for r in conn.execute("SELECT pr_number, COUNT(*) c FROM pr_evidence GROUP BY pr_number")
    }


def duplicate_rows(conn):
    return conn.execute(
        "SELECT pr_number, commit_id, rule, raw FROM pr_evidence"
        " GROUP BY pr_number, commit_id, rule, raw HAVING COUNT(*) > 1"
    ).fetchall()


class PrSpy:
    """Processes PRs through the real machinery while logging every
    incorporated PR number. `abort_after` interrupts (KeyboardInterrupt)
    before the next PR — the mid-run interruption simulation."""

    def __init__(self, monkeypatch):
        self.log: list[int] = []
        self.abort_after: int | None = None
        self._orig = prsweep._pr_rows
        monkeypatch.setattr(prsweep, "_pr_rows", self)

    def __call__(self, conn, repo, branch, pr_):
        if self.abort_after is not None and len(self.log) >= self.abort_after:
            raise KeyboardInterrupt
        rows = self._orig(conn, repo, branch, pr_)
        self.log.append(pr_.number)
        return rows


# ---------------------------------------------------------------------------
# acceptance criterion 1: interrupt + resume — zero skipped, zero duplicated
# ---------------------------------------------------------------------------


def test_interrupted_run_resumes_by_merge_date_with_zero_skips(tmp_path, repo, monkeypatch):
    # THE canonical interrupt/resume scenario, in merge-date order (NOT
    # number order: #3 merged first, then #2, then #1): a mid-run
    # interruption leaves a contiguous date-ordered prefix, the marker
    # tracks it, and the resume processes the first candidate past the
    # marker — zero skipped PRs, zero duplicated rows.
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2, 3])
    t1, t2, t3 = (
        "2026-10-25T12:00:00+00:00",
        "2026-10-23T12:00:00+00:00",
        "2026-10-21T12:00:00+00:00",
    )
    rows = [
        pr(3, body="Generated with Codex.\n", head="claude/f3", merged_at=t3),
        pr(2, body="Generated with Codex.\n", head="claude/f2", merged_at=t2),
        pr(1, body="Generated with Codex.\n", head="claude/f1", merged_at=t1),
    ]
    spy = PrSpy(monkeypatch)
    spy.abort_after = 2  # the third PR's processing dies mid-flight

    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(rows), now=OBSERVED_AT
    )
    assert res.stop == prsource.STOP_INTERRUPTED  # clean stop, never a failure
    assert res.processed == 2 and spy.log == [3, 2]  # merge-date order: #3 then #2
    assert res.watermark == 2 == prsweep.watermark(conn)  # the contiguous prefix
    assert prsweep.resume_key(conn) == (t2, 2)
    assert rows_per_pr(conn) == {2: 3, 3: 3}

    spy.abort_after = None
    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(rows), now=OBSERVED_AT
    )
    assert res2.stop is None and res2.processed == 1
    assert spy.log == [1]  # #1 exactly: skipping nothing
    assert prsweep.resume_key(conn) == (t1, 1)
    assert prsweep.watermark(conn) == 1  # the marker PR's own number
    assert rows_per_pr(conn) == {1: 3, 2: 3, 3: 3}  # zero skipped
    assert duplicate_rows(conn) == []  # zero duplicated evidence rows


# ---------------------------------------------------------------------------
# acceptance criterion 2: consecutive push-run increments are disjoint
# ---------------------------------------------------------------------------


def test_two_consecutive_increments_process_disjoint_pr_ranges(tmp_path, repo, monkeypatch):
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2, 3])
    spy = PrSpy(monkeypatch)
    res1 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1, 2, 3)), now=OBSERVED_AT
    )
    assert spy.log == [1, 2, 3] and res1.watermark == 3

    land_squash_prs(repo, [4, 5])  # two new PRs land between the runs
    gitwalk.walk(repo, conn, "main")  # every update run walks before the sweep
    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1, 2, 3, 4, 5)), now=OBSERVED_AT
    )
    assert spy.log == [4, 5]  # disjoint from the first run's range
    assert min(spy.log) > res1.watermark  # second starts above the first's watermark
    assert res2.watermark == 5
    assert rows_per_pr(conn) == {1: 3, 2: 3, 3: 3, 4: 3, 5: 3}
    assert duplicate_rows(conn) == []


# ---------------------------------------------------------------------------
# acceptance criterion 4: rate-limit exhaustion stops cleanly, next run resumes
# ---------------------------------------------------------------------------


def test_rate_limit_stops_cleanly_and_the_next_run_resumes(tmp_path, repo, monkeypatch):
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2, 3])
    spy = PrSpy(monkeypatch)
    res0 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1, 2, 3)), now=OBSERVED_AT
    )
    assert res0.watermark == 3

    land_squash_prs(repo, [4, 5])  # new PRs arrive; the API budget is gone
    gitwalk.walk(repo, conn, "main")

    def limited(url):
        # genuine exhaustion: 403 with the rate budget at zero
        raise urllib.error.HTTPError(url, 403, "rate limited", {"X-RateLimit-Remaining": "0"}, None)

    spy.log.clear()
    res = prsweep.sweep_increment(conn, repo, "main", transport=limited, now=OBSERVED_AT)
    assert res.stop == prsource.STOP_RATE_LIMIT  # a clean stop, never a failure
    assert res.processed == 0 and spy.log == []
    assert res.watermark == 3 == prsweep.watermark(conn)  # untouched: resume here

    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1, 2, 3, 4, 5)), now=OBSERVED_AT
    )
    assert res2.stop is None and spy.log == [4, 5] and res2.watermark == 5


# ---------------------------------------------------------------------------
# the watermark never advances past a PR that cannot be incorporated
# ---------------------------------------------------------------------------


def test_pr_merged_after_the_walk_stops_the_run_at_that_pr(tmp_path, repo, monkeypatch):
    # PR 3 merged AFTER the last walk: its landing sha is not in `commits`,
    # so the run stops there cleanly — never records suspicious rows and
    # never claims past it; the next run (after a newer walk) redoes it
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2])
    spy = PrSpy(monkeypatch)
    shas3 = land_squash_prs(repo, [3])  # merged after the walk — nothing walked yet
    # rows an earlier failed attempt left behind: the redo must REPLACE them
    prsweep.replace_pr_rows(
        conn,
        3,
        [prsweep.PrRow("tool-trailer", "stale:row", "yes", None, True, None, None)],
        "2026-10-29T00:00:00+00:00",
    )
    conn.commit()
    rows = [
        pr(3, body="Generated with Codex.\n", head="claude/f3", merge_commit_sha=shas3[3]),
        pr(2, body="Generated with Codex.\n", head="claude/f2", merge_commit_sha=shas[2]),
        pr(1, body="Generated with Codex.\n", head="claude/f1", merge_commit_sha=shas[1]),
    ]
    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(rows), now=OBSERVED_AT
    )
    assert res.stop == prsweep.STOP_WALK_STALE  # "waiting for the walk to catch up"
    assert res.processed == 2 and spy.log == [1, 2]  # stopped AT PR 3
    assert res.watermark == 2 == prsweep.watermark(conn)  # never past 3

    gitwalk.walk(repo, conn, "main")  # the walk catches up
    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(rows), now=OBSERVED_AT
    )
    assert res2.stop is None and spy.log == [3]  # resumed: the PR is redone
    assert prsweep.watermark(conn) == 3
    assert ("tool-trailer", "stale:row") not in {(r[2], r[3]) for r in pr_rows(conn, 3)}
    assert rows_per_pr(conn) == {1: 3, 2: 3, 3: 3}  # replaced, never extended
    assert duplicate_rows(conn) == []


def test_unmapped_pr_without_a_landing_sha_waits_for_the_walk_too(tmp_path, repo, monkeypatch):
    # an API body with no merge_commit_sha: nothing maps onto the walked set,
    # so the watermark must not claim past it either — a conservative wait,
    # never suspicious rows
    conn, shas = primed_sweep_db(tmp_path, repo, [1])
    spy = PrSpy(monkeypatch)
    land_squash_prs(repo, [2])  # merged after the walk
    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(2)), now=OBSERVED_AT
    )
    assert res.stop == prsweep.STOP_WALK_STALE
    assert res.processed == 0 and spy.log == []
    assert prsweep.watermark(conn) == 0  # nothing claimed, PR 2 never past it
    assert pr_rows(conn, 2) == []  # nothing recorded, nothing suspicious


def test_transient_head_fetch_failure_stops_cleanly_without_suspicious_rows(
    tmp_path, repo, monkeypatch
):
    # a broken/unreachable remote is a TRANSIENT failure: the run stops
    # cleanly at that PR — it must never be recorded as a vanished-ref
    # suspicious set nor claimed by the watermark
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2])
    spy = PrSpy(monkeypatch)
    res0 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1)), now=OBSERVED_AT
    )
    assert res0.processed == 1 and prsweep.watermark(conn) == 1

    run(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))  # unreachable
    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1, 2)), now=OBSERVED_AT
    )
    assert res.stop == prsweep.STOP_FETCH  # distinct stop: transient fetch failure
    assert res.processed == 0 and prsweep.watermark(conn) == 1  # never past 2
    assert pr_rows(conn, 2) == []  # nothing recorded, nothing suspicious

    run(repo, "remote", "set-url", "origin", str(tmp_path / "remote.git"))  # back
    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1, 2)), now=OBSERVED_AT
    )
    assert res2.stop is None and spy.log == [2] and prsweep.watermark(conn) == 2


# ---------------------------------------------------------------------------
# stop conditions: the per-run processing cap
# ---------------------------------------------------------------------------


def test_per_run_cap_stops_cleanly_and_the_next_run_continues(tmp_path, repo, monkeypatch):
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2, 3, 4, 5])
    spy = PrSpy(monkeypatch)
    res = prsweep.sweep_increment(
        conn,
        repo,
        "main",
        transport=FakeTransport(pr_list_rows(1, 2, 3, 4, 5)),
        now=OBSERVED_AT,
        max_prs=2,
    )
    # one listing page covers all five PRs, yet the watermark moved per PR
    assert res.requests == 1
    assert res.stop == prsweep.STOP_CAP and spy.log == [1, 2] and res.watermark == 2

    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn,
        repo,
        "main",
        transport=FakeTransport(pr_list_rows(1, 2, 3, 4, 5)),
        now=OBSERVED_AT,
        max_prs=2,
    )
    assert res2.stop == prsweep.STOP_CAP and spy.log == [3, 4] and res2.watermark == 4

    spy.log.clear()
    res3 = prsweep.sweep_increment(
        conn,
        repo,
        "main",
        transport=FakeTransport(pr_list_rows(1, 2, 3, 4, 5)),
        now=OBSERVED_AT,
        max_prs=2,
    )
    assert res3.stop is None and spy.log == [5] and res3.watermark == 5


# ---------------------------------------------------------------------------
# full-history mode: same walk, shared watermark, later increments continue
# ---------------------------------------------------------------------------


def test_full_history_mode_shares_the_watermark_and_increments_continue_after_it(
    tmp_path, repo, monkeypatch
):
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2, 3])
    spy = PrSpy(monkeypatch)
    prsweep.sweep_increment(
        conn,
        repo,
        "main",
        transport=FakeTransport(pr_list_rows(1, 2, 3)),
        now=OBSERVED_AT,
        max_prs=2,
    )
    assert prsweep.watermark(conn) == 2  # a capped increment left 3 behind

    spy.log.clear()
    full = prsweep.sweep_full(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1, 2, 3)), now=OBSERVED_AT
    )
    assert spy.log == [1, 2, 3]  # the whole numbering sequence, oldest-first
    assert full.stop is None and full.watermark == 3
    # the re-walk replaces rows in place: nothing duplicated
    assert rows_per_pr(conn) == {1: 3, 2: 3, 3: 3} and duplicate_rows(conn) == []

    land_squash_prs(repo, [4])  # later push runs continue after it
    gitwalk.walk(repo, conn, "main")
    spy.log.clear()
    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1, 2, 3, 4)), now=OBSERVED_AT
    )
    assert spy.log == [4] and res.watermark == 4


# ---------------------------------------------------------------------------
# skipped PR numbers: never incorporated, below the next merged PR's watermark
# ---------------------------------------------------------------------------


def test_unmerged_and_other_base_prs_are_never_incorporated(tmp_path, repo, monkeypatch):
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 3])  # PR 2 closed-unmerged, PR 4 other-base
    spy = PrSpy(monkeypatch)
    res = prsweep.sweep_full(
        conn,
        repo,
        "main",
        transport=FakeTransport(
            [
                pr(4, head="claude/f3", base="develop"),
                pr(3, head="claude/f3", body="Generated with Codex.\n"),
                pr(2, head="claude/f3", merged=False),
                pr(1, head="claude/f1", body="Generated with Codex.\n"),
            ]
        ),
        now=OBSERVED_AT,
    )
    assert spy.log == [1, 3]
    assert res.watermark == 3  # skipped numbers sit below the next merged PR's
    assert rows_per_pr(conn) == {1: 3, 3: 3}
    assert pr_rows(conn, 2) == [] and pr_rows(conn, 4) == []


# ---------------------------------------------------------------------------
# the recorded watermark's invariant: contiguous coverage only
# ---------------------------------------------------------------------------


def test_explicit_bound_above_the_watermark_records_but_never_advances_it(
    tmp_path, repo, monkeypatch
):
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2, 3])
    spy = PrSpy(monkeypatch)
    res = prsweep.sweep(
        conn, repo, "main", 2, transport=FakeTransport(pr_list_rows(1, 2, 3)), now=OBSERVED_AT
    )
    assert spy.log == [3]
    assert res.watermark == 0  # the gap is never claimed: zero skipped stays true

    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1, 2, 3)), now=OBSERVED_AT
    )
    assert spy.log == [1, 2, 3]  # earlier records are re-replaced, never duplicated
    assert res2.watermark == 3
    assert rows_per_pr(conn) == {1: 3, 2: 3, 3: 3} and duplicate_rows(conn) == []


def test_a_positive_bound_never_advances_the_marker_past_a_late_lower_number(
    tmp_path, repo, monkeypatch
):
    # The merge-date gap the number bound cannot see: the marker sits at
    # (D50, #50) while PR #20 merges LATE (D52) — below the bound 30. The
    # bounded run records #60 (D55), but advancing the marker to #60's
    # tuple would leave #20's (D52, #20) below it and a later increment
    # would skip the late lower number forever. A positive --after-pr bound
    # NEVER advances the merge-date marker; only bound 0/full may claim
    # coverage. The later increment must still pick #20 up.
    conn, shas = primed_sweep_db(tmp_path, repo, [20, 60])
    d50 = "2026-10-20T12:00:00+00:00"
    d52 = "2026-10-22T12:00:00+00:00"
    d55 = "2026-10-25T12:00:00+00:00"
    prsweep.advance_watermark(conn, d50, 50)  # the operator's prior marker
    conn.commit()
    rows = [
        pr(60, body="Generated with Codex.\n", head="claude/f60", merged_at=d55),
        pr(20, body="Generated with Codex.\n", head="claude/f20", merged_at=d52),
    ]
    spy = PrSpy(monkeypatch)
    res = prsweep.sweep(conn, repo, "main", 30, transport=FakeTransport(rows), now=OBSERVED_AT)
    assert spy.log == [60] and res.processed == 1  # the bound selects only #60
    assert rows_per_pr(conn) == {60: 3}  # the bounded run records its candidates
    assert prsweep.resume_key(conn) == (d50, 50)  # the marker never moved
    assert prsweep.watermark(conn) == 50

    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(rows), now=OBSERVED_AT
    )
    assert spy.log == [20, 60]  # the late lower number is still picked up
    assert res2.watermark == 60 and prsweep.resume_key(conn) == (d55, 60)
    assert rows_per_pr(conn) == {20: 3, 60: 3} and duplicate_rows(conn) == []


def test_half_recorded_pr_is_replaced_not_extended_on_resume(tmp_path, repo, monkeypatch):
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2])
    spy = PrSpy(monkeypatch)
    prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1)), now=OBSERVED_AT
    )
    assert prsweep.watermark(conn) == 1

    # a dead run recorded PR 2's rows but died before its watermark moved
    prsweep.replace_pr_rows(
        conn,
        2,
        [
            prsweep.PrRow(
                "tool-trailer", "stale:row", "yes", commit_id(conn, shas[2]), False, "x", None
            )
        ],
        "2026-10-29T00:00:00+00:00",
    )
    conn.commit()
    assert prsweep.watermark(conn) == 1  # rows-only record: still unadvanced

    spy.log.clear()
    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1, 2)), now=OBSERVED_AT
    )
    assert spy.log == [2]
    rows2 = pr_rows(conn, 2)
    assert ("tool-trailer", "stale:row") not in {(r[2], r[3]) for r in rows2}
    assert len(rows2) == 3 and duplicate_rows(conn) == []  # replaced, not extended
    assert res.watermark == 2


# ---------------------------------------------------------------------------
# per-PR atomicity: the row replacement and the marker advance commit or
# roll back TOGETHER — a write-phase failure never leaks partial state
# ---------------------------------------------------------------------------


def test_incorporate_pr_rolls_back_rows_and_marker_on_a_write_phase_failure(
    tmp_path, repo, monkeypatch
):
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2])
    # PR 2's prior rows and the marker an earlier run left behind
    prsweep.replace_pr_rows(
        conn,
        2,
        [
            prsweep.PrRow(
                "tool-trailer", "prior:row", "yes", commit_id(conn, shas[2]), False, "x", None
            )
        ],
        "2026-10-28T00:00:00+00:00",
    )
    prsweep.advance_watermark(conn, "2026-10-20T12:00:00+00:00", 1)
    conn.commit()
    prior_rows = pr_rows(conn, 2)
    prior_marker = prsweep.resume_key(conn)

    orig = prsweep.advance_watermark

    def advancing_then_dying(c, merged_at, pr_number):
        orig(c, merged_at, pr_number)  # the marker write lands in the transaction...
        raise RuntimeError("write phase died")  # ...then the write phase dies

    monkeypatch.setattr(prsweep, "advance_watermark", advancing_then_dying)
    pulled = prsource._parse_pr(pr(2, body="Generated with Codex.\n", head="claude/f2"))
    with pytest.raises(RuntimeError):
        prsweep.incorporate_pr(conn, repo, "main", pulled, now=OBSERVED_AT, advance=True)
    conn.commit()  # a later write phase must never pick partial state up
    assert pr_rows(conn, 2) == prior_rows  # the PR's previous rows survive
    assert prsweep.resume_key(conn) == prior_marker  # and the marker too


# ---------------------------------------------------------------------------
# schema step: pre-watermark working DBs migrate on open
# ---------------------------------------------------------------------------


def test_pr_watermark_schema_step_migrates_pre_watermark_dbs(tmp_path, repo):
    conn = make_db(tmp_path / "w.db", repo)
    conn.execute("DROP TABLE pr_watermark")
    conn.execute("DROP TABLE _schema_step_0006")
    conn.execute("DROP TABLE _schema_step_0007")  # pre-watermark: both steps pending
    conn.commit()
    conn.close()
    migrated = connect(tmp_path / "w.db")
    cols = [str(r["name"]) for r in migrated.execute("PRAGMA table_info(pr_watermark)")]
    assert cols == ["repo_id", "last_pr_number", "last_merged_at"]
    migrated.close()


def test_step_7_preserves_a_step_6_watermark_on_open(tmp_path, repo):
    # a number-keyed-era DB (pr_watermark without a merge date) migrates on
    # open: the column is added and the recorded watermark survives it —
    # its date is NULL, a marker that claims no merge date
    conn = make_db(tmp_path / "w.db", repo)
    conn.execute("ALTER TABLE pr_watermark DROP COLUMN last_merged_at")
    conn.execute(
        "INSERT INTO pr_watermark (repo_id, last_pr_number)"
        " VALUES ((SELECT id FROM repositories ORDER BY id LIMIT 1), 7)"
    )
    conn.execute("DROP TABLE _schema_step_0007")
    conn.commit()
    conn.close()
    migrated = connect(tmp_path / "w.db")
    cols = [str(r["name"]) for r in migrated.execute("PRAGMA table_info(pr_watermark)")]
    assert cols == ["repo_id", "last_pr_number", "last_merged_at"]
    row = migrated.execute("SELECT last_pr_number, last_merged_at FROM pr_watermark").fetchone()
    assert (row["last_pr_number"], row["last_merged_at"]) == (7, None)  # preserved
    migrated.close()


def test_pr_disclosure_schema_step_migrates_pre_step_8_dbs_and_is_idempotent(tmp_path, repo):
    # the marker table arrives at step 8; a pre-step-8 DB migrates on open,
    # and re-running the step (its bookkeeping dropped) is a no-op that keeps
    # recorded markers — the disclosure marker is recorded state
    conn = make_db(tmp_path / "w.db", repo)
    conn.execute("DROP TABLE pr_disclosure")
    conn.execute("DROP TABLE _schema_step_0008")  # pre-step-8: the step is pending
    conn.commit()
    conn.close()
    migrated = connect(tmp_path / "w.db")
    cols = [str(r["name"]) for r in migrated.execute("PRAGMA table_info(pr_disclosure)")]
    assert cols == ["repo_id"]  # minimal: repo_id is the primary key, nothing else
    assert migrated.execute("SELECT COUNT(*) c FROM pr_disclosure").fetchone()["c"] == 0
    migrated.execute(
        "INSERT INTO pr_disclosure (repo_id)"
        " VALUES ((SELECT id FROM repositories ORDER BY id LIMIT 1))"
    )
    migrated.commit()
    migrated.close()

    reopened = connect(tmp_path / "w.db")
    assert reopened.execute("SELECT COUNT(*) c FROM pr_disclosure").fetchone()["c"] == 1
    reopened.execute("DROP TABLE _schema_step_0008")  # force the migration to run again
    reopened.commit()
    reopened.close()
    third = connect(tmp_path / "w.db")
    assert third.execute("SELECT COUNT(*) c FROM pr_disclosure").fetchone()["c"] == 1  # idempotent
    third.close()


# ---------------------------------------------------------------------------
# pagination: the increment's window never stops before a late deep merge
# ---------------------------------------------------------------------------


def test_the_increment_window_reaches_low_numbers_that_merged_late(tmp_path, repo, monkeypatch):
    # The window cut is the wave's heart. "Stop when the page's min merged_at
    # <= the marker's date" would stop at page 1 here and lose PR 30 — an
    # old-numbered PR that merged LATE (its merge date is newer than the
    # marker's), sitting in page 2 among neighbours that merged long before
    # the marker. No date-based early stop is sound on number-descending
    # pages (an older number can always merge later), so the increment's
    # window walks the list to the end (list pages only) and cuts candidates
    # from it by (merged_at, number) > the marker.
    conn = make_db(tmp_path / "w.db", repo)
    githubize(conn)
    old = "2026-10-01T12:00:00+00:00"
    late = "2026-10-29T12:00:00+00:00"
    prsweep.advance_watermark(conn, "2026-10-15T12:00:00+00:00", 150)
    rows = [pr(n, merged_at=(late if n == 30 else old)) for n in range(150, 0, -1)]
    t = FakeTransport(rows)  # pages of 100: PR 30 sits in page 2
    log: list[int] = []

    def stub(_conn, _repo, _branch, pulled):
        log.append(pulled.number)
        return []

    monkeypatch.setattr(prsweep, "_pr_rows", stub)
    res = prsweep.sweep_increment(conn, repo, "main", transport=t, now=OBSERVED_AT)
    assert res.requests == 2  # the deep page is fetched: nothing may hide below
    assert log == [30]  # the late merge is a candidate despite its low number
    assert prsweep.resume_key(conn) == (late, 30)


# ---------------------------------------------------------------------------
# CLI faces (the one #37 wires into the update pipeline): the increment
# default, the stop-notice face — the ONE canonical home for the
# run-machinery stop notices (cap / rate-limit / forbidden; the
# unavailable notice's sanitization and prefix accounting keep their own
# dedicated suites) — and the --full / --after-pr flags sharing the
# watermark
# ---------------------------------------------------------------------------


def test_cli_pr_sweep_default_is_the_increment_face(tmp_path, repo, monkeypatch, capsys):
    db = cli_sweep_db(tmp_path, repo, [1, 2, 3])
    monkeypatch.setattr(
        prsource, "default_transport", lambda url: FakeTransport(pr_list_rows(1, 2, 3))(url)
    )
    cli.main(["pr-sweep", "--db", str(db)])
    out = capsys.readouterr().out
    assert "pr-sweep" in out and "3 PRs" in out and "watermark 3" in out

    land_squash_prs(repo, [4, 5])  # new PRs land; the default face resumes
    cli.main(["walk", "--db", str(db)])  # every run walks before the sweep
    monkeypatch.setattr(
        prsource, "default_transport", lambda url: FakeTransport(pr_list_rows(1, 2, 3, 4, 5))(url)
    )
    cli.main(["pr-sweep", "--db", str(db)])
    out = capsys.readouterr().out
    assert "2 PRs" in out and "watermark 5" in out


def read_watermark(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    wm = prsweep.watermark(conn)
    conn.close()
    return wm


def test_cli_pr_sweep_stop_notice_is_never_a_failure(tmp_path, repo, monkeypatch, capsys):
    # the canonical CLI stop-notice face: every stop condition ends the run
    # with a ::notice:: on stdout and exit 0 — the rate limit (the
    # transport's own RateLimited exception, the shape production raises)
    # and the run's own processing cap
    db = cli_sweep_db(tmp_path, repo, [1, 2, 3])

    def limited(url):
        raise prsource.RateLimited("rate limit exhausted")

    for transport, argv, fragment, recorded, watermark in [
        (limited, [], "rate limit exhausted", 0, 0),
        (
            FakeTransport(pr_list_rows(1, 2, 3)),
            ["--max-prs", "2"],
            "processing cap reached",
            2,
            2,
        ),
    ]:
        monkeypatch.setattr(prsource, "default_transport", lambda url, t=transport: t(url))
        cli.main(["pr-sweep", "--db", str(db), *argv])  # no failure exit
        out = capsys.readouterr().out
        assert "::notice::pr-sweep stopped" in out and fragment in out
        assert f"{recorded} PRs recorded" in out
        assert "next run resumes" in out
        assert read_watermark(db) == watermark


def test_cli_forbidden_notice_names_the_permission_not_the_rate_limit(
    tmp_path, repo, monkeypatch, capsys
):
    # "Resource not accessible by integration" is a PERMISSION failure, not
    # rate-limit exhaustion: the notice names the missing permission, never
    # claims a rate limit and never promises a resume
    db = cli_sweep_db(tmp_path, repo, [1])

    def denied(url):
        raise urllib.error.HTTPError(url, 403, "Resource not accessible by integration", {}, None)

    monkeypatch.setattr(prsource, "default_transport", denied)
    cli.main(["pr-sweep", "--db", str(db)])  # a clean stop, never a failure exit
    out = capsys.readouterr().out
    assert "::notice::" in out
    assert "pull-requests: read" in out  # the notice names the permission
    assert "rate limit exhausted" not in out
    assert "next run resumes" not in out


def test_cli_full_flag_re_sweeps_from_the_beginning(tmp_path, repo, monkeypatch, capsys):
    db = cli_sweep_db(tmp_path, repo, [1, 2])
    t = FakeTransport(pr_list_rows(1, 2))
    monkeypatch.setattr(prsource, "default_transport", lambda url: t(url))

    cli.main(["pr-sweep", "--db", str(db), "--after-pr", "1"])  # adoption bound
    assert read_watermark(db) == 0  # the gap below the bound is never claimed
    conn = sqlite3.connect(db)
    assert {r[1] for r in pr_rows(conn)} == {2}  # only the bound's own candidates
    conn.close()

    cli.main(["pr-sweep", "--db", str(db), "--full"])  # operator's full-history sweep
    out = capsys.readouterr().out
    assert "2 PRs recorded" in out
    conn = sqlite3.connect(db)
    assert {r[1] for r in pr_rows(conn)} == {1, 2}
    conn.close()
    assert read_watermark(db) == 2  # shared watermark: contiguous now


def test_cli_pr_sweep_rejects_full_with_after_pr(tmp_path, repo):
    commit_files(repo, "init", {"a.txt": "a\n"})  # init identifies the branch via HEAD
    db = tmp_path / "cli.db"
    cli.main(["init", str(repo), "--db", str(db)])
    with pytest.raises(SystemExit):
        cli.main(["pr-sweep", "--db", str(db), "--full", "--after-pr", "0"])


# ---------------------------------------------------------------------------
# the run() entry point: face selection, default caps, and operational-
# failure conversion (the ONE face the CLI verbs call)
# ---------------------------------------------------------------------------


def _counting_rows(monkeypatch):
    """Stub the per-PR step: log every candidate number, record nothing."""
    log: list[int] = []

    def stub(_conn, _repo, _branch, pulled):
        log.append(pulled.number)
        return []

    monkeypatch.setattr(prsweep, "_pr_rows", stub)
    return log


def _thirty_prs():
    return [pr(n) for n in range(30, 0, -1)]  # API order: descending number


def test_run_honours_the_cap_and_rejects_full_with_after_pr(tmp_path, repo, monkeypatch):
    # the cap binds whichever face runs — the default increment face at its
    # DEFAULT_RUN_CAP, an explicit max_prs on the full face — and the full
    # and --after-pr faces stay mutually exclusive
    conn = make_db(tmp_path / "w.db", repo)
    githubize(conn)
    log = _counting_rows(monkeypatch)
    t = FakeTransport(_thirty_prs())
    res = prsweep.run(conn, repo, "main", transport=t, now=OBSERVED_AT)
    assert log == list(range(1, 26))  # the increment face caps at 25 by default
    assert res.stop == prsweep.STOP_CAP and res.processed == 25
    assert res.watermark == 25  # the marker moved per incorporated PR

    log.clear()
    res = prsweep.run(conn, repo, "main", full=True, max_prs=2, transport=t, now=OBSERVED_AT)
    assert log == [1, 2] and res.stop == prsweep.STOP_CAP
    with pytest.raises(prsweep.SweepError):
        prsweep.run(conn, repo, "main", after_pr=1, full=True, transport=t, now=OBSERVED_AT)


def test_run_full_face_is_the_uncapped_full_history_sweep(tmp_path, repo, monkeypatch):
    conn = make_db(tmp_path / "w.db", repo)
    githubize(conn)
    log = _counting_rows(monkeypatch)
    t = FakeTransport(_thirty_prs())
    res = prsweep.run(conn, repo, "main", full=True, transport=t, now=OBSERVED_AT)
    assert log == list(range(1, 31))  # unlimited by default
    assert res.stop is None and res.processed == 30


def test_run_after_pr_face_is_the_uncapped_bounded_sweep(tmp_path, repo, monkeypatch):
    conn = make_db(tmp_path / "w.db", repo)
    githubize(conn)
    log = _counting_rows(monkeypatch)
    t = FakeTransport(_thirty_prs())
    res = prsweep.run(conn, repo, "main", after_pr=27, transport=t, now=OBSERVED_AT)
    assert log == [28, 29, 30]  # the number bound selects, unlimited by default
    assert res.stop is None and res.watermark == 0  # the gap below the bound: never claimed


def test_run_converts_operational_failures_into_a_stop_naming_the_detail(tmp_path, repo):
    conn = make_db(tmp_path / "w.db", repo)
    githubize(conn)

    def boom(_url):
        raise ValueError("bad API body")

    res = prsweep.run(conn, repo, "main", transport=boom, now=OBSERVED_AT)
    assert res.stop == prsweep.STOP_UNAVAILABLE  # a clean stop, never an exception
    assert res.detail == "bad API body"  # useful detail rides the result
    assert res.processed == 0 and res.watermark == 0  # nothing may be claimed here


def test_run_unclassified_http_failure_is_unavailable_not_mislabelled(tmp_path, repo):
    conn = make_db(tmp_path / "w.db", repo)
    githubize(conn)

    def boom(_url):
        raise urllib.error.HTTPError("url", 500, "Server Error", hdrs={}, fp=None)

    res = prsweep.run(conn, repo, "main", transport=boom, now=OBSERVED_AT)
    assert res.stop == prsweep.STOP_UNAVAILABLE  # operational, never a traceback
    assert res.stop != prsource.STOP_RATE_LIMIT
    assert "500" in (res.detail or "")


def test_run_mid_processing_failure_preserves_the_committed_prefix(tmp_path, repo, monkeypatch):
    conn = make_db(tmp_path / "w.db", repo)
    githubize(conn)

    def fail_second(_conn, _repo, _branch, pulled):
        if pulled.number == 2:
            raise subprocess.CalledProcessError(1, ["git", "rev-list"])
        return []

    monkeypatch.setattr(prsweep, "_pr_rows", fail_second)
    res = prsweep.run(conn, repo, "main", transport=FakeTransport([pr(2), pr(1)]), now=OBSERVED_AT)
    assert res.stop == prsweep.STOP_UNAVAILABLE
    assert res.processed == 1  # PR 1 committed before PR 2 failed
    assert res.watermark == 1  # the result reports the advanced marker, not 'kept'
    assert res.requests == 1
    assert "rev-list" in (res.detail or "")


def test_run_keeps_sweep_error_explicit_never_a_clean_stop(tmp_path, repo):
    conn = make_db(tmp_path / "w.db", repo)  # no GitHub identity: SweepError
    with pytest.raises(prsweep.SweepError):
        prsweep.run(conn, repo, "main", transport=FakeTransport([]), now=OBSERVED_AT)


# ---------------------------------------------------------------------------
# the PR-channel disclosure marker (schema step 8): every actual sweep face
# records it before listing any PR — a row means the PR tier has RUN, even
# when the run finds zero non-suspicious rows (or dies operationally right
# after). Config errors surface as SweepError BEFORE that and record nothing.
# ---------------------------------------------------------------------------


def _disclosed(conn):
    return conn.execute("SELECT 1 FROM pr_disclosure LIMIT 1").fetchone() is not None


def _disclosed_rows(conn):
    return conn.execute("SELECT 1 FROM pr_evidence LIMIT 1").fetchone() is not None


@pytest.mark.parametrize("face", ["increment", "backfill", "bounded", "full"])
def test_every_sweep_face_records_the_disclosure_marker_before_any_work(tmp_path, repo, face):
    conn = make_db(tmp_path / "w.db", repo)
    githubize(conn)
    t = FakeTransport([])  # an empty listing: zero PRs, zero evidence rows
    kw = {"transport": t, "now": OBSERVED_AT}
    faces = {
        "increment": lambda: prsweep.sweep_increment(conn, repo, "main", **kw),
        "backfill": lambda: prsweep.sweep_backfill(conn, repo, "main", **kw),
        "bounded": lambda: prsweep.sweep(conn, repo, "main", 3, **kw),
        "full": lambda: prsweep.sweep_full(conn, repo, "main", **kw),
    }
    res = faces[face]()
    assert res.processed == 0 and res.rows == 0  # zero non-suspicious evidence rows
    assert _disclosed(conn)  # yet the marker: the PR tier has run
    assert not _disclosed_rows(conn)  # never as a substitute for evidence rows
    conn.close()


def test_the_marker_is_recorded_before_listing_so_operational_failure_still_discloses(
    tmp_path, repo
):
    conn = make_db(tmp_path / "w.db", repo)
    githubize(conn)

    def boom(_url):
        raise ValueError("bad API body")

    res = prsweep.run(conn, repo, "main", transport=boom, now=OBSERVED_AT)
    assert res.stop == prsweep.STOP_UNAVAILABLE  # the listing died...
    assert _disclosed(conn)  # ...but the marker landed before it: disclosure stays on
    prsweep.run(conn, repo, "main", transport=boom, now=OBSERVED_AT)  # recorded once
    assert int(conn.execute("SELECT COUNT(*) c FROM pr_disclosure").fetchone()["c"]) == 1
    conn.close()


def test_invalid_config_records_no_disclosure_marker(tmp_path, repo):
    # config errors surface as SweepError BEFORE the marker: the tier never ran
    conn = make_db(tmp_path / "w.db", repo)  # local identity: no GitHub origin
    with pytest.raises(prsweep.SweepError):
        prsweep.sweep_increment(conn, repo, "main", transport=FakeTransport([]), now=OBSERVED_AT)
    assert not _disclosed(conn)
    githubize(conn)
    with pytest.raises(prsweep.SweepError):  # no Target branch configured
        prsweep.sweep_increment(conn, repo, "", transport=FakeTransport([]), now=OBSERVED_AT)
    assert not _disclosed(conn)
    conn.close()


# ---------------------------------------------------------------------------
# operator skip/unskip: an unprocessable PR wedges every run until an operator
# marks it. The mark is ONE audit-only `operator-skip` row per PR (suspicious,
# never attribution, never touching the PR's other rows); the faces that may
# claim coverage advance past the marked PR explicitly, in processing order —
# the marker never jumps over earlier unprocessed PRs — and the operator
# commands themselves never move the marker.
# ---------------------------------------------------------------------------


def test_a_wedged_pr_stalls_the_run_until_it_is_marked_skipped(tmp_path, repo, monkeypatch):
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 3])
    land_squash_prs(repo, [2])  # PR 2 lands but is never walked: a permanent wedge
    rows = pr_list_rows(1, 2, 3)
    spy = PrSpy(monkeypatch)

    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(rows), now=OBSERVED_AT
    )
    assert res.stop == prsweep.STOP_WALK_STALE  # stalled AT the wedge
    assert res.processed == 1 and spy.log == [1] and prsweep.watermark(conn) == 1

    prsweep.skip_pr(conn, 2, "unmappable landing")
    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(rows), now=OBSERVED_AT
    )
    assert res2.stop is None and spy.log == [3]  # past the wedge: later PRs processed
    assert res2.processed == 1  # the skip itself is not a processed PR
    assert prsweep.watermark(conn) == 3  # the marker advanced past the marked PR


def test_unskip_restores_normal_processing(tmp_path, repo, monkeypatch):
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2])
    rows = pr_list_rows(1, 2)
    prsweep.skip_pr(conn, 2)
    spy = PrSpy(monkeypatch)

    full = prsweep.sweep_full(conn, repo, "main", transport=FakeTransport(rows), now=OBSERVED_AT)
    assert full.stop is None and spy.log == [1]  # full sweeps ignore marked rows too
    assert prsweep.watermark(conn) == 2  # advanced past the marked PR, in order
    assert [r[2] for r in pr_rows(conn, 2)] == [prsweep.RULE_OPERATOR_SKIP]  # audit row only

    prsweep.unskip_pr(conn, 2)
    assert pr_rows(conn, 2) == []  # unmark deletes ONLY the operator-skip row

    spy.log.clear()
    res = prsweep.sweep_full(conn, repo, "main", transport=FakeTransport(rows), now=OBSERVED_AT)
    assert res.stop is None and spy.log == [1, 2]  # normal processing restored
    assert rows_per_pr(conn) == {1: 3, 2: 3}  # the PR records normally again


def test_marking_a_pr_preserves_its_existing_evidence_rows(tmp_path, repo, monkeypatch):
    conn, shas = primed_sweep_db(tmp_path, repo, [1])
    prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1)), now=OBSERVED_AT
    )
    before = pr_rows(conn, 1)
    assert len(before) == 3

    prsweep.skip_pr(conn, 1, "operator decision")
    rows = pr_rows(conn, 1)
    assert [r for r in rows if r[2] != prsweep.RULE_OPERATOR_SKIP] == before  # untouched
    (skip,) = [r for r in rows if r[2] == prsweep.RULE_OPERATOR_SKIP]
    assert skip[0] is None and skip[3] == "operator decision" and skip[4] == "possible"
    assert skip[5] == "pr" and skip[7] == 1 and skip[8] is None and skip[9] is None

    prsweep.skip_pr(conn, 1)  # marking again replaces ONLY the operator-skip row
    rows = pr_rows(conn, 1)
    assert len(rows) == 4
    assert [r for r in rows if r[2] != prsweep.RULE_OPERATOR_SKIP] == before
    (skip,) = [r for r in rows if r[2] == prsweep.RULE_OPERATOR_SKIP]
    assert skip[3] == "operator skip"  # the raw falls back to the default reason


def test_a_skip_consumes_no_cap_and_creates_no_attribution(tmp_path, repo, monkeypatch):
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2, 3])
    prsweep.skip_pr(conn, 1)
    spy = PrSpy(monkeypatch)
    res = prsweep.sweep_increment(
        conn,
        repo,
        "main",
        transport=FakeTransport(pr_list_rows(1, 2, 3)),
        now=OBSERVED_AT,
        max_prs=1,
    )
    assert spy.log == [2]  # the skip cost no cap: PR 2 processed under cap 1
    assert res.processed == 1 and res.stop == prsweep.STOP_CAP
    assert prsweep.watermark(conn) == 2

    (skip,) = [r for r in pr_rows(conn) if r[2] == prsweep.RULE_OPERATOR_SKIP]
    assert skip[0] is None and skip[7] == 1  # NULL commit + suspicious: never attribution
    for sha in (shas[1], shas[2]):
        ev = aiattr.effective_evidence(conn, commit_id(conn, sha))
        assert all(c.rule != prsweep.RULE_OPERATOR_SKIP for c in ev)


def test_the_positive_bounded_face_skips_a_marked_pr_but_never_advances(
    tmp_path, repo, monkeypatch
):
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2, 3])
    prsweep.skip_pr(conn, 2)
    spy = PrSpy(monkeypatch)
    res = prsweep.sweep(
        conn, repo, "main", 1, transport=FakeTransport(pr_list_rows(1, 2, 3)), now=OBSERVED_AT
    )
    assert spy.log == [3]  # the marked PR 2 is skipped, PR 3 is recorded
    assert res.processed == 1
    assert prsweep.resume_key(conn) == (None, 0)  # a positive bound never advances
    assert rows_per_pr(conn) == {2: 1, 3: 3}  # PR 2: the audit row only


# ---------------------------------------------------------------------------
# the operator CLI faces: record/clear the skip, never move the marker
# ---------------------------------------------------------------------------


def test_cli_skip_pr_and_unskip_pr_record_and_clear_the_audit_row(tmp_path, repo, capsys):
    db = cli_sweep_db(tmp_path, repo, [1, 2])
    cli.main(["pr-sweep", "--db", str(db), "--skip-pr", "2"])
    out = capsys.readouterr().out
    assert "PR 2" in out and "marked to skip" in out

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM pr_evidence WHERE pr_number = 2").fetchall()
    assert [(r["rule"], r["raw"], r["verdict"], r["suspicious"], r["commit_id"]) for r in rows] == [
        (prsweep.RULE_OPERATOR_SKIP, "operator skip", "possible", 1, None)
    ]
    assert conn.execute("SELECT COUNT(*) c FROM pr_watermark").fetchone()["c"] == 0
    conn.close()  # the operator command never advances the marker

    cli.main(["pr-sweep", "--db", str(db), "--unskip-pr", "2"])
    out = capsys.readouterr().out
    assert "PR 2" in out and "skip cleared" in out
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) c FROM pr_evidence").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM pr_watermark").fetchone()["c"] == 0
    conn.close()


def test_cli_skip_pr_validates_the_number_and_stays_mutually_exclusive(tmp_path, repo):
    commit_files(repo, "init", {"a.txt": "a\n"})
    db = tmp_path / "cli.db"
    cli.main(["init", str(repo), "--db", str(db)])
    for args in (["--skip-pr", "0"], ["--skip-pr", "-1"], ["--unskip-pr", "0"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(["pr-sweep", "--db", str(db), *args])
        assert "error" in str(exc.value)  # config errors surface via SweepError
    with pytest.raises(SystemExit):
        cli.main(["pr-sweep", "--db", str(db), "--skip-pr", "1", "--full"])
    with pytest.raises(SystemExit):
        cli.main(["pr-sweep", "--db", str(db), "--skip-pr", "1", "--unskip-pr", "1"])
