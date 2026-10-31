"""Watermark-driven per-increment PR backfill (ticket #36, ADR-0007 clause 2,
re-keyed by merge date per the 2026-10-30 user ruling).

The bounded sweep becomes run machinery: the resume marker — the
(merged_at, number) tuple of the last INCORPORATED PR — rides the Artifact
with the recorded evidence rows (``pr_watermark``: ``last_merged_at`` +
``last_pr_number``, schema step 7). Merged PRs are processed OLDEST FIRST BY
MERGE DATE (ties by PR number ascending) and the marker advances per PR
recorded — every interruption leaves a contiguous processed prefix in
merge-date order and the next run resumes at the first PR whose (merged_at,
number) tuple is newer. Stop conditions (rate-limit exhaustion, per-run
processing cap, interruption) end a run cleanly with a visible notice, never
a failure. Closed-unmerged PRs never merge and are never candidates. The
full-history sweep walks the same merge-date order, sharing the marker,
merely further — its listing reaches the beginning of the PR numbering — and
later push runs continue after it. The PR number stays the per-row key (and
the ``--after-pr`` bounded face's bound); it is only the RESUME point that
moved to merge dates, because the number key lost out-of-order merges (a
still-open lower number sat below the number watermark forever).
"""

import io
import urllib.error
from pathlib import Path

import pytest

from coding_stats import cli, gitwalk, prsweep
from conftest import commit_files, make_db, run
from test_prsweep import (
    OBSERVED_AT,
    TRAILER_LINE,
    FakeTransport,
    commit_id,
    githubize,
    pr,
    pr_rows,
    push_head_ref,
    start_remote,
)

STOP_CAP = "cap"
STOP_RATE_LIMIT = "rate-limit"
STOP_INTERRUPTED = "interrupted"


def pr_list(numbers):
    """PR list rows (API order: descending number) with a body signal on
    every PR so each one records at least one evidence row."""
    return [
        pr(n, body="Generated with Codex.\n", head=f"claude/f{n}")
        for n in sorted(numbers, reverse=True)
    ]


def land_prs(repo, numbers):
    """One squash-merged PR per number: head ref pushed (then branch
    deleted), landing commit subject ``… (#n)``."""
    shas = {}
    for n in numbers:
        run(repo, "checkout", "-b", f"claude/f{n}")
        h = commit_files(repo, f"feat: {n}\n\n{TRAILER_LINE}\n", {f"f{n}.txt": f"{n}\n"})
        push_head_ref(repo, h, n)
        run(repo, "checkout", "main")
        run(repo, "branch", "-D", f"claude/f{n}")
        shas[n] = commit_files(repo, f"feat: work {n} (#{n})", {f"f{n}.txt": f"{n}\n"})
    return shas


def primed_db(tmp_path, repo, numbers):
    """Landed squash PRs + walked + GitHub-ized DB (the sweep's pre-state)."""
    start_remote(tmp_path, repo)
    shas = land_prs(repo, numbers)
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    return conn, shas


def row_numbers(conn):
    return {r[1] for r in pr_rows(conn)}


def assert_no_duplicate_rows(conn):
    rows = pr_rows(conn)
    assert len(rows) == len({(r[1], r[2], r[3]) for r in rows})


# ---------------------------------------------------------------------------
# oldest-first, per-PR watermark advance (acceptance criteria 1 + 2)
# ---------------------------------------------------------------------------


def test_increment_is_oldest_first_watermark_advances_per_pr_not_per_page(tmp_path, repo):
    conn, _ = primed_db(tmp_path, repo, [1, 2, 3, 4, 5])
    t = FakeTransport(pr_list([1, 2, 3, 4, 5]))

    res = prsweep.sweep_increment(conn, repo, "main", transport=t, now=OBSERVED_AT, max_prs=2)
    # one listing page covers all five PRs, yet the watermark moved per PR
    assert res.requests == 1
    assert res.stop == STOP_CAP and res.processed == 2
    assert res.watermark == 2 == prsweep.watermark(conn)
    assert row_numbers(conn) == {1, 2}  # ascending: the OLDEST two first

    res2 = prsweep.sweep_increment(conn, repo, "main", transport=t, now=OBSERVED_AT, max_prs=2)
    assert res2.stop == STOP_CAP and res2.processed == 2
    assert res2.watermark == 4 == prsweep.watermark(conn)

    res3 = prsweep.sweep_increment(conn, repo, "main", transport=t, now=OBSERVED_AT)
    assert res3.stop is None and res3.processed == 1 and res3.watermark == 5

    # disjoint ranges for consecutive runs: {1,2} / {3,4} / {5}
    assert row_numbers(conn) == {1, 2, 3, 4, 5}
    assert_no_duplicate_rows(conn)


def test_two_consecutive_increments_process_disjoint_ranges(tmp_path, repo):
    conn, _ = primed_db(tmp_path, repo, [1, 2, 3, 4])
    t = FakeTransport(pr_list([1, 2, 3, 4]))

    res1 = prsweep.sweep_increment(conn, repo, "main", transport=t, now=OBSERVED_AT, max_prs=2)
    first = row_numbers(conn)
    res2 = prsweep.sweep_increment(conn, repo, "main", transport=t, now=OBSERVED_AT)
    second = row_numbers(conn) - first

    assert res1.processed == 2 and res2.processed == 2
    assert first == {1, 2} and second == {3, 4}  # disjoint, second above first's watermark
    assert first.isdisjoint(second)
    assert res1.watermark == 2 < res2.watermark == 4


# ---------------------------------------------------------------------------
# interruption: contiguous prefix, resume at watermark + 1 (acceptance 1)
# ---------------------------------------------------------------------------


def test_interrupted_run_resumes_at_watermark_with_zero_skips_zero_duplicates(
    tmp_path, repo, monkeypatch
):
    conn, _ = primed_db(tmp_path, repo, [1, 2, 3, 4, 5])
    t = FakeTransport(pr_list([1, 2, 3, 4, 5]))

    orig = prsweep.fetch_head_ref

    def interrupting(r, n):
        if n == 3:
            raise KeyboardInterrupt
        return orig(r, n)

    monkeypatch.setattr(prsweep, "fetch_head_ref", interrupting)
    res = prsweep.sweep_increment(conn, repo, "main", transport=t, now=OBSERVED_AT)
    assert res.stop == STOP_INTERRUPTED
    assert res.processed == 2 and res.watermark == 2 == prsweep.watermark(conn)
    assert row_numbers(conn) == {1, 2}  # contiguous processed prefix

    # the interrupted PR's rows landed without its watermark move (the worst
    # case resume must redo): recorded, unadvanced
    cid = commit_id(conn, gitwalk.list_commits(repo, "main")[-1])
    prsweep.replace_pr_rows(
        conn,
        3,
        [prsweep.PrRow("tool-trailer", "stale:row", "yes", cid, False, "claude", None)],
        OBSERVED_AT,
    )
    conn.commit()
    assert prsweep.watermark(conn) == 2  # still unadvanced

    monkeypatch.setattr(prsweep, "fetch_head_ref", orig)
    res2 = prsweep.sweep_increment(conn, repo, "main", transport=t, now=OBSERVED_AT)
    assert res2.stop is None and res2.processed == 3  # 3, 4, 5: skipping nothing
    assert res2.watermark == 5 == prsweep.watermark(conn)
    # the single redone PR was replaced, never extended; watermark contiguous
    assert ("tool-trailer", "stale:row") not in {(r[2], r[3]) for r in pr_rows(conn)}
    assert row_numbers(conn) == {1, 2, 3, 4, 5}
    assert_no_duplicate_rows(conn)


# ---------------------------------------------------------------------------
# closed-unmerged / other-base PRs never incorporated, never advance
# ---------------------------------------------------------------------------


def test_closed_unmerged_and_other_base_never_advance_the_watermark(tmp_path, repo):
    conn, _ = primed_db(tmp_path, repo, [1, 4])
    run(repo, "checkout", "-b", "claude/g2")
    h = commit_files(repo, "feat: g2\n\n" + TRAILER_LINE + "\n", {"g2.txt": "g\n"})
    push_head_ref(repo, h, 2)  # closed-unmerged: head ref only, no landing
    run(repo, "checkout", "main")
    run(repo, "branch", "-D", "claude/g2")
    t = FakeTransport(
        [
            pr(4, body="Generated with Codex.\n", head="claude/f4"),
            pr(3, body="Generated with Codex.\n", head="claude/g2", base="develop"),
            pr(2, body="Generated with Codex.\n", head="claude/g2", merged=False),
            pr(1, body="Generated with Codex.\n", head="claude/f1"),
        ]
    )

    res = prsweep.sweep_increment(conn, repo, "main", transport=t, now=OBSERVED_AT, max_prs=1)
    assert res.processed == 1 and res.watermark == 1  # PR 1 only
    res2 = prsweep.sweep_increment(conn, repo, "main", transport=t, now=OBSERVED_AT)
    assert res2.processed == 1 and res2.watermark == 4
    # 2 (closed-unmerged) and 3 (other base) skipped: never incorporated, and
    # their numbers sit below the watermark the next merged PR produced
    assert row_numbers(conn) == {1, 4}
    assert prsweep.watermark(conn) == 4


# ---------------------------------------------------------------------------
# out-of-order merges: resume keyed by merge date, never the PR number
# ---------------------------------------------------------------------------


def test_out_of_order_merge_is_processed_when_its_merge_date_is_newer(tmp_path, repo):
    # PR 5 is still open while PR 7 merges: #7 is processed first (its merge
    # date is the only one), the marker becomes (T7, 7) — and when #5 merges
    # LATER its merge date is newer than the marker's, so it is processed
    # next: a still-open lower number is never lost under the merge-date key
    conn, _ = primed_db(tmp_path, repo, [7])
    t7 = "2026-10-20T12:00:00+00:00"
    t5 = "2026-10-25T12:00:00+00:00"
    t = FakeTransport(
        [
            pr(7, body="Generated with Codex.\n", head="claude/f7", merged_at=t7),
            pr(5, body="Generated with Codex.\n", head="claude/f5", state="open"),
        ]
    )
    res = prsweep.sweep_increment(conn, repo, "main", transport=t, now=OBSERVED_AT)
    assert res.processed == 1  # PR 7 only; PR 5 (open) is not merged: never a candidate
    assert row_numbers(conn) == {7}
    assert prsweep.resume_key(conn) == (t7, 7)
    assert prsweep.watermark(conn) == 7  # no clamp: the marker is PR 7's own tuple

    # later run: PR 5 has merged — its merge date is NEWER than the marker's
    land_prs(repo, [5])
    gitwalk.walk(repo, conn, "main")
    t2 = FakeTransport(
        [
            pr(7, body="Generated with Codex.\n", head="claude/f7", merged_at=t7),
            pr(5, body="Generated with Codex.\n", head="claude/f5", merged_at=t5),
        ]
    )
    res2 = prsweep.sweep_increment(conn, repo, "main", transport=t2, now=OBSERVED_AT)
    assert res2.processed == 1  # PR 5 exactly: PR 7 sits below the marker
    assert row_numbers(conn) == {5, 7}
    assert prsweep.resume_key(conn) == (t5, 5)
    assert prsweep.watermark(conn) == 5  # the number column: the marker PR's number
    assert_no_duplicate_rows(conn)


def test_equal_merge_dates_tie_break_by_pr_number(tmp_path, repo):
    # deterministic tie handling: same merge date, ordered and resumed by
    # PR number ascending — "equal merged_at + higher number than the last
    # incorporated PR counts as newer", one increment at a time
    conn, _ = primed_db(tmp_path, repo, [7, 8, 9])
    t = "2026-10-25T12:00:00+00:00"
    rows = [
        pr(n, body="Generated with Codex.\n", head=f"claude/f{n}", merged_at=t) for n in (9, 8, 7)
    ]
    seen = []
    for _ in range(3):
        res = prsweep.sweep_increment(
            conn, repo, "main", transport=FakeTransport(rows), now=OBSERVED_AT, max_prs=1
        )
        assert res.processed == 1
        seen.append(prsweep.resume_key(conn)[1])
    assert seen == [7, 8, 9]  # tie order: lowest number first, each the next candidate

    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(rows), now=OBSERVED_AT, max_prs=1
    )
    assert res.processed == 0  # nothing is newer than (t, 9)
    assert prsweep.resume_key(conn) == (t, 9)
    assert row_numbers(conn) == {7, 8, 9}
    assert_no_duplicate_rows(conn)


def test_interrupted_run_resumes_by_merge_date_with_zero_skips(tmp_path, repo, monkeypatch):
    # merge-date order is NOT number order here: #3 merged first, then #2,
    # then #1 — the marker tracks merge dates, so interruption and resume
    # follow that order and skip nothing
    conn, _ = primed_db(tmp_path, repo, [1, 2, 3])
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
    orig = prsweep.fetch_head_ref

    def interrupting(r, n):
        if n == 1:  # the third PR in merge-date order: dies mid-flight
            raise KeyboardInterrupt
        return orig(r, n)

    monkeypatch.setattr(prsweep, "fetch_head_ref", interrupting)
    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(rows), now=OBSERVED_AT
    )
    assert res.stop == STOP_INTERRUPTED  # clean stop, never a failure
    assert res.processed == 2  # merge-date order: #3 (10-21) then #2 (10-23)
    assert row_numbers(conn) == {2, 3}
    assert prsweep.resume_key(conn) == (t2, 2)  # the contiguous date-ordered prefix

    monkeypatch.setattr(prsweep, "fetch_head_ref", orig)
    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(rows), now=OBSERVED_AT
    )
    assert res2.stop is None and res2.processed == 1  # #1 exactly: skipping nothing
    assert row_numbers(conn) == {1, 2, 3}
    assert prsweep.resume_key(conn) == (t1, 1)
    assert_no_duplicate_rows(conn)


def test_closed_unmerged_prs_are_never_candidates(tmp_path, repo):
    # a closed-unmerged PR can never merge later, so it is never a candidate
    # and the marker passes its number when the next PR incorporates (the
    # number-key clamp of the review wave is gone — merge dates subsume it)
    conn, _ = primed_db(tmp_path, repo, [2])
    t = FakeTransport(
        [
            pr(2, body="Generated with Codex.\n", head="claude/f2"),
            pr(1, body="Generated with Codex.\n", head="claude/f1", merged=False),
        ]
    )
    res = prsweep.sweep_increment(conn, repo, "main", transport=t, now=OBSERVED_AT)
    assert res.processed == 1
    assert res.watermark == 2 == prsweep.watermark(conn)  # past closed PR 1


# ---------------------------------------------------------------------------
# a migrated number-key marker (no merge date) re-examines everything
# ---------------------------------------------------------------------------


def test_a_null_merge_date_marker_re_examines_every_merged_pr(tmp_path, repo):
    # a step-6-era marker claims a number but NO merge date: it cannot vouch
    # for any date, so every merged PR is a candidate (oldest first;
    # replace-not-duplicate keeps the redo clean) — completeness over the
    # number key's convenience
    conn, _ = primed_db(tmp_path, repo, [1, 2, 3])
    conn.execute(
        "INSERT INTO pr_watermark (repo_id, last_pr_number, last_merged_at)"
        " VALUES ((SELECT id FROM repositories ORDER BY id LIMIT 1), 2, NULL)"
    )
    conn.commit()
    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list([1, 2, 3])), now=OBSERVED_AT
    )
    assert res.processed == 3  # nothing is skipped under an unknown date
    assert row_numbers(conn) == {1, 2, 3}
    assert prsweep.resume_key(conn)[0] is not None  # the marker carries a date again
    assert_no_duplicate_rows(conn)


# ---------------------------------------------------------------------------
# rate-limit exhaustion: clean stop notice, automatic resume (acceptance 4)
# ---------------------------------------------------------------------------


class LimitedTransport:
    def __call__(self, url):
        raise prsweep.RateLimited("rate limit exhausted")


def test_rate_limit_ends_the_run_cleanly_and_the_next_run_resumes(tmp_path, repo):
    conn, _ = primed_db(tmp_path, repo, [1, 2, 3, 4, 5])

    res = prsweep.sweep_increment(conn, repo, "main", transport=LimitedTransport(), now=OBSERVED_AT)
    assert res.stop == STOP_RATE_LIMIT
    assert res.processed == 0 and res.watermark == 0 == prsweep.watermark(conn)

    res2 = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list([1, 2, 3, 4, 5])), now=OBSERVED_AT
    )
    assert res2.stop is None and res2.processed == 5  # resumed automatically
    assert row_numbers(conn) == {1, 2, 3, 4, 5}  # zero skipped
    assert_no_duplicate_rows(conn)


def test_permissions_403_is_a_forbidden_stop_never_a_rate_limit(tmp_path, repo):
    # "Resource not accessible by integration" is a PERMISSION failure, not
    # rate-limit exhaustion: it must never surface as "rate limit exhausted
    # ... the next run resumes" — it is a distinct stop naming the permission
    conn, _ = primed_db(tmp_path, repo, [1, 2])

    def denied(url):
        raise urllib.error.HTTPError(
            url, 403, "Resource not accessible by integration", {}, None
        )

    res = prsweep.sweep_increment(conn, repo, "main", transport=denied, now=OBSERVED_AT)
    assert res.stop == prsweep.STOP_FORBIDDEN  # distinct stop, never rate-limit
    assert res.stop != STOP_RATE_LIMIT
    assert res.processed == 0 and res.watermark == 0 == prsweep.watermark(conn)


def test_cli_forbidden_notice_names_the_permission_not_the_rate_limit(
    tmp_path, repo, monkeypatch, capsys
):
    db = cli_db(tmp_path, repo, [1])

    def denied(url):
        raise urllib.error.HTTPError(
            url, 403, "Resource not accessible by integration", {}, None
        )

    monkeypatch.setattr(prsweep, "default_transport", denied)
    cli.main(["pr-sweep", "--db", str(db)])  # a clean stop, never a failure exit
    out = capsys.readouterr().out
    assert "::notice::" in out
    assert "pull-requests: read" in out  # the notice names the permission
    assert "rate limit exhausted" not in out
    assert "next run resumes" not in out


def test_default_transport_only_raises_rate_limited_for_genuine_rate_limits(monkeypatch):
    def failing(code, headers):
        def go(req, **kw):
            raise urllib.error.HTTPError(req.full_url, code, "limited", headers, io.BytesIO(b""))

        return go

    # genuine exhaustion: 403 with the rate budget at zero, and 429
    monkeypatch.setattr(
        urllib.request, "urlopen", failing(403, {"X-RateLimit-Remaining": "0"})
    )
    with pytest.raises(prsweep.RateLimited):
        prsweep.default_transport("https://api.github.com/x")
    monkeypatch.setattr(urllib.request, "urlopen", failing(429, {}))
    with pytest.raises(prsweep.RateLimited):
        prsweep.default_transport("https://api.github.com/x")
    # a permissions 403 is NOT laundered into a clean rate-limit stop
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        failing(403, {"X-RateLimit-Remaining": "99"}),
    )
    with pytest.raises(urllib.error.HTTPError):
        prsweep.default_transport("https://api.github.com/x")


def test_default_transport_uses_a_request_timeout(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"[]"

    seen: dict = {}

    def go(req, **kw):
        seen.update(kw)
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", go)
    assert prsweep.default_transport("https://api.github.com/x") == []
    assert seen.get("timeout") == 30  # bounded: a hung API never hangs the sweep


def test_head_fetch_never_prompts_interactively(monkeypatch):
    # GIT_TERMINAL_PROMPT=0 + closed stdin: a sweep fetch can never hang
    # asking for credentials (and nothing interactive is accepted)
    import subprocess as sp

    seen = []

    def fake_run(cmd, **kw):
        seen.append((cmd, kw))
        return sp.CompletedProcess(cmd, 1, "", "fatal: nope")

    monkeypatch.setattr(sp, "run", fake_run)
    with pytest.raises(prsweep.FetchUnavailable):  # transient, not "vanished"
        prsweep.fetch_head_ref(Path("/tmp/opencode/none"), 5)
    (cmd, kw) = seen[0]
    assert cmd[3] == "fetch"
    assert kw.get("env", {}).get("GIT_TERMINAL_PROMPT") == "0"
    assert kw.get("stdin") is sp.DEVNULL


# ---------------------------------------------------------------------------
# full-history sweep: shares the watermark, later push runs continue after it
# ---------------------------------------------------------------------------


def test_full_history_sweep_reaches_the_beginning_and_push_runs_continue_after_it(tmp_path, repo):
    conn, _ = primed_db(tmp_path, repo, [1, 2, 3, 4])
    t = FakeTransport(pr_list([1, 2, 3, 4]))
    # adoption through an explicit bound: only 3, 4 recorded — and the
    # watermark never claims past the gap below it (zero skipped stays true)
    res = prsweep.sweep(conn, repo, "main", 2, transport=t, now=OBSERVED_AT)
    assert res.processed == 2 and res.watermark == 0 == prsweep.watermark(conn)
    assert row_numbers(conn) == {3, 4}

    # the operator's full-history sweep: same oldest-first walk, shared
    # watermark, merely walked further — the listing reaches the beginning
    res = prsweep.sweep_full(conn, repo, "main", transport=t, now=OBSERVED_AT)
    assert res.stop is None and res.processed == 4  # 1..4 re-swept from the beginning
    assert row_numbers(conn) == {1, 2, 3, 4}
    assert prsweep.watermark(conn) == 4  # contiguous now: the gap filled, claimed
    assert_no_duplicate_rows(conn)

    # a later push run continues after it
    land_prs(repo, [5])
    gitwalk.walk(repo, conn, "main")
    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list([1, 2, 3, 4, 5])), now=OBSERVED_AT
    )
    assert res.processed == 1 and res.watermark == 5
    assert row_numbers(conn) == {1, 2, 3, 4, 5}


def test_full_history_sweep_is_oldest_first_and_honors_the_cap(tmp_path, repo):
    conn, _ = primed_db(tmp_path, repo, [1, 2, 3, 4])
    t = FakeTransport(pr_list([1, 2, 3, 4]))
    prsweep.sweep(conn, repo, "main", 2, transport=t, now=OBSERVED_AT)
    assert row_numbers(conn) == {3, 4}

    res = prsweep.sweep_full(conn, repo, "main", transport=t, now=OBSERVED_AT, max_prs=2)
    assert res.stop == STOP_CAP and res.processed == 2
    assert row_numbers(conn) == {1, 2, 3, 4}  # oldest first: 1 and 2 filled in
    assert prsweep.watermark(conn) == 2  # contiguous claim over 1..2 only


# ---------------------------------------------------------------------------
# CLI face (the increment mode #37 wires into the update pipeline)
# ---------------------------------------------------------------------------


def cli_db(tmp_path, repo, numbers):
    start_remote(tmp_path, repo)
    land_prs(repo, numbers)
    db = tmp_path / "cli.db"
    cli.main(["init", str(repo), "--db", str(db)])
    cli.main(["walk", "--db", str(db)])
    import sqlite3

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    githubize(conn)
    conn.close()
    return db


def read_watermark(db):
    import sqlite3

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    wm = prsweep.watermark(conn)
    conn.close()
    return wm


def test_cli_increment_face_is_watermark_driven_with_visible_stop_notice(
    tmp_path, repo, monkeypatch, capsys
):
    db = cli_db(tmp_path, repo, [1, 2])
    t = FakeTransport(pr_list([1, 2]))
    monkeypatch.setattr(prsweep, "default_transport", lambda url: t(url))

    cli.main(["pr-sweep", "--db", str(db), "--max-prs", "1"])
    out = capsys.readouterr().out
    assert "::notice::" in out and STOP_CAP in out  # visible stop notice, exit 0
    assert read_watermark(db) == 1

    cli.main(["pr-sweep", "--db", str(db)])  # the next run resumes automatically
    out = capsys.readouterr().out
    assert "1 PRs recorded" in out and "watermark 2" in out  # only PR 2: resumed above 1
    assert read_watermark(db) == 2


def test_cli_rate_limit_stop_notice_is_visible_and_exit_zero(tmp_path, repo, monkeypatch, capsys):
    db = cli_db(tmp_path, repo, [1, 2])
    monkeypatch.setattr(prsweep, "default_transport", lambda url: LimitedTransport()(url))
    cli.main(["pr-sweep", "--db", str(db)])  # no failure exit
    out = capsys.readouterr().out
    assert "::notice::" in out and "rate limit" in out
    assert read_watermark(db) == 0


def test_cli_full_flag_re_sweeps_from_the_beginning(tmp_path, repo, monkeypatch, capsys):
    db = cli_db(tmp_path, repo, [1, 2])
    t = FakeTransport(pr_list([1, 2]))
    monkeypatch.setattr(prsweep, "default_transport", lambda url: t(url))

    cli.main(["pr-sweep", "--db", str(db), "--after-pr", "1"])  # adoption bound
    assert read_watermark(db) == 0  # the gap below the bound is never claimed
    conn_rows = _cli_rows(db)
    assert {r[0] for r in conn_rows} == {2}

    cli.main(["pr-sweep", "--db", str(db), "--full"])  # operator's full-history sweep
    out = capsys.readouterr().out
    assert "2 PRs recorded" in out
    assert {r[0] for r in _cli_rows(db)} == {1, 2}
    assert read_watermark(db) == 2  # shared watermark: contiguous now


def _cli_rows(db):
    import sqlite3

    conn = sqlite3.connect(db)
    out = [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT pr_number, rule, raw FROM pr_evidence ORDER BY pr_number, rule, raw"
    )]
    conn.close()
    return out
