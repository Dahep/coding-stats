"""Gather + org report union of PR-channel attribution (ticket #38).

The union resolves ONE attribution per Shared commit out of the evidence
every branch's copy carries, by the channel-precedence rule — git-only
evidence outranks inherited PR-channel evidence — REUSING prsweep's
effective_* at the union level, never a second rule. The org report's AI
metrics (AI share per feature/repo, harness/model/channel breakdowns) are
rendered as LOWER BOUNDS: never normalized upward to PR counts,
``no-evidence`` never rendered as "human", GitHub-sourced rows carrying
#37's disclosure line. Gathered numbers are reproducible from the
per-branch artifacts: rows are recorded state, never re-derived.
"""

import sqlite3

from coding_stats import artifact, db, gather, gitwalk, htmlreport, orgview, prsweep
from coding_stats.db import connect
from coding_stats.metrics import (
    attribution_by_feature,
    attribution_by_repo,
    attribution_rows,
    attribution_summary,
    snapshot,
)
from coding_stats.report import text_report
from conftest import add_feature, commit_files, make_db, run

OBSERVED_AT = "2026-10-30T00:00:00+00:00"
TRAILER_VALUE = "Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
DISCLOSURE = "GitHub-sourced (PR-channel) evidence present"


def _pr_rows(conn):
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT repo_id, pr_number, commit_id, rule, raw, verdict, harness, model,"
            " channel, observed_at, suspicious FROM pr_evidence"
            " ORDER BY repo_id, pr_number, commit_id, rule, raw"
        )
    ]


def _two_key_union(tmp_path, repo):
    """One repo, two Target branches (Repository keys) sharing both commits:
    key @main carries the git-channel evidence plus a feature; key @release
    carries ONLY recorded PR-channel evidence (an artifact whose git rows
    are absent while its recorded PR rows stand). The @main key wins the
    Shared-commit dedup, so the PR rows must RE-POINT to it, never drop."""
    h_c = commit_files(
        repo, f"feat: trailered\n\nCo-authored-by: {TRAILER_VALUE}\n", {"a.txt": "1\n"}
    )
    h_d = commit_files(repo, "fix: rounding", {"b.txt": "2\n"})
    run(repo, "branch", "release")

    conn_a = make_db(tmp_path / "main.db", repo, "main")
    gitwalk.walk(repo, conn_a, "main")
    fid = add_feature(conn_a, "alpha module")
    conn_a.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role)"
        " VALUES ((SELECT id FROM commits WHERE sha = ?), ?, 'defines')",
        (h_c, fid),
    )
    conn_a.commit()
    sources = [artifact.write(conn_a, tmp_path / "main-store")]
    conn_a.close()

    conn_b = make_db(tmp_path / "release.db", repo, "release")
    gitwalk.walk(repo, conn_b, "release")
    ids = {r["sha"]: int(r["id"]) for r in conn_b.execute("SELECT id, sha FROM commits")}
    conn_b.execute("DELETE FROM ai_attribution")
    conn_b.execute("DELETE FROM ai_evidence")
    prsweep.replace_pr_rows(
        conn_b,
        7,
        [
            prsweep.PrRow(
                "tool-trailer", "codex:gpt-5", "yes", ids[h_c], False, "codex", "gpt-5"
            ),
            prsweep.PrRow(
                "tool-trailer", "codex:gpt-5", "yes", ids[h_d], False, "codex", "gpt-5"
            ),
        ],
        OBSERVED_AT,
    )
    conn_b.execute(
        "INSERT INTO pr_watermark (repo_id, last_pr_number)"
        " VALUES ((SELECT id FROM repositories ORDER BY id LIMIT 1), 7)"
    )
    conn_b.commit()
    sources.append(artifact.write(conn_b, tmp_path / "release-store"))
    conn_b.close()

    union = tmp_path / "union.db"
    assert gather.gather(union, sources).ingested == 2
    return union, h_c, h_d


def _attribution_state(union_path):
    """Everything gathered attribution must make reproducible."""
    conn = sqlite3.connect(union_path)
    conn.row_factory = sqlite3.Row
    try:
        return (
            attribution_summary(conn),
            attribution_by_repo(conn),
            attribution_by_feature(conn),
            attribution_rows(conn),
        )
    finally:
        conn.close()


# ---------------------------------------------------------- acceptance ----


def test_shared_commit_is_attributed_once_by_the_stronger_signal(tmp_path, repo):
    union, h_c, h_d = _two_key_union(tmp_path, repo)
    uconn = sqlite3.connect(union)
    uconn.row_factory = sqlite3.Row
    view = orgview.org(uconn)
    assert snapshot(view).total_commits == 2  # a Shared commit counts once

    s = attribution_summary(view)
    # PR-channel evidence feeds gathered metrics (h_d carries only PR rows),
    # and the Shared commit keeps ONE attribution — by the stronger signal
    assert s.verdict_counts == {"yes": 2}
    assert s.channel_counts == {"git": 1, "pr": 1}
    assert dict(s.harness_counts) == {"claude": 1, "codex": 1}  # git-first on h_c
    assert dict(s.model_counts) == {"Opus 4.8 (1M context)": 1, "gpt-5": 1}
    assert s.github_sourced is True
    assert s.ai_share == 1.0

    cid = int(view.execute("SELECT id FROM commits WHERE sha = ?", (h_c,)).fetchone()["id"])
    eff = prsweep.effective_attribution(view, cid)
    assert (eff.verdict, eff.harness, eff.model) == ("yes", "claude", "Opus 4.8 (1M context)")
    # evidence from several branches feeds that ONE attribution: the git row
    # first (it outranks), the inherited PR row still attached
    ev = prsweep.effective_evidence(view, cid)
    assert [(c, rule) for c, rule, _raw, _v in ev] == [
        ("git", "co-authored-by"),
        ("pr", "tool-trailer"),
    ]

    # AI share per repo (lower bound): shared commits count ONCE
    repos = {a.slug: a for a in attribution_by_repo(view)}
    assert (repos["repo@main"].commits, repos["repo@main"].yes) == (2, 2)
    assert sum(a.commits for a in repos.values()) == 2
    assert sum(a.yes for a in repos.values()) == 2
    # ... and per feature
    feats = attribution_by_feature(view)
    assert [(f.fid, f.commits, f.yes) for f in feats] == [("repo@main-1", 1, 1)]

    rows = attribution_rows(view)
    assert [r.sha[:7] for r in rows] == [h_c[:7], h_d[:7]]
    by_sha = {r.sha: r for r in rows}
    assert (by_sha[h_c].harness, by_sha[h_c].channel) == ("claude", "git")  # stronger signal
    assert (by_sha[h_d].harness, by_sha[h_d].channel) == ("codex", "pr")  # PR-only counts
    uconn.close()


def test_loser_git_rows_outrank_winner_pr_rows_in_the_union(tmp_path):
    """Evidence from several branches of one repo feeds ONE attribution,
    resolved by channel precedence — not by which key won the dedup: the
    LOSER's git rows outrank the WINNER's PR-channel claim."""
    conn = db.connect(tmp_path / "union.db")
    conn.executescript(
        """
        INSERT INTO repositories (id, repo_key, slug, branch, clone_url, added_at)
          VALUES (1, 'local:eq@later', 'eq@later', 'later', NULL, ''),
                 (2, 'local:eq@early', 'eq@early', 'early', NULL, '');
        INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at,
                             author_name, title, message, walk_index, kind, diff,
                             added_lines, deleted_lines, churn_lines)
          VALUES (1, 1, 'x40', NULL, 't', '2026-01-05T00:00:00Z', 't', 'one', 'm', 1,
                  'normal', '', 1, 0, 1),
                 (2, 2, 'x40', NULL, 't', '2026-01-05T00:00:00Z', 't', 'one', 'm', 1,
                  'normal', '', 1, 0, 1);
        INSERT INTO ai_attribution (commit_id, verdict, harness, model)
          VALUES (1, 'yes', 'claude', 'Opus 4.8 (1M context)');
        INSERT INTO ai_evidence (commit_id, rule, raw, verdict)
          VALUES (1, 'co-authored-by', 'Claude <noreply@anthropic.com>', 'yes');
        INSERT INTO pr_evidence (repo_id, pr_number, commit_id, rule, raw, verdict,
                                 harness, model, channel, observed_at, suspicious)
          VALUES (2, 5, 2, 'tool-trailer', 'codex:gpt-5', 'yes', 'codex', 'gpt-5',
                  'pr', '2026-10-30T00:00:00+00:00', 0);
        """
    )
    conn.commit()
    view = orgview.org(conn)
    assert view.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"] == 1
    cid = int(view.execute("SELECT id FROM commits WHERE sha = 'x40'").fetchone()["id"])
    eff = prsweep.effective_attribution(view, cid)
    assert (eff.verdict, eff.harness, eff.model) == ("yes", "claude", "Opus 4.8 (1M context)")
    assert [c for c, _r, _raw, _v in prsweep.effective_evidence(view, cid)] == ["git", "pr"]
    assert attribution_summary(view).verdict_counts == {"yes": 1}
    conn.close()


def test_regather_after_a_branch_recompute_changes_nothing_attributed(tmp_path, repo):
    h1 = commit_files(
        repo, f"feat: trailered\n\nCo-authored-by: {TRAILER_VALUE}\n", {"a.txt": "1\n"}
    )
    _h2 = commit_files(repo, "fix: rounding", {"b.txt": "2\n"})
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    ids = {r["sha"]: int(r["id"]) for r in conn.execute("SELECT id, sha FROM commits")}
    prsweep.replace_pr_rows(
        conn,
        7,
        [prsweep.PrRow("tool-trailer", "codex:gpt-5", "yes", ids[h1], False, "codex", "gpt-5")],
        OBSERVED_AT,
    )
    prsweep.replace_pr_rows(
        conn,
        8,
        [prsweep.PrRow("branch-prefix", "ghost/x", "possible", None, True, None, None)],
        OBSERVED_AT,
    )
    conn.execute(
        "INSERT INTO pr_watermark (repo_id, last_pr_number)"
        " VALUES ((SELECT id FROM repositories ORDER BY id LIMIT 1), 8)"
    )
    conn.commit()
    art1 = artifact.write(conn, tmp_path / "store")
    recorded = _pr_rows(conn)
    conn.close()

    union1 = tmp_path / "u1.db"
    assert gather.gather(union1, [art1]).ingested == 1
    state1 = _attribution_state(union1)

    # the branch's recompute: the git rows are re-derived from the commit
    # objects (pure functions); the recorded PR rows ride through untouched
    restored_path = tmp_path / "re.db"
    raw = sqlite3.connect(restored_path)
    artifact.restore_into(raw, art1.read_bytes())
    raw.close()
    restored = connect(restored_path)
    restored.execute("DELETE FROM ai_attribution")
    restored.execute("DELETE FROM ai_evidence")
    restored.commit()
    assert gitwalk.backfill_attribution(restored, repo) == 2
    assert gitwalk.walk(repo, restored, "main") == 0
    assert _pr_rows(restored) == recorded  # recorded state, never re-derived
    art2 = artifact.write(restored, tmp_path / "store2")
    restored.close()
    assert art2.read_bytes() == art1.read_bytes()  # reproducible from the artifact

    union2 = tmp_path / "u2.db"
    assert gather.gather(union2, [art2]).ingested == 1
    assert _attribution_state(union2) == state1  # nothing attributed changes


# ------------------------------------------------------ report contract ----


def test_org_report_renders_breakdowns_lower_bounds_and_the_disclosure(tmp_path, repo):
    union, _h_c, _h_d = _two_key_union(tmp_path, repo)
    uconn = sqlite3.connect(union)
    uconn.row_factory = sqlite3.Row
    page = htmlreport.build(uconn)
    assert DISCLOSURE in page  # #37's contract: GitHub-sourced rows present
    assert "lower bound" in page.lower()
    for label in ("Channel", "Harness", "Model"):
        assert label in page  # attribution breakdowns
    assert "claude" in page and "codex" in page  # harness breakdown
    assert "Opus 4.8 (1M context)" in page  # model breakdown, where known
    assert "AI share per repository" in page  # org view: AI share per repo
    assert "AI share per feature" in page
    assert "human" not in page.lower()

    text = text_report(uconn)
    assert DISCLOSURE in text
    assert "lower bound" in text.lower()
    assert "human" not in text.lower()
    uconn.close()


def test_git_only_report_carries_no_github_disclosure(tmp_path, repo):
    commit_files(repo, f"feat: trailered\n\nCo-authored-by: {TRAILER_VALUE}\n", {"a.txt": "1\n"})
    conn = make_db(tmp_path / "main.db", repo)
    gitwalk.walk(repo, conn, "main")
    art = artifact.write(conn, tmp_path / "store")
    conn.close()
    union = tmp_path / "union.db"
    assert gather.gather(union, [art]).ingested == 1
    uconn = sqlite3.connect(union)
    uconn.row_factory = sqlite3.Row
    page = htmlreport.build(uconn)
    assert "AI attribution" in page
    assert DISCLOSURE not in page  # the mode is off: no GitHub-sourced rows
    assert "lower bound" in page.lower()  # the framing is always on
    assert "human" not in page.lower()
    uconn.close()
