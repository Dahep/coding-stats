"""Metrics tests and CLI wiring against a fixture repo."""

import json

import pytest

from coding_stats.cli import main
from coding_stats.gitwalk import walk
from coding_stats.metrics import snapshot, snapshot_to_json
from conftest import add_feature, make_db


@pytest.fixture()
def classified(tmp_path, repo, shas):
    conn = make_db(tmp_path / "m.db", repo)
    walk(repo, conn, "main")
    for key, verdict in (
        ("c1", "feature"),
        ("c2", "feature"),
        ("c3", "fix"),
        ("c4", "refactor"),
        ("c5", "fix"),
        ("c6", "feature"),
        ("cr", "feature"),
        ("c7", "feature"),
        ("c8", "cleanup"),
    ):
        conn.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
            " classified_at) VALUES ((SELECT id FROM commits WHERE sha = ?), ?, '', '', 'm','x')",
            (shas[key], verdict),
        )
    fid = add_feature(conn, "alpha", "x", "2026-01-01")
    conn.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role)"
        " VALUES ((SELECT id FROM commits WHERE sha = ?), ?, 'defines')",
        (shas["c1"], fid),
    )
    conn.commit()
    yield conn


def test_snapshot_counts(classified):
    snap = snapshot(classified)
    assert snap.total_commits == 9
    assert snap.verdict_counts == {"feature": 5, "fix": 2, "refactor": 1, "cleanup": 1}
    assert snap.total_features == 1
    # Fix events are REACHES (fixes_features edges), not fix-verdict commits:
    # before the lineage closure runs there are no edges, so the Fix event
    # total is 0 while 2 fix commits are recorded. (Re-anchored from
    # total_fixes == 2, which counted the raw commits.)
    assert snap.fix_event_total == 0
    assert snap.uncovered_fix_commits == 2  # audit view: raw commit counts


def test_snapshot_json_shape(classified):
    data = json.loads(snapshot_to_json(snapshot(classified)))
    assert data["features"] == 1
    assert data["fix_events"] == 0
    assert set(data) >= {"total_commits", "verdicts", "features_per_year", "fixes_per_feature"}


def test_populate_fix_rows_then_metrics(classified, repo, shas):
    from coding_stats.derive import annotate_all, close_lineage

    n = annotate_all(repo, classified)
    assert n == 2
    close_lineage(classified)
    snap = snapshot(classified)
    # c3 -> alpha (direct) + c5 -> alpha (via c3): two reaches, two Fix events
    assert snap.fix_event_total == 2
    assert snap.uncovered_fix_commits == 0
    assert snap.fixes_per_feature == 2.0
    assert snap.top_features_by_fixes[0][2] == 2
    # c3's written a.txt has 1 del + 1 add = 2 churn lines; feature alpha churn from c1's diff
    assert snap.top_features_by_churn[0][2] >= 2


def test_cli_init_walk(tmp_path, repo, shas, capsys, monkeypatch):
    db_path = tmp_path / "cli.db"
    main(["init", str(repo), "--db", str(db_path)])
    main(["walk", "--db", str(db_path)])
    out = capsys.readouterr().out
    assert "walked 9 new commits" in out
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 9
    # init registered THE repository row and stored its id in settings
    row = conn.execute("SELECT * FROM repositories").fetchone()
    assert row["repo_key"] == "local:repo@main"
    rid = conn.execute("SELECT value FROM settings WHERE key = 'repo_id'").fetchone()[0]
    assert json.loads(rid) == row["id"]
    assert conn.execute("SELECT COUNT(DISTINCT repo_id) FROM commits").fetchone()[0] == 1


def test_cli_report_text(classified, capsys):
    with pytest.MonkeyPatch.context() as mp:
        import coding_stats.report as report

        mp.setattr(report, "snapshot", lambda conn, top=15: snapshot(conn, top))
        main(["report"])
    out = capsys.readouterr().out
    assert "Features / year" in out


def test_rolling_and_timeline(classified, repo):
    from coding_stats.derive import annotate_all, close_lineage
    from coding_stats.metrics import features_timeline, fixes_rolling

    assert annotate_all(repo, classified) == 2
    close_lineage(classified)
    tl = features_timeline(classified)
    assert tl and tl[-1].features_cumulative == 1
    assert sum(p.features_new for p in tl) == 1

    roll = fixes_rolling(classified)
    assert roll and all(p.fixes_per_feature >= 0 for p in roll)
    # c3 (targets c1 defining alpha) + c5 (fix-of-fix, lineage -> alpha) both
    # land on the same day: one sample, 2 Fix events (one reach each),
    # 1 distinct feature touched
    assert roll[-1].fix_events == 2
    assert roll[-1].features_touched == 1
    assert roll[-1].fixes_per_feature == 2.0


def test_fix_events_count_per_reach(tmp_path):
    """THE Fix-event semantics, discriminated from the old per-commit count:
    ONE fix commit rewriting a line from EACH of two features counts TWO Fix
    events (two (fix, Feature) reaches) — the pre-change `total_fixes` said
    one. Its events bucket by the Landing date (committer date), never the
    author date a rebase preserves. A fix reaching nothing is no event at
    all: it survives only in the audit view."""
    import os
    import subprocess

    repo = tmp_path / "rr"
    repo.mkdir()

    def git(*args: str, env_extra: dict | None = None) -> str:
        env = dict(os.environ, **(env_extra or {}))
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
        ).stdout

    def commit(msg: str, files: dict[str, str], dates: dict | None = None) -> str:
        for name, content in files.items():
            p = repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        git("add", "-A")
        git("commit", "-m", msg, env_extra=dates)
        return git("rev-parse", "HEAD").strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    git("config", "commit.gpgsign", "false")

    def pinned(day: str, t: str) -> dict[str, str]:
        # author and committer dates pinned equal: only the both-features fix
        # below diverges them (rebased work vs landing)
        return {
            "GIT_AUTHOR_DATE": f"2026-04-{day}T{t}",
            "GIT_COMMITTER_DATE": f"2026-04-{day}T{t}",
        }

    c1 = commit("feature one", {"f1.txt": "F1-A\nF1-B\n"}, dates=pinned("01", "12:00:00Z"))
    c2 = commit("feature two", {"f2.txt": "F2-A\nF2-B\n"}, dates=pinned("02", "12:00:01Z"))
    # THE fix: one commit rewriting a line from each feature. Rebases the
    # author date into January; lands (committer date) in March.
    commit(
        "fix both features",
        {"f1.txt": "F1-A-FIXED\nF1-B\n", "f2.txt": "F2-A-FIXED\nF2-B\n"},
        dates={
            "GIT_AUTHOR_DATE": "2026-01-10T12:00:00Z",
            "GIT_COMMITTER_DATE": "2026-03-20T12:00:00Z",
        },
    )
    # reach-less fixes: append-only (no deleted lines -> no targets, no
    # fixes_features edges, no Fix events). TWO of them, so the raw fix
    # COMMIT count (3) and the reach count (2) can never be confused.
    for i, day in enumerate(("05", "06"), 1):
        commit(
            f"fix nothing yet {i}",
            {f"n{i}.txt": "new\n"},
            dates={
                "GIT_AUTHOR_DATE": f"2026-02-{day}T12:00:00Z",
                "GIT_COMMITTER_DATE": f"2026-02-{day}T12:00:00Z",
            },
        )

    conn = make_db(tmp_path / "rr.db", repo)
    walk(repo, conn, "main")
    for sha, verdict in ((c1, "feature"), (c2, "feature")):
        conn.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
            " classified_at) VALUES ((SELECT id FROM commits WHERE sha = ?), ?, '', '', 'm', 'x')",
            (sha, verdict),
        )
    for sha in conn.execute("SELECT sha FROM commits WHERE sha NOT IN (?, ?)", (c1, c2)).fetchall():
        conn.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
            " classified_at) VALUES ((SELECT id FROM commits WHERE sha = ?), 'fix', '', '',"
            " 'm', 'x')",
            (sha["sha"],),
        )
    for sha, title in ((c1, "one"), (c2, "two")):
        fid = add_feature(conn, title, "", "2026-01-01")
        conn.execute(
            "INSERT INTO commits_features (commit_id, feature_id, role)"
            " VALUES ((SELECT id FROM commits WHERE sha = ?), ?, 'defines')",
            (sha, fid),
        )
    conn.commit()

    from coding_stats.derive import annotate_all, close_lineage
    from coding_stats.metrics import features_timeline, fixes_rolling

    assert annotate_all(repo, conn) == 3  # three fix commits annotated; the
    # reach-less ones target nothing (an append deletes no lines), so the
    # closure below sees exactly the both-features fix's two reaches
    close_lineage(conn)

    snap = snapshot(conn)
    assert snap.verdict_counts["fix"] == 3  # three fix COMMITS (raw count, unchanged)
    # but only TWO Fix events — both reaches of the one both-features fix.
    # The pre-change count (raw fix commits) said 3.
    assert snap.fix_event_total == 2
    assert snap.uncovered_fix_commits == 2  # the reach-less fixes: audit view only

    roll = fixes_rolling(conn)
    # the events' Landing date is the fix's COMMITTER date (2026-03-20); the
    # author date (2026-01-10) must never start the series
    assert roll[0].when == "2026-03-20"
    assert (roll[0].fix_events, roll[0].features_touched) == (2, 2)

    tl = features_timeline(conn)
    march = [p for p in tl if p.month == "2026-03"]
    assert march and march[0].fix_events == 2 and march[0].commits == 1
    assert all(p.fix_events == 0 for p in tl if p.month == "2026-02")  # reach-less: no event
    assert all(p.fix_events == 0 for p in tl if p.month == "2026-01")  # never the author date
    conn.close()


def test_rolling_and_timeline_empty_db(tmp_path):
    from coding_stats.db import connect
    from coding_stats.metrics import features_timeline, fixes_rolling

    conn = connect(tmp_path / "empty.db")
    assert features_timeline(conn) == []
    assert fixes_rolling(conn) == []


def test_html_report(tmp_path, classified, repo):
    from coding_stats.derive import annotate_all, close_lineage
    from coding_stats.htmlreport import write_report
    from coding_stats.metrics import feature_ranking

    assert annotate_all(repo, classified) == 2
    close_lineage(classified)

    out = write_report(classified, tmp_path / "r.html")
    text = out.read_text(encoding="utf-8")
    assert "<svg" in text and "coding-stats report" in text
    assert "alpha" in text  # fixture feature named in ranking table
    ranking = feature_ranking(classified)[:5]
    assert ranking and ranking[0].fid == "repo@main-1" and ranking[0].fixes == 2
