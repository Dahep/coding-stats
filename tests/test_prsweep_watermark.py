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
claim invariant, replace-not-duplicate on resume); the merge-date marker's
own semantics live in test_pr_watermark.py. Builders live in conftest.py.
"""

import urllib.error

import pytest

from coding_stats import cli, gitwalk, prsweep
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


def test_interrupted_run_resumes_at_the_first_pr_above_the_watermark(
    tmp_path, repo, monkeypatch
):
    conn, shas = primed_sweep_db(tmp_path, repo, [1, 2, 3, 4, 5])
    spy = PrSpy(monkeypatch)
    spy.abort_after = 2  # the third PR's processing dies mid-flight

    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1, 2, 3, 4, 5)), now=OBSERVED_AT
    )
    assert res.stop == prsweep.STOP_INTERRUPTED  # clean stop, never a failure
    assert res.processed == 2 and spy.log == [1, 2]
    assert res.watermark == 2 == prsweep.watermark(conn)  # contiguous prefix
    assert rows_per_pr(conn) == {1: 3, 2: 3}

    spy.abort_after = None
    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1, 2, 3, 4, 5)), now=OBSERVED_AT
    )
    assert res2.stop is None and res2.processed == 3
    assert spy.log == [3, 4, 5]  # resumes at the first PR past the marker
    assert prsweep.watermark(conn) == 5
    assert rows_per_pr(conn) == {1: 3, 2: 3, 3: 3, 4: 3, 5: 3}  # zero skipped
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
    assert res.stop == prsweep.STOP_RATE_LIMIT  # a clean stop, never a failure
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
        [prsweep.PrRow(
            "tool-trailer", "stale:row", "yes", commit_id(conn, shas[2]), False, "x", None
        )],
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
    row = migrated.execute(
        "SELECT last_pr_number, last_merged_at FROM pr_watermark"
    ).fetchone()
    assert (row["last_pr_number"], row["last_merged_at"]) == (7, None)  # preserved
    migrated.close()


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
# CLI face: the increment mode (the one #37 wires into the update pipeline)
# ---------------------------------------------------------------------------


def test_cli_pr_sweep_default_is_the_increment_face(tmp_path, repo, monkeypatch, capsys):
    db = cli_sweep_db(tmp_path, repo, [1, 2, 3])
    monkeypatch.setattr(
        prsweep, "default_transport", lambda url: FakeTransport(pr_list_rows(1, 2, 3))(url)
    )
    cli.main(["pr-sweep", "--db", str(db)])
    out = capsys.readouterr().out
    assert "pr-sweep" in out and "3 PRs" in out and "watermark 3" in out

    land_squash_prs(repo, [4, 5])  # new PRs land; the default face resumes
    cli.main(["walk", "--db", str(db)])  # every run walks before the sweep
    monkeypatch.setattr(
        prsweep, "default_transport", lambda url: FakeTransport(pr_list_rows(1, 2, 3, 4, 5))(url)
    )
    cli.main(["pr-sweep", "--db", str(db)])
    out = capsys.readouterr().out
    assert "2 PRs" in out and "watermark 5" in out


def test_cli_pr_sweep_stop_notice_is_never_a_failure(tmp_path, repo, monkeypatch, capsys):
    db = cli_sweep_db(tmp_path, repo, [1, 2, 3])
    monkeypatch.setattr(
        prsweep, "default_transport", lambda url: FakeTransport(pr_list_rows(1, 2, 3))(url)
    )
    cli.main(["pr-sweep", "--db", str(db), "--max-prs", "2"])  # clean stop, exit 0
    out = capsys.readouterr().out
    assert "2 PRs" in out
    assert "pr-sweep stopped" in out and "cap" in out
    assert "next run resumes" in out


def test_cli_pr_sweep_rejects_full_with_after_pr(tmp_path, repo):
    commit_files(repo, "init", {"a.txt": "a\n"})  # init identifies the branch via HEAD
    db = tmp_path / "cli.db"
    cli.main(["init", str(repo), "--db", str(db)])
    with pytest.raises(SystemExit):
        cli.main(["pr-sweep", "--db", str(db), "--full", "--after-pr", "0"])
