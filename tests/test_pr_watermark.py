"""Watermark-driven per-increment PR backfill: the merge-date marker's own
semantics (ticket #36, ADR-0007 clause 2, re-keyed by merge date per the
2026-10-30 user ruling), plus the transport/HTTP stop classification and
the CLI notice faces.

The resume marker — the (merged_at, number) tuple of the last INCORPORATED
PR — rides the Artifact with the recorded evidence rows (``pr_watermark``:
``last_merged_at`` + ``last_pr_number``, schema step 7). Merged PRs are
processed OLDEST FIRST BY MERGE DATE (ties by PR number ascending) and the
marker advances per PR recorded. Out-of-order merges are never lost under
this key (the number key lost a still-open lower number forever); a marker
with a NULL merge date claims no date and re-examines the history; a
closed-unmerged PR is never a candidate and its number is passed by the
next merged PR's marker. Stop conditions end a run cleanly with a visible
notice, never a failure — a permissions 403 is a distinct forbidden stop,
never laundered into "rate limit exhausted".

The run-machinery scenarios (interrupt/resume, disjoint increments, clean
stops, the shared full-history watermark, the contiguous-claim invariant)
live in test_prsweep_watermark.py. Builders live in conftest.py.
"""

import io
import urllib.error
from pathlib import Path

import pytest

from coding_stats import cli, gitwalk, prsweep
from conftest import (
    OBSERVED_AT,
    FakeTransport,
    cli_sweep_db,
    land_squash_prs,
    pr,
    pr_list_rows,
    pr_rows,
    primed_sweep_db,
)

STOP_RATE_LIMIT = "rate-limit"
STOP_INTERRUPTED = "interrupted"


def row_numbers(conn):
    return {r[1] for r in pr_rows(conn)}


def assert_no_duplicate_rows(conn):
    rows = pr_rows(conn)
    assert len(rows) == len({(r[1], r[2], r[3]) for r in rows})


class LimitedTransport:
    def __call__(self, url):
        raise prsweep.RateLimited("rate limit exhausted")


# ---------------------------------------------------------------------------
# out-of-order merges: resume keyed by merge date, never the PR number
# ---------------------------------------------------------------------------


def test_out_of_order_merge_is_processed_when_its_merge_date_is_newer(tmp_path, repo):
    # PR 5 is still open while PR 7 merges: #7 is processed first (its merge
    # date is the only one), the marker becomes (T7, 7) — and when #5 merges
    # LATER its merge date is newer than the marker's, so it is processed
    # next: a still-open lower number is never lost under the merge-date key
    conn, _ = primed_sweep_db(tmp_path, repo, [7])
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
    land_squash_prs(repo, [5])
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
    conn, _ = primed_sweep_db(tmp_path, repo, [7, 8, 9])
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
    conn, _ = primed_sweep_db(tmp_path, repo, [1, 2, 3])
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


# ---------------------------------------------------------------------------
# a migrated number-key marker (no merge date) re-examines everything
# ---------------------------------------------------------------------------


def test_a_null_merge_date_marker_re_examines_every_merged_pr(tmp_path, repo):
    # a step-6-era marker claims a number but NO merge date: it cannot vouch
    # for any date, so every merged PR is a candidate (oldest first;
    # replace-not-duplicate keeps the redo clean) — completeness over the
    # number key's convenience
    conn, _ = primed_sweep_db(tmp_path, repo, [1, 2, 3])
    conn.execute(
        "INSERT INTO pr_watermark (repo_id, last_pr_number, last_merged_at)"
        " VALUES ((SELECT id FROM repositories ORDER BY id LIMIT 1), 2, NULL)"
    )
    conn.commit()
    res = prsweep.sweep_increment(
        conn, repo, "main", transport=FakeTransport(pr_list_rows(1, 2, 3)), now=OBSERVED_AT
    )
    assert res.processed == 3  # nothing is skipped under an unknown date
    assert row_numbers(conn) == {1, 2, 3}
    assert prsweep.resume_key(conn)[0] is not None  # the marker carries a date again
    assert_no_duplicate_rows(conn)


# ---------------------------------------------------------------------------
# stop classification: a permissions failure is a forbidden stop, never a
# rate limit — and the transport only raises RateLimited for genuine ones
# ---------------------------------------------------------------------------


def test_permissions_403_is_a_forbidden_stop_never_a_rate_limit(tmp_path, repo):
    # "Resource not accessible by integration" is a PERMISSION failure, not
    # rate-limit exhaustion: it must never surface as "rate limit exhausted
    # ... the next run resumes" — it is a distinct stop naming the permission
    conn, _ = primed_sweep_db(tmp_path, repo, [1, 2])

    def denied(url):
        raise urllib.error.HTTPError(
            url, 403, "Resource not accessible by integration", {}, None
        )

    res = prsweep.sweep_increment(conn, repo, "main", transport=denied, now=OBSERVED_AT)
    assert res.stop == prsweep.STOP_FORBIDDEN  # distinct stop, never rate-limit
    assert res.stop != STOP_RATE_LIMIT
    assert res.processed == 0 and res.watermark == 0 == prsweep.watermark(conn)


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
# CLI faces: visible stop notices (exit 0), forbidden names the permission,
# and the --full / --after-pr faces share the watermark
# ---------------------------------------------------------------------------


def read_watermark(db):
    import sqlite3

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    wm = prsweep.watermark(conn)
    conn.close()
    return wm


def _cli_rows(db):
    import sqlite3

    conn = sqlite3.connect(db)
    out = [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT pr_number, rule, raw FROM pr_evidence ORDER BY pr_number, rule, raw"
    )]
    conn.close()
    return out


def test_cli_forbidden_notice_names_the_permission_not_the_rate_limit(
    tmp_path, repo, monkeypatch, capsys
):
    db = cli_sweep_db(tmp_path, repo, [1])

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


def test_cli_rate_limit_stop_notice_is_visible_and_exit_zero(tmp_path, repo, monkeypatch, capsys):
    db = cli_sweep_db(tmp_path, repo, [1, 2])
    monkeypatch.setattr(prsweep, "default_transport", lambda url: LimitedTransport()(url))
    cli.main(["pr-sweep", "--db", str(db)])  # no failure exit
    out = capsys.readouterr().out
    assert "::notice::" in out and "rate limit" in out
    assert read_watermark(db) == 0


def test_cli_full_flag_re_sweeps_from_the_beginning(tmp_path, repo, monkeypatch, capsys):
    db = cli_sweep_db(tmp_path, repo, [1, 2])
    t = FakeTransport(pr_list_rows(1, 2))
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
