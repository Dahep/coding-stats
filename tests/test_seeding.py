"""Seeding tests (ADR-0006 clause 3, ticket #22): update for Target branch B
reuses sibling-Data-branch verdicts for shared shas — never re-sends them to
the LLM — while the deterministic stages keep running on B. External seam:
main([...]) over temp repos + bare origin."""

import sqlite3
import subprocess

from coding_stats import artifact, ci, datastore, db, gitwalk, identity
from coding_stats.cli import main
from conftest import commit_files, run, store_worktree


def _bare(tmp_path) -> str:
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(path)], check=True)
    return str(path)


def _with_remote(repo, bare) -> None:
    run(repo, "remote", "add", "origin", bare)
    run(repo, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "-C", bare, "symbolic-ref", "HEAD", "refs/heads/main"], check=True)


class Scripted:
    """Scripted classifier; logs every send; unexpected requests fail loudly."""

    session_id = None

    def __init__(self, replies, log):
        self.replies = list(replies)
        self.log = log

    def send(self, message: str) -> str:
        self.log.append(message)
        if not self.replies:
            raise AssertionError(f"unexpected classification request: {message[:100]}")
        return self.replies.pop(0)


def _sibling(tmp_path, repo, branch, classified_at, verdict="feature"):
    """A verifying sibling Data branch holding the shared history classified
    at its own time; the copied ids there keep their branch prefix. Built
    with the engine's own pieces and landed via the CLI verb."""
    from coding_stats.classify import create_feature

    conn = db.connect(str(tmp_path / f"{branch}.db"))
    identity.register_repository(conn, repo, branch)
    gitwalk.walk(repo, conn, "main")
    commits = conn.execute("SELECT id FROM commits ORDER BY walk_index").fetchall()
    for c in commits:
        fid = create_feature(conn, 1, "feature", "", classified_at)
        conn.execute(
            "INSERT INTO commits_features (commit_id, feature_id, role) VALUES (?, ?, 'defines')",
            (c["id"], fid),
        )
        conn.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
            " model, classified_at) VALUES (?, ?, 'seeded', '{}', 'seed-model', ?)",
            (c["id"], verdict, classified_at),
        )
    data = artifact.serialize(conn)
    conn.close()
    store = datastore.prepare(repo, branch)
    art = store.worktree / ".coding-stats" / "coding-stats.sql"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_bytes(data)
    assert ci.land(store, repo, "origin").status == "landed"
    return art


def _mat_rows(sql, query):
    mat = sqlite3.connect(":memory:")
    mat.executescript(sql)
    out = [tuple(r) for r in mat.execute(query)]
    mat.close()
    return out


def _art(repo, branch="main") -> str:
    return (store_worktree(repo, branch) / ".coding-stats" / "coding-stats.sql").read_text(
        encoding="utf-8"
    )


def test_update_seeds_shared_commits_from_verifying_sibling(tmp_path, repo, capsys, monkeypatch):

    commit_files(repo, "shared feature", {"s.txt": "s1\ns2\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    db1 = str(tmp_path / "main.db")
    main(["init", str(repo), "--db", db1])
    log1 = []
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            ['{"k":"new","title":"shared","about":"s","why":"y"}'], log1
        ),
    )
    monkeypatch.chdir(repo)
    main(["update", "--db", db1])
    main(["ci", "land", "--branch", "main"])
    capsys.readouterr()
    verdict_at = _mat_rows(_art(repo), "SELECT classified_at FROM commit_verdicts")[0][0]

    # branch dev shares main's history; a fix of the shared commit lands there
    run(repo, "checkout", "-q", "-b", "dev")
    commit_files(repo, "fix the shared module", {"s.txt": "ALPHA\ns2\n"})

    # run 2 (dev): the shared commit is seeded; the LLM only sees the fix
    db2 = str(tmp_path / "dev.db")
    main(["init", str(repo), "--branch", "dev", "--db", db2])
    log2 = []
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(['{"k":"fix","why":"repairs"}'], log2),
    )
    main(["update", "--db", db2])
    out = capsys.readouterr().out
    assert "seeded 1 commits from coding-stats/main" in out
    assert len(log2) == 1  # only the fix was really classified

    dev_art = _art(repo, "dev")
    ids = _mat_rows(dev_art, "SELECT id FROM features ORDER BY id")
    # the seeded feature keeps the id of the branch that first classified it
    assert ids == [("repo@main-1",)]
    rows = _mat_rows(
        dev_art,
        "SELECT c.walk_index, v.verdict, v.classified_at FROM commit_verdicts v"
        " JOIN commits c ON c.id = v.commit_id ORDER BY c.walk_index",
    )
    # the seeded row keeps the sibling's original classified_at (walk 1);
    # the dev fix classified later is walk 2 — 2 verdicts
    assert rows == [(1, "feature", verdict_at), (2, "fix", rows[1][2])]
    # fix attribution is the updating branch's own (detect + lineage ran)
    assert _mat_rows(dev_art, "SELECT COUNT(*) FROM fix_targets") == [(1,)]


def test_seeding_skips_non_verifying_sibling(tmp_path, repo, capsys, monkeypatch):
    commit_files(repo, "shared feature", {"s.txt": "s1\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    db1 = str(tmp_path / "main.db")
    main(["init", str(repo), "--db", db1])
    log1 = []
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            ['{"k":"new","title":"shared","about":"s","why":"y"}'], log1
        ),
    )
    monkeypatch.chdir(repo)
    main(["update", "--db", db1])
    main(["ci", "land", "--branch", "main"])
    # tamper the sibling's landed store: the artifact no longer verifies,
    # so it can never seed
    art = store_worktree(repo) / ".coding-stats" / "coding-stats.sql"
    art.write_bytes(art.read_bytes() + b"\ngarbage\n")
    main(["ci", "land", "--branch", "main"])
    capsys.readouterr()

    run(repo, "checkout", "-q", "-b", "dev")
    dev_db = str(tmp_path / "dev.db")
    main(["init", str(repo), "--branch", "dev", "--db", dev_db])
    log2 = []
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            ['{"k":"new","title":"shared-dev","about":"s","why":"y"}'], log2
        ),
    )
    main(["update", "--db", dev_db])
    out = capsys.readouterr().out
    assert "digest does not verify; skipped" in out
    assert "seeded" not in out  # optimization off: classified on its own
    assert len(log2) == 1  # the shared commit WAS sent to the LLM


def test_seeding_multi_source_earliest_wins(tmp_path, repo, capsys, monkeypatch):
    commit_files(repo, "shared feature", {"s.txt": "s1\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    db_path = str(tmp_path / "u.db")
    main(["init", str(repo), "--db", db_path])
    # two same-repo siblings cover the same sha; the EARLIER verdict wins
    _sibling(tmp_path, repo, "later", "2026-01-05T00:00:00Z", verdict="cleanup")
    _sibling(tmp_path, repo, "early", "2026-01-01T00:00:00Z", verdict="feature")
    capsys.readouterr()
    monkeypatch.chdir(repo)
    monkeypatch.setattr("coding_stats.cli.Session", lambda model, sid=None: Scripted([], []))
    main(["update", "--db", db_path])
    out = capsys.readouterr().out
    assert "seeded 1 commits from coding-stats/early" in out
    assert _mat_rows(
        _art(repo, "main"),
        "SELECT verdict, classified_at FROM commit_verdicts",
    ) == [("feature", "2026-01-01T00:00:00Z")]
