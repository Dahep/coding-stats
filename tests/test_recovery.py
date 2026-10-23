"""Partial recompute tests (ADR-0003 clause 3): anchor restore from the
artifact file's git history, full recompute fallback, coverage-horizon delta,
and the stateless-DB store seed behind it."""

import hashlib
import sqlite3
import subprocess

import pytest

from coding_stats import artifact, ci, datastore
from coding_stats.cli import main
from coding_stats.gitwalk import walk
from conftest import commit_files, make_db, run, store_dir


def _good_artifact(extra: bytes = b"") -> bytes:
    """A minimal self-verifying artifact blob (valid embedded digest)."""
    blank = (
        b"-- coding-stats artifact\n-- format: 1\n-- schema-step: 2\n"
        b"-- repository-key: x\n-- covered-through: y\n-- digest:\n"
    )
    digest = hashlib.sha256(blank + extra).hexdigest()
    return blank.replace(b"-- digest:\n", f"-- digest: {digest}\n".encode(), 1) + extra


class Scripted:
    """Scripted classifier; logs every send into the shared log list."""

    def __init__(self, replies, log):
        self.replies = list(replies)
        self.log = log
        self.session_id = None

    def send(self, message: str) -> str:
        self.log.append(message)
        return self.replies.pop(0) if self.replies else '{"k":"cleanup","why":"w"}'


def _init(tmp_path, repo) -> str:
    db_path = str(tmp_path / "r.db")
    main(["init", str(repo), "--db", db_path])
    return db_path


def _land_artifact(repo) -> str:
    """The run's resulting Data-branch tip: land (commit) via the CLI in the
    repo (like the CI step does) and return the orphan Data branch's tip."""
    import os

    os.chdir(repo)
    main(["ci", "land", "--branch", "main"])
    return _data_tip(repo)


def _data_tip(repo, branch: str = "coding-stats/main") -> str:
    """The local Data branch's tip (landed or not yet)."""
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"refs/heads/{branch}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _features(db_path):
    conn = sqlite3.connect(db_path)
    out = [r[0] for r in conn.execute("SELECT id FROM features ORDER BY id")]
    conn.close()
    return out


def test_partial_recompute_anchors_prior_committed_version(tmp_path, repo, capsys, monkeypatch):
    commit_files(repo, "feature one", {"a.txt": "a1\n"})
    commit_files(repo, "feature two", {"b.txt": "b1\n"})
    db_path = _init(tmp_path, repo)
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            [
                '{"k":"new","title":"one","about":"x","why":"y"}',
                '{"k":"new","title":"two","about":"x","why":"y"}',
            ],
            [],
        ),
    )
    main(["update", "--db", db_path])
    capsys.readouterr()
    v1_commit = _land_artifact(repo)  # committed artifact version (good)

    # a second version whose committed content is hand-tampered
    commit_files(repo, "feature three", {"c.txt": "c1\n"})
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(['{"k":"new","title":"three","about":"x","why":"y"}'], []),
    )
    main(["update", "--db", db_path])
    capsys.readouterr()
    art = store_dir(repo) / "coding-stats.sql"
    art.write_text(
        art.read_text(encoding="utf-8")
        + "\nINSERT INTO features (id, title, about, created_at, repo_id, live_lines)"
        " VALUES ('zzz-fake', 'fake', '', '2026-01-01T00:00:00Z', 1, 999);\n",
        encoding="utf-8",
    )
    _land_artifact(repo)

    log = []
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            ['{"k":"new","title":"three","about":"x","why":"y"}'], log
        ),
    )
    main(["update", "--db", db_path])
    out = capsys.readouterr().out
    assert "artifact digest: MISMATCH" in out
    assert f"anchored at artifact version {v1_commit[:12]}" in out  # not the tampered one
    assert "new coverage horizon" in out
    assert "zzz-fake" not in _features(db_path)  # tampered rows never loaded
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 3
    conn.close()
    assert len(log) == 1  # only the delta after v1 re-classified
    assert artifact.verify(art) is None


def test_full_recompute_when_no_committed_version_verifies(tmp_path, repo, capsys, monkeypatch):
    commit_files(repo, "feature one", {"a.txt": "a1\n"})
    commit_files(repo, "feature two", {"b.txt": "b1\n"})
    db_path = _init(tmp_path, repo)
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            [
                '{"k":"new","title":"one","about":"x","why":"y"}',
                '{"k":"new","title":"two","about":"x","why":"y"}',
            ],
            [],
        ),
    )
    main(["update", "--db", db_path])
    capsys.readouterr()
    # history-rewrite case: the ONLY committed version is tampered
    art = store_dir(repo) / "coding-stats.sql"
    art.write_text(
        art.read_text(encoding="utf-8")
        + "\nINSERT INTO features (id, title, about, created_at, repo_id, live_lines)"
        " VALUES ('zzz-fake', 'fake', '', '2026-01-01T00:00:00Z', 1, 999);\n",
        encoding="utf-8",
    )
    _land_artifact(repo)

    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            [
                '{"k":"new","title":"one","about":"x","why":"y"}',
                '{"k":"new","title":"two","about":"x","why":"y"}',
            ],
            [],
        ),
    )
    main(["update", "--db", db_path])
    out = capsys.readouterr().out
    assert "artifact digest: MISMATCH" in out
    assert "FULL-RECOMPUTE" in out
    assert "zzz-fake" not in _features(db_path)  # no tampered row ever loaded
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 2  # rebuilt from scratch
    conn.close()
    assert artifact.verify(art) is None


def test_reclassify_only_commits_newer_than_coverage_horizon(tmp_path, repo, capsys, monkeypatch):
    commit_files(repo, "feature one", {"a.txt": "a1\n"})
    commit_files(repo, "feature two", {"b.txt": "b1\n"})
    db_path = _init(tmp_path, repo)
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            [
                '{"k":"new","title":"one","about":"x","why":"y"}',
                '{"k":"new","title":"two","about":"x","why":"y"}',
            ],
            [],
        ),
    )
    main(["update", "--db", db_path])
    capsys.readouterr()
    _land_artifact(repo)  # anchor version: covers the first two commits
    commit_files(repo, "feature three", {"c.txt": "c1\n"})
    commit_files(repo, "feature four", {"d.txt": "d1\n"})

    # tampered CURRENT FILE (uncommitted hand-edit) resolves through the same
    # MISMATCH path: the newest self-verifying committed version anchors
    art = store_dir(repo) / "coding-stats.sql"
    art.write_bytes(art.read_bytes() + b"\n-- hand tamper\n")
    log = []
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            [
                '{"k":"new","title":"three","about":"x","why":"y"}',
                '{"k":"new","title":"four","about":"x","why":"y"}',
            ],
            log,
        ),
    )
    main(["update", "--db", db_path])
    out = capsys.readouterr().out
    assert "artifact digest: MISMATCH" in out
    assert "anchored at artifact version" in out
    assert len(log) == 2  # only the two commits past the horizon re-classified
    assert len(_features(db_path)) == 4  # two anchored + two re-derived
    assert artifact.verify(art) is None


def test_mismatch_without_history_falls_back_to_fresh_walk(tmp_path, repo, capsys, monkeypatch):
    commit_files(repo, "feature one", {"a.txt": "a1\n"})
    commit_files(repo, "feature two", {"b.txt": "b1\n"})
    db_path = _init(tmp_path, repo)  # no update yet: nothing committed, nothing trusted
    datastore.prepare(repo, "main")  # mount the store worktree first
    art = store_dir(repo) / "coding-stats.sql"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text("garbage without a digest line\n", encoding="utf-8")
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            [
                '{"k":"new","title":"one","about":"x","why":"y"}',
                '{"k":"new","title":"two","about":"x","why":"y"}',
            ],
            [],
        ),
    )
    main(["update", "--db", db_path])
    out = capsys.readouterr().out
    assert "artifact digest: MISMATCH" in out
    assert "FULL-RECOMPUTE" in out
    assert "walked 2 new commits" in out
    assert artifact.verify(art) is None


def test_update_seeds_fresh_db_from_verified_artifact(tmp_path, repo, capsys, monkeypatch):
    """The CI working DB is throwaway: a fresh DB continues from the committed
    store instead of re-analyzing (and re-classifying) everything."""
    commit_files(repo, "feature one", {"a.txt": "a1\n"})
    commit_files(repo, "feature two", {"b.txt": "b1\n"})
    db_path = _init(tmp_path, repo)
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            [
                '{"k":"new","title":"one","about":"x","why":"y"}',
                '{"k":"new","title":"two","about":"x","why":"y"}',
            ],
            [],
        ),
    )
    main(["update", "--db", db_path])
    capsys.readouterr()
    _land_artifact(repo)

    monkeypatch.chdir(repo)  # the checkout is the repo (CI semantics)
    ci_db = str(repo / ".coding-stats" / "working.db")
    log = []
    monkeypatch.setattr("coding_stats.cli.Session", lambda model, sid=None: Scripted([], log))
    main(["update", "--db", ci_db])
    out = capsys.readouterr().out
    assert "artifact digest: ok" in out
    assert "seeded working db from the artifact" in out
    assert "walked 0 new commits" in out
    assert log == []  # verdicts came from the store: nothing re-classified
    conn = sqlite3.connect(ci_db)
    assert conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM commit_verdicts").fetchone()[0] == 2
    conn.close()


# ---------------------------- diff rehydration, atomic swap, branch scoping --


def test_seed_rehydrates_pending_diffs_including_merges(tmp_path, repo, capsys, monkeypatch):
    from coding_stats import gitwalk
    from conftest import run as _run

    commit_files(repo, "base", {"a.txt": "a1\n"})
    _run(repo, "checkout", "-q", "-b", "side")
    commit_files(repo, "side work", {"s.txt": "s1\ns2\n"})
    _run(repo, "checkout", "-q", "main")
    _run(repo, "merge", "--no-ff", "side", "-m", "merge side work")
    db_path = _init(tmp_path, repo)
    # the committed store carries the backlog unclassified: artifact rows have
    # diff='' by format
    monkeypatch.setattr("coding_stats.cli.Session", lambda model, sid=None: Scripted([], []))
    main(["update", "--db", db_path, "--no-classify"])
    capsys.readouterr()
    _land_artifact(repo)

    monkeypatch.chdir(repo)
    ci_db = str(repo / ".coding-stats" / "working.db")
    log = []
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(['{"k":"new","title":"x","about":"x","why":"y"}'], log),
    )
    main(["update", "--db", ci_db])
    capsys.readouterr()

    conn = sqlite3.connect(ci_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT sha, diff FROM commits ORDER BY walk_index").fetchall()
    assert len(rows) == 2  # base + merge (first-parent walk)
    expected = {r["sha"]: gitwalk.prepare_diff(gitwalk.diff_for(repo, r["sha"])).diff for r in rows}
    for r in rows:
        assert r["diff"] == expected[r["sha"]]  # byte-identical to a fresh walk
        assert r["diff"].strip()  # non-empty: no noise classification
    conn.close()
    # classify received the real first-parent diffs — both commits sent
    # (incl. the merge: the empty-merge shortcut must not fire)
    assert len(log) == 2
    for r_sha in expected:
        assert any(expected[r_sha] in msg for msg in log)


def test_rebuild_swap_failure_keeps_original_db(tmp_path, repo, monkeypatch):
    from coding_stats import recovery

    commit_files(repo, "base", {"a.txt": "a1\n"})
    db_path = tmp_path / "r.db"
    conn = make_db(db_path, repo)
    walk(repo, conn, "main")
    conn.commit()
    conn.close()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(recovery.os, "replace", boom)
    old = sqlite3.connect(db_path)
    old.row_factory = sqlite3.Row
    with pytest.raises(OSError, match="disk full"):
        recovery.rebuild(db_path, None, old, repo, "main")
    # the original is intact and openable with its rows
    again = sqlite3.connect(db_path)
    assert again.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 1
    again.close()


def test_find_anchor_scopes_to_one_target_data_branch(tmp_path, repo):
    from coding_stats import datastore, recovery

    commit_files(repo, "base", {"a.txt": "a1\n"})
    # artifact history exists only on side's Data branch (its own orphan)
    side = datastore.prepare(repo, "side")
    store = side.worktree / ".coding-stats" / "coding-stats.sql"
    store.parent.mkdir(exist_ok=True)
    store.write_bytes(_good_artifact())
    run(side.worktree, "add", "-A", "--", ".coding-stats")
    run(side.worktree, "commit", "-m", "coding-stats artifact update (auto)")
    side_tip = _data_tip(repo, "coding-stats/side")

    assert datastore.anchor_ref(repo, "coding-stats/main") is None  # no main data branch
    assert recovery.find_anchor(repo, None) is None
    ref = datastore.anchor_ref(repo, "coding-stats/side")
    found = recovery.find_anchor(repo, ref)
    assert found is not None and found[0] == side_tip  # scoped: side's own history


def test_anchor_ref_resolves_local_then_fetched_remote(repo, tmp_path):
    from coding_stats import datastore

    commit_files(repo, "base", {"a.txt": "a\n"})
    assert datastore.anchor_ref(repo, "coding-stats/main") is None  # no Data branch

    # land once locally: the branch is born and anchors locally
    store = datastore.prepare(repo, "main")
    art = store.worktree / ".coding-stats" / "coding-stats.sql"
    art.parent.mkdir(exist_ok=True)
    art.write_bytes(_good_artifact())
    assert ci.land(store, repo).status == "landed"
    assert datastore.anchor_ref(repo, "coding-stats/main") == "refs/heads/coding-stats/main"

    # publish to a bare remote, then drop the local branch+worktree: the
    # fetched remote ref still gives an anchor (recovery survives a checkout
    # where only the remote's Data branch was fetched)
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    run(repo, "remote", "add", "origin", bare)
    run(repo, "push", "-q", "origin", "refs/heads/coding-stats/main:refs/heads/coding-stats/main")
    run(repo, "worktree", "remove", "--force", str(store.worktree))
    run(repo, "update-ref", "-d", "refs/heads/coding-stats/main")
    refspec = "refs/heads/coding-stats/main:refs/remotes/origin/coding-stats/main"
    run(repo, "fetch", "origin", refspec)
    assert datastore.anchor_ref(repo, "coding-stats/main") == (
        "refs/remotes/origin/coding-stats/main"
    )


def test_no_cross_branch_restore_on_mismatch(tmp_path, repo, capsys, monkeypatch):
    commit_files(repo, "feature one", {"a.txt": "a1\n"})
    commit_files(repo, "feature two", {"b.txt": "b1\n"})
    db_path = _init(tmp_path, repo)
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            [
                '{"k":"new","title":"one","about":"x","why":"y"}',
                '{"k":"new","title":"two","about":"x","why":"y"}',
            ],
            [],
        ),
    )
    main(["update", "--db", db_path])
    capsys.readouterr()
    # a self-verifying artifact version lives only on side's Data branch
    side = datastore.prepare(repo, "side")
    (side.worktree / ".coding-stats").mkdir(exist_ok=True)
    (side.worktree / ".coding-stats" / "coding-stats.sql").write_bytes(_good_artifact())
    run(side.worktree, "add", "-A", "--", ".coding-stats")
    run(side.worktree, "commit", "-m", "coding-stats artifact update (auto)")
    # tamper main's CURRENT store (uncommitted hand-edit); main's own Data
    # branch was never landed, so its anchor ref does not exist — and the
    # side Data branch's version is never restored
    main_store = datastore.prepare(repo, "main")
    art = main_store.worktree / ".coding-stats" / "coding-stats.sql"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_bytes(
        _good_artifact()
        + b"\nINSERT INTO features (id, title, about, created_at, repo_id, live_lines)"
        b" VALUES ('zzz-fake', 'fake', '', '2026-01-01T00:00:00Z', 1, 999);\n"
    )

    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            [
                '{"k":"new","title":"one","about":"x","why":"y"}',
                '{"k":"new","title":"two","about":"x","why":"y"}',
            ],
            [],
        ),
    )
    main(["update", "--db", db_path])
    out = capsys.readouterr().out
    assert "FULL-RECOMPUTE" in out  # the side-branch version is never restored
    assert "anchored at artifact version" not in out
    assert "zzz-fake" not in _features(db_path)


def _git_head(repo) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
