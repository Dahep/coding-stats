"""Watermark resume + oldest-first per-increment PR backfill (ticket #36,
ADR-0007 clause 2).

The bounded sweep becomes watermark-driven run machinery: the watermark — the
last INCORPORATED PR number — advances after each PR's evidence is recorded
(never per page), so every interruption leaves a contiguous processed prefix.
Resume starts at the first PR above the watermark: zero skipped PRs, zero
duplicated rows (replace-not-duplicate applies on resume). Full-history mode
walks the whole numbering sequence the same oldest-first way and shares the
watermark; later push runs continue after it. Stop conditions (rate limit,
per-run cap, interruption) end a run cleanly with a notice, never a failure —
the next run resumes automatically. Closed-unmerged and other-base PRs are
never incorporated: their numbers sit below whatever watermark the next
merged PR sets. Keyed by PR number, never merge date.
"""

import sqlite3
import urllib.error

import pytest

from coding_stats import cli, gitwalk, prsweep
from coding_stats.db import connect
from conftest import commit_files, make_db, run
from test_prsweep import (
    FakeTransport,
    commit_id,
    githubize,
    pr,
    pr_rows,
    push_head_ref,
    start_remote,
)

OBSERVED_AT = "2026-10-30T00:00:00+00:00"
TRAILER_VALUE = "Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
TRAILER_LINE = f"Co-authored-by: {TRAILER_VALUE}"


def make_squash_prs(repo, ks):
    """One squash-merged PR per number in ks, oldest first: a trailer-carrying
    head pushed to refs/pull/N/head, then the (#N) landing on main (the same
    patch — the head-parentage check passes)."""
    shas = {}
    for k in ks:
        run(repo, "checkout", "-b", f"claude/f{k}")
        h = commit_files(repo, f"feat: core {k}\n\n{TRAILER_LINE}\n", {f"f{k}.txt": f"{k}\n"})
        push_head_ref(repo, h, k)
        run(repo, "checkout", "main")
        run(repo, "branch", "-D", f"claude/f{k}")
        shas[k] = commit_files(repo, f"feat: work {k} (#{k})", {f"f{k}.txt": f"{k}\n"})
    return shas


def pulls(*ks):
    """The list endpoint's rows for the given PR numbers (API order: desc)."""
    return [
        pr(k, body="Generated with Codex.\n", head=f"claude/f{k}") for k in sorted(ks, reverse=True)
    ]


def built(tmp_path, repo, ks):
    """Walked + GitHub-identified DB over ks squash PRs (1 remote fetch per
    PR, list pages served by the injected transport)."""
    start_remote(tmp_path, repo)
    shas = make_squash_prs(repo, ks)
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    return conn, shas


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
    conn, shas = built(tmp_path, repo, [1, 2, 3, 4, 5])
    spy = PrSpy(monkeypatch)
    spy.abort_after = 2  # the third PR's processing dies mid-flight

    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pulls(1, 2, 3, 4, 5)), now=OBSERVED_AT
    )
    assert res.stop == prsweep.STOP_INTERRUPTED  # clean stop, never a failure
    assert res.processed == 2 and spy.log == [1, 2]
    assert res.watermark == 2 == prsweep.watermark(conn)  # contiguous prefix
    assert rows_per_pr(conn) == {1: 3, 2: 3}

    spy.abort_after = None
    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pulls(1, 2, 3, 4, 5)), now=OBSERVED_AT
    )
    assert res2.stop is None and res2.processed == 3
    assert spy.log == [3, 4, 5]  # resumes at the first PR above the watermark
    assert prsweep.watermark(conn) == 5
    assert rows_per_pr(conn) == {1: 3, 2: 3, 3: 3, 4: 3, 5: 3}  # zero skipped
    assert duplicate_rows(conn) == []  # zero duplicated evidence rows


# ---------------------------------------------------------------------------
# acceptance criterion 2: consecutive push-run increments are disjoint
# ---------------------------------------------------------------------------


def test_two_consecutive_increments_process_disjoint_pr_ranges(tmp_path, repo, monkeypatch):
    conn, shas = built(tmp_path, repo, [1, 2, 3])
    spy = PrSpy(monkeypatch)
    res1 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pulls(1, 2, 3)), now=OBSERVED_AT
    )
    assert spy.log == [1, 2, 3] and res1.watermark == 3

    make_squash_prs(repo, [4, 5])  # two new PRs land between the runs
    gitwalk.walk(repo, conn, "main")  # every update run walks before the sweep
    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pulls(1, 2, 3, 4, 5)), now=OBSERVED_AT
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
    conn, shas = built(tmp_path, repo, [1, 2, 3])
    spy = PrSpy(monkeypatch)
    res0 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pulls(1, 2, 3)), now=OBSERVED_AT
    )
    assert res0.watermark == 3

    make_squash_prs(repo, [4, 5])  # new PRs arrive; the API budget is gone
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
        conn, repo, "main", transport=FakeTransport(pulls(1, 2, 3, 4, 5)), now=OBSERVED_AT
    )
    assert res2.stop is None and spy.log == [4, 5] and res2.watermark == 5


# ---------------------------------------------------------------------------
# the watermark never advances past a PR that cannot be incorporated
# ---------------------------------------------------------------------------


def test_pr_merged_after_the_walk_stops_the_run_at_that_pr(tmp_path, repo, monkeypatch):
    # PR 3 merged AFTER the last walk: its landing sha is not in `commits`,
    # so the run stops there cleanly — never records suspicious rows and
    # never claims past it; the next run (after a newer walk) redoes it
    conn, shas = built(tmp_path, repo, [1, 2])
    spy = PrSpy(monkeypatch)
    shas3 = make_squash_prs(repo, [3])  # merged after the walk — nothing walked yet
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
    conn, shas = built(tmp_path, repo, [1])
    spy = PrSpy(monkeypatch)
    make_squash_prs(repo, [2])  # merged after the walk
    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pulls(2)), now=OBSERVED_AT
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
    conn, shas = built(tmp_path, repo, [1, 2])
    spy = PrSpy(monkeypatch)
    res0 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pulls(1)), now=OBSERVED_AT
    )
    assert res0.processed == 1 and prsweep.watermark(conn) == 1

    run(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))  # unreachable
    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pulls(1, 2)), now=OBSERVED_AT
    )
    assert res.stop == prsweep.STOP_FETCH  # distinct stop: transient fetch failure
    assert res.processed == 0 and prsweep.watermark(conn) == 1  # never past 2
    assert pr_rows(conn, 2) == []  # nothing recorded, nothing suspicious

    run(repo, "remote", "set-url", "origin", str(tmp_path / "remote.git"))  # back
    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pulls(1, 2)), now=OBSERVED_AT
    )
    assert res2.stop is None and spy.log == [2] and prsweep.watermark(conn) == 2


# ---------------------------------------------------------------------------
# stop conditions: the per-run processing cap
# ---------------------------------------------------------------------------


def test_per_run_cap_stops_cleanly_and_the_next_run_continues(tmp_path, repo, monkeypatch):
    conn, shas = built(tmp_path, repo, [1, 2, 3, 4, 5])
    spy = PrSpy(monkeypatch)
    res = prsweep.sweep_increment(
        conn,
        repo,
        "main",
        transport=FakeTransport(pulls(1, 2, 3, 4, 5)),
        now=OBSERVED_AT,
        max_prs=2,
    )
    assert res.stop == prsweep.STOP_CAP and spy.log == [1, 2] and res.watermark == 2

    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn,
        repo,
        "main",
        transport=FakeTransport(pulls(1, 2, 3, 4, 5)),
        now=OBSERVED_AT,
        max_prs=2,
    )
    assert res2.stop == prsweep.STOP_CAP and spy.log == [3, 4] and res2.watermark == 4

    spy.log.clear()
    res3 = prsweep.sweep_increment(
        conn,
        repo,
        "main",
        transport=FakeTransport(pulls(1, 2, 3, 4, 5)),
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
    conn, shas = built(tmp_path, repo, [1, 2, 3])
    spy = PrSpy(monkeypatch)
    prsweep.sweep_increment(
        conn,
        repo,
        "main",
        transport=FakeTransport(pulls(1, 2, 3)),
        now=OBSERVED_AT,
        max_prs=2,
    )
    assert prsweep.watermark(conn) == 2  # a capped increment left 3 behind

    spy.log.clear()
    full = prsweep.sweep_full(
        conn, repo, "main", transport=FakeTransport(pulls(1, 2, 3)), now=OBSERVED_AT
    )
    assert spy.log == [1, 2, 3]  # the whole numbering sequence, oldest-first
    assert full.stop is None and full.watermark == 3
    # the re-walk replaces rows in place: nothing duplicated
    assert rows_per_pr(conn) == {1: 3, 2: 3, 3: 3} and duplicate_rows(conn) == []

    make_squash_prs(repo, [4])  # later push runs continue after it
    gitwalk.walk(repo, conn, "main")
    spy.log.clear()
    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pulls(1, 2, 3, 4)), now=OBSERVED_AT
    )
    assert spy.log == [4] and res.watermark == 4


# ---------------------------------------------------------------------------
# skipped PR numbers: never incorporated, below the next merged PR's watermark
# ---------------------------------------------------------------------------


def test_unmerged_and_other_base_prs_are_never_incorporated(tmp_path, repo, monkeypatch):
    conn, shas = built(tmp_path, repo, [1, 3])  # PR 2 closed-unmerged, PR 4 other-base
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
    conn, shas = built(tmp_path, repo, [1, 2, 3])
    spy = PrSpy(monkeypatch)
    res = prsweep.sweep(
        conn, repo, "main", 2, transport=FakeTransport(pulls(1, 2, 3)), now=OBSERVED_AT
    )
    assert spy.log == [3]
    assert res.watermark == 0  # the gap is never claimed: zero skipped stays true

    spy.log.clear()
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pulls(1, 2, 3)), now=OBSERVED_AT
    )
    assert spy.log == [1, 2, 3]  # earlier records are re-replaced, never duplicated
    assert res2.watermark == 3
    assert rows_per_pr(conn) == {1: 3, 2: 3, 3: 3} and duplicate_rows(conn) == []


def test_half_recorded_pr_is_replaced_not_extended_on_resume(tmp_path, repo, monkeypatch):
    conn, shas = built(tmp_path, repo, [1, 2])
    spy = PrSpy(monkeypatch)
    prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pulls(1)), now=OBSERVED_AT
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

    spy.log.clear()
    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pulls(1, 2)), now=OBSERVED_AT
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
    conn.commit()
    conn.close()
    migrated = connect(tmp_path / "w.db")
    cols = [str(r["name"]) for r in migrated.execute("PRAGMA table_info(pr_watermark)")]
    assert cols == ["repo_id", "last_pr_number"]
    migrated.close()


# ---------------------------------------------------------------------------
# CLI face: the increment mode (the one #37 wires into the update pipeline)
# ---------------------------------------------------------------------------


def cli_setup(tmp_path, repo, ks):
    start_remote(tmp_path, repo)
    make_squash_prs(repo, ks)
    db = tmp_path / "cli.db"
    cli.main(["init", str(repo), "--db", str(db)])
    cli.main(["walk", "--db", str(db)])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    githubize(conn)
    conn.close()
    return db


def test_cli_pr_sweep_default_is_the_increment_face(tmp_path, repo, monkeypatch, capsys):
    db = cli_setup(tmp_path, repo, [1, 2, 3])
    monkeypatch.setattr(
        prsweep, "default_transport", lambda url: FakeTransport(pulls(1, 2, 3))(url)
    )
    cli.main(["pr-sweep", "--db", str(db)])
    out = capsys.readouterr().out
    assert "pr-sweep" in out and "3 PRs" in out and "watermark 3" in out

    make_squash_prs(repo, [4, 5])  # new PRs land; the default face resumes
    cli.main(["walk", "--db", str(db)])  # every run walks before the sweep
    monkeypatch.setattr(
        prsweep, "default_transport", lambda url: FakeTransport(pulls(1, 2, 3, 4, 5))(url)
    )
    cli.main(["pr-sweep", "--db", str(db)])
    out = capsys.readouterr().out
    assert "2 PRs" in out and "watermark 5" in out


def test_cli_pr_sweep_full_flag_walks_the_whole_history(tmp_path, repo, monkeypatch, capsys):
    db = cli_setup(tmp_path, repo, [1, 2, 3])
    monkeypatch.setattr(
        prsweep, "default_transport", lambda url: FakeTransport(pulls(1, 2, 3))(url)
    )
    cli.main(["pr-sweep", "--db", str(db), "--full"])
    out = capsys.readouterr().out
    assert "3 PRs" in out and "watermark 3" in out


def test_cli_pr_sweep_stop_notice_is_never_a_failure(tmp_path, repo, monkeypatch, capsys):
    db = cli_setup(tmp_path, repo, [1, 2, 3])
    monkeypatch.setattr(
        prsweep, "default_transport", lambda url: FakeTransport(pulls(1, 2, 3))(url)
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
