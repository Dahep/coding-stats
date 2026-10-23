"""Gather multi-branch acceptance tests (ADR-0006 clause 4, ticket #23).

Crafted union DBs exercise the org projection directly: Shared commits
dedup to one count (earliest classified_at wins, ties by Repository key),
flipped verdicts re-point or drop the loser's dependents, drifted defining
shas for one id refuse to gather naming both keys, and re-gathering one
key never deletes another key's rows."""

import pytest

from coding_stats import artifact, ci, datastore, db, identity
from coding_stats.cli import main
from coding_stats.metrics import feature_ranking, snapshot
from conftest import commit_files, run, store_worktree
from test_seeding import Scripted, _bare, _with_remote


def test_gather_two_data_branches_of_same_repo(tmp_path, repo, capsys, monkeypatch):
    """No refusal for two Target branches; seed-copied ids stay verbatim; the
    org totals count a Shared sha and a duplicated feature id once."""
    commit_files(repo, "shared feature", {"s.txt": "s1\ns2\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    db1 = str(tmp_path / "main.db")
    main(["init", str(repo), "--db", db1])
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            ['{"k":"new","title":"shared","about":"s","why":"y"}'], []
        ),
    )
    monkeypatch.chdir(repo)
    main(["update", "--db", db1])
    main(["ci", "land", "--branch", "main"])
    capsys.readouterr()

    run(repo, "checkout", "-q", "-b", "dev")
    commit_files(repo, "dev feature", {"d.txt": "d1\n"})
    dev_db = str(tmp_path / "dev.db")
    main(["init", str(repo), "--branch", "dev", "--db", dev_db])
    # the shared commit arrives seeded; only the dev-only commit classifies
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            ['{"k":"new","title":"dev-only","about":"d","why":"y"}'], []
        ),
    )
    main(["update", "--db", dev_db])
    main(["ci", "land", "--branch", "dev"])
    capsys.readouterr()

    union = str(tmp_path / "union.db")
    main(["gather", "--db", union, str(repo)])
    recon = db.connect(union)
    assert [
        r["repo_key"] for r in recon.execute("SELECT repo_key FROM repositories ORDER BY repo_key")
    ] == ["local:repo@dev", "local:repo@main"]
    assert recon.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"] == 3  # per key
    s = snapshot(recon)
    assert s.total_commits == 2  # the shared sha counted once
    assert s.total_features == 2  # 'repo@main-1' under both keys = ONE feature
    recon.close()


def test_regather_one_key_keeps_the_others_rows(tmp_path, repo, capsys, monkeypatch):
    """Re-gathering of one key must never delete another key's rows for a
    shared (seeded) feature id or its commits."""
    commit_files(repo, "shared feature", {"s.txt": "s1\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    db1 = str(tmp_path / "main.db")
    main(["init", str(repo), "--db", db1])
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            ['{"k":"new","title":"shared","about":"s","why":"y"}'], []
        ),
    )
    monkeypatch.chdir(repo)
    main(["update", "--db", db1])
    main(["ci", "land", "--branch", "main"])
    capsys.readouterr()

    run(repo, "checkout", "-q", "-b", "dev")
    commit_files(repo, "dev feature", {"d.txt": "d1\n"})
    dev_db = str(tmp_path / "dev.db")
    main(["init", str(repo), "--branch", "dev", "--db", dev_db])
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            ['{"k":"new","title":"dev-only","about":"d","why":"y"}'], []
        ),
    )
    main(["update", "--db", dev_db])
    capsys.readouterr()

    union = str(tmp_path / "union.db")
    main(["gather", "--db", union, str(repo)])
    dev_commits = [
        tuple(r)
        for r in db.connect(union).execute(
            "SELECT COUNT(*) c FROM commits WHERE repo_id ="
            " (SELECT id FROM repositories WHERE repo_key = 'local:repo@dev')"
        )
    ]
    assert dev_commits == [(2,)]  # dev's own two commits (shared + dev-only)
    main(["gather", "--db", union, str(repo)])  # dir source: expands to ALL
    main(
        [
            "gather",
            "--db",
            union,
            str(store_worktree(repo, "main") / ".coding-stats" / "coding-stats.sql"),
        ]
    )
    dev_rows = [
        tuple(r)
        for r in db.connect(union).execute(
            "SELECT COUNT(*) c FROM commits WHERE repo_id ="
            " (SELECT id FROM repositories WHERE repo_key = 'local:repo@dev')"
        )
    ]
    assert dev_rows == [(2,)]  # unchanged: re-gathering one key deletes nothing
    shared = [
        r[0]
        for r in db.connect(union).execute(
            """SELECT COUNT(DISTINCT s.feature_id) c
               FROM feature_line_samples s
               JOIN features f ON f.id = s.feature_id
               WHERE f.id = 'repo@main-1'"""
        )
    ]
    assert shared == [1]  # the shared feature's rows still exist for both keys


def _crafted(path: str | None, cleanup_winner: bool = False, fix: bool = False):
    """Two keys of ONE repo classify sha x40. 'early' (2026-01-01) always
    wins over 'later' (2026-01-05). The 'later' key also holds a fix commit
    yfix of x40, attributed (lineage) to the LOSING verdict's feature."""
    conn = db.connect(path or ":memory:")
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
                 (2, 2, 'x40', NULL, 't', '2026-01-01T00:00:00Z', 't', 'one', 'm', 1,
                  'normal', '', 1, 0, 1);
        """
    )
    if fix:
        conn.executescript(
            """
        INSERT INTO commits
          (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title,
           message, walk_index, kind, diff, added_lines, deleted_lines, churn_lines)
          VALUES (3, 1, 'yfix', 'x40', 't', '2026-01-06T00:00:00Z', 't', 'fix it', 'm', 2,
                  'normal', '', 1, 1, 2);
        INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,
                                     model, run_id, classified_at)
          VALUES (3, 'fix', '', '{}', 'm', NULL, '2026-01-06T00:00:00Z');
        INSERT INTO fix_targets (fix_commit_id, target_commit_id) VALUES (3, 1);
        INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id)
          VALUES (3, 'eq@later-1', NULL);
        """
        )
    if cleanup_winner:
        # the winner (earliest) emits no features: its唯一 attribution set empty
        conn.executescript(
            """
        INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,
                                     model, run_id, classified_at)
          VALUES (2, 'cleanup', '', '{}', 'm', NULL, '2026-01-01T00:00:00Z'),
                 (1, 'feature', '', '{}', 'm', NULL, '2026-01-05T00:00:00Z');
        INSERT INTO features (id, title, about, title_history_json, created_at,
                              created_run_id, repo_id, live_lines)
          VALUES ('eq@later-1', 'loser feature', '', '[]',
                  '2026-01-05T00:00:00Z', NULL, 1, 8);
        INSERT INTO commits_features (commit_id, feature_id, role)
          VALUES (1, 'eq@later-1', 'defines');
        """
        )
    else:
        conn.executescript(
            """
        INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,
                                     model, run_id, classified_at)
          VALUES (1, 'feature', '', '{}', 'm', NULL, '2026-01-05T00:00:00Z'),
                 (2, 'feature', '', '{}', 'm', NULL, '2026-01-01T00:00:00Z');
        INSERT INTO features (id, title, about, title_history_json, created_at,
                              created_run_id, repo_id, live_lines)
          VALUES ('eq@later-1', 'loser feature', '', '[]',
                  '2026-01-05T00:00:00Z', NULL, 1, 8),
                 ('eq@early-1', 'winner feature', '', '[]',
                  '2026-01-01T00:00:00Z', NULL, 2, 8);
        INSERT INTO commits_features (commit_id, feature_id, role)
          VALUES (1, 'eq@later-1', 'defines'), (2, 'eq@early-1', 'defines');
        """
        )
    conn.commit()
    return conn


def test_loser_dependents_repoint_through_winner(tmp_path):
    """The losing verdict's own feature dies; a fix lineaged onto it re-points
    to the feature the winner attributes the shared sha to."""
    from coding_stats.orgview import org

    union = str(tmp_path / "union.db")
    conn = _crafted(union, cleanup_winner=False, fix=True)
    s = snapshot(conn)
    assert s.total_commits == 2  # x40 (shared, once) + yfix
    assert s.total_features == 1  # eq@later-1 purged, eq@early-1 survives
    assert [f.fid for f in feature_ranking(conn)] == ["eq@early-1"]
    # per-key rows untouched in the union; the org projection re-points
    view = org(conn)
    rows = view.execute("SELECT feature_id FROM fixes_features WHERE fix_commit_id = 3").fetchall()
    assert [tuple(r)[0] for r in rows] == ["eq@early-1"]
    conn.close()


def test_loser_dependents_dropped_when_winner_attributes_nothing(tmp_path):
    from coding_stats.orgview import org

    union = str(tmp_path / "union.db")
    conn = _crafted(union, cleanup_winner=True, fix=True)
    s = snapshot(conn)
    assert s.total_commits == 2
    # the winner (a cleanup verdict) attributes x40 to nothing: the fix's
    # attributed row DROPPED in the projection; per-key rows stay untouched
    view = org(conn)
    rows = view.execute("SELECT feature_id FROM fixes_features WHERE fix_commit_id = 3").fetchall()
    assert rows == []
    assert [
        tuple(r) for r in conn.execute("SELECT fix_commit_id, feature_id FROM fixes_features")
    ] == [(3, "eq@later-1")]  # the raw union still holds the losing row
    assert s.total_features == 0
    assert feature_ranking(conn) == []
    conn.close()


def _drifted_artifact(tmp_path, repo, branch, fid, def_sha) -> None:
    """An artifact of `branch` whose feature `fid` is DEFINED by `def_sha`."""
    conn = db.connect(str(tmp_path / f"{branch.replace('/', '-')}.db"))
    identity.register_repository(conn, repo, branch)
    conn.execute(
        """INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at,
           author_name, title, message, walk_index, kind, diff,
           added_lines, deleted_lines, churn_lines)
           VALUES (1, 1, ?, NULL, 't', '2026-01-01T00:00:00Z', 't', 'one', 'm', 1,
                   'normal', '', 1, 0, 1)""",
        (def_sha,),
    )
    conn.execute(
        """INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,
           model, run_id, classified_at) VALUES (1, 'feature', '', '{}', 'm', NULL,
           '2026-01-01T00:00:00Z')"""
    )
    conn.execute(
        """INSERT INTO features (id, title, about, title_history_json, created_at,
           created_run_id, repo_id, live_lines) VALUES (?, 'feature', '', '[]',
           '2026-01-01T00:00:00Z', NULL, 1, 0)""",
        (fid,),
    )
    conn.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role) VALUES (1, ?, 'defines')",
        (fid,),
    )
    data = artifact.serialize(conn)
    conn.close()
    store = datastore.prepare(repo, branch)
    art = store.worktree / ".coding-stats" / "coding-stats.sql"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_bytes(data)
    assert ci.land(store, repo, "origin").status == "landed"


def test_drifted_defining_sha_gathers_loudly(tmp_path, repo, capsys):
    """The same id defined by different shas across two of the repo's
    artifacts: a gather error naming BOTH keys — never a silent merge."""
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    _drifted_artifact(tmp_path, repo, "b1", "repo@x-1", "x40")
    _drifted_artifact(tmp_path, repo, "b2", "repo@x-1", "y40")
    union = str(tmp_path / "union.db")
    store_a = store_worktree(repo, "b1") / ".coding-stats" / "coding-stats.sql"
    store_b = store_worktree(repo, "b2") / ".coding-stats" / "coding-stats.sql"
    with pytest.raises(SystemExit) as excinfo:
        main(["gather", "--db", union, str(store_a), str(store_b)])
    msg = str(excinfo.value)
    assert "drifted" in msg
    assert "local:repo@b1" in msg and "local:repo@b2" in msg
    assert "repo@x-1" in msg


def test_gather_unmounted_data_branch_refs(tmp_path, repo, capsys, monkeypatch):
    """Sources with UNMOUNTED data branches (refs fetched, no store on
    disk) — gather reads the artifacts straight out of the refs."""
    commit_files(repo, "shared feature", {"s.txt": "s1\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    db1 = str(tmp_path / "main.db")
    main(["init", str(repo), "--db", db1])
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            ['{"k":"new","title":"shared","about":"s","why":"y"}'], []
        ),
    )
    monkeypatch.chdir(repo)
    main(["update", "--db", db1])
    main(["ci", "land", "--branch", "main"])
    capsys.readouterr()
    run(repo, "checkout", "-q", "-b", "dev")
    commit_files(repo, "dev feature", {"d.txt": "d1\n"})
    dev_db = str(tmp_path / "dev.db")
    main(["init", str(repo), "--branch", "dev", "--db", dev_db])
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            ['{"k":"new","title":"dev-only","about":"d","why":"y"}'], []
        ),
    )
    main(["update", "--db", dev_db])
    main(["update", "--db", dev_db])
    main(["ci", "land", "--branch", "dev"])  # land BEFORE unmounting

    # unmount: remove the mounted worktrees + prune; refs remain
    for branch in ("main", "dev"):
        wt = store_worktree(repo, branch)
        if (wt / ".git").exists() or (wt / ".git").is_file():
            run(repo, "worktree", "remove", "--force", str(wt))
    run(repo, "update-ref", "-d", "refs/heads/coding-stats/main")
    run(repo, "update-ref", "-d", "refs/heads/coding-stats/dev")
    # wait — the local heads belong to the CHECKED OUT worktrees; removed
    # above, so deletion succeeds; the remote-tracking refs tell the truth
    run(repo, "fetch", "origin")
    assert not (store_worktree(repo, "main") / ".coding-stats").exists()
    assert not (store_worktree(repo, "dev") / ".coding-stats").exists()

    union = str(tmp_path / "union.db")
    main(["gather", "--db", union, str(repo)])
    recon = db.connect(union)
    assert [
        r["repo_key"] for r in recon.execute("SELECT repo_key FROM repositories ORDER BY repo_key")
    ] == ["local:repo@dev", "local:repo@main"]  # both discovered via refs
    assert snapshot(recon).total_commits == 2  # the shared sha once
    recon.close()
