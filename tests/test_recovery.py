"""Partial recompute tests (ADR-0003 clause 3): anchor restore from the
artifact file's git history, full recompute fallback, coverage-horizon delta,
and the stateless-DB store seed behind it."""

import sqlite3

from conftest import commit_files, run
from skill_stats import artifact
from skill_stats.cli import main


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
    """Commit the current .skill-stats/ store the way ci land does."""
    run(repo, "add", "-A", "--", ".skill-stats")
    run(repo, "commit", "-m", "skill-stats artifact update (auto)")
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
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
        "skill_stats.cli.Session",
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
        "skill_stats.cli.Session",
        lambda model, sid=None: Scripted(['{"k":"new","title":"three","about":"x","why":"y"}'], []),
    )
    main(["update", "--db", db_path])
    capsys.readouterr()
    art = repo / ".skill-stats" / "skill-stats.sql"
    art.write_text(
        art.read_text(encoding="utf-8")
        + "\nINSERT INTO features (id, title, about, created_at, repo_id, live_lines)"
        " VALUES ('zzz-fake', 'fake', '', '2026-01-01T00:00:00Z', 1, 999);\n",
        encoding="utf-8",
    )
    _land_artifact(repo)

    log = []
    monkeypatch.setattr(
        "skill_stats.cli.Session",
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
        "skill_stats.cli.Session",
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
    art = repo / ".skill-stats" / "skill-stats.sql"
    art.write_text(
        art.read_text(encoding="utf-8")
        + "\nINSERT INTO features (id, title, about, created_at, repo_id, live_lines)"
        " VALUES ('zzz-fake', 'fake', '', '2026-01-01T00:00:00Z', 1, 999);\n",
        encoding="utf-8",
    )
    _land_artifact(repo)

    monkeypatch.setattr(
        "skill_stats.cli.Session",
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
        "skill_stats.cli.Session",
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
    art = repo / ".skill-stats" / "skill-stats.sql"
    art.write_bytes(art.read_bytes() + b"\n-- hand tamper\n")
    log = []
    monkeypatch.setattr(
        "skill_stats.cli.Session",
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
    art = repo / ".skill-stats" / "skill-stats.sql"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text("garbage without a digest line\n", encoding="utf-8")
    monkeypatch.setattr(
        "skill_stats.cli.Session",
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
        "skill_stats.cli.Session",
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
    ci_db = str(repo / ".skill-stats" / "working.db")
    log = []
    monkeypatch.setattr("skill_stats.cli.Session", lambda model, sid=None: Scripted([], log))
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
