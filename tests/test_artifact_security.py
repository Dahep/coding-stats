"""Artifact replay is never trusted as code (ADR-0003 honest-limits follow-up).

The artifact digest is an unkeyed checksum — anyone with commit access can
recompute it — so artifact bytes must never execute arbitrary SQL. The
materialize authorizer permits ONLY the curated shape (CREATE TABLE of the
known tables + INSERTs into them); ATTACH/DETACH, PRAGMAs, DML beyond INSERT,
and any other DDL are denied. Both callers are covered: gather ingest and
recovery rebuild (anchor bytes from artifact git history are untrusted too).
"""

import hashlib
import re
import sqlite3
from pathlib import Path

import pytest

from coding_stats import artifact, recovery
from coding_stats.cli import main
from conftest import commit_files, run


class Scripted:
    def __init__(self, replies):
        self.replies = list(replies)
        self.session_id = None

    def send(self, message: str) -> str:
        return self.replies.pop(0) if self.replies else '{"k":"cleanup","why":"w"}'


def _updated_repo(tmp_path) -> Path:
    repo = tmp_path / "alpha-repo"
    repo.mkdir(parents=True)
    run(repo, "init", "-q", "-b", "main")
    run(repo, "config", "user.name", "T")
    run(repo, "config", "user.email", "t@t.io")
    run(repo, "config", "commit.gpgsign", "false")
    commit_files(repo, "feature one", {"a.txt": "a1\n"})
    db_path = str(tmp_path / "alpha-repo.db")
    main(["init", str(repo), "--db", db_path])
    import unittest.mock as mock

    with mock.patch(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(['{"k":"new","title":"one","about":"x","why":"y"}']),
    ):
        main(["update", "--db", db_path, "--no-detect", "--no-lineage", "--no-report"])
    return repo


def _rechecksum(data: bytes) -> bytes:
    """Recompute the embedded digest like an attacker would (unkeyed)."""
    m = re.search(rb"^-- digest:(?: [0-9a-f]+)?$", data, re.M)
    assert m is not None
    blanked = data[: m.start()] + b"-- digest:" + data[m.end() :]
    digest = hashlib.sha256(blanked).hexdigest()
    return blanked.replace(b"-- digest:\n", f"-- digest: {digest}\n".encode(), 1)


def _hostile_artifact(repo: Path, payload: str) -> bytes:
    art = repo / ".coding-stats" / "coding-stats.sql"
    return _rechecksum(art.read_bytes() + ("\n" + payload + "\n").encode())


HOSTILE = [
    "ATTACH DATABASE '/tmp/opencode/pwned.db' AS victim;",
    "CREATE TABLE victim.pwned(x);",
    "INSERT INTO victim.pwned VALUES (1);",
    ".shell echo pwned",
    "PRAGMA user_version = 7;",
    "DROP TABLE commits;",
    "DELETE FROM features;",
    "UPDATE features SET title = 'x';",
    "CREATE TABLE pwned(x);",
    "CREATE VIEW v AS SELECT 1;",
    "CREATE TRIGGER t BEFORE INSERT ON features BEGIN SELECT 1; END;",
]


@pytest.mark.parametrize("payload", HOSTILE)
def test_materialize_refuses_hostile_sql(tmp_path, payload):
    repo = _updated_repo(tmp_path)
    data = _hostile_artifact(repo, payload)
    conn = sqlite3.connect(":memory:")
    with pytest.raises(artifact.MaterializeError):
        artifact.materialize(conn, data)
    conn.close()


def test_gather_refuses_hostile_artifact(tmp_path):
    repo = _updated_repo(tmp_path)
    art = repo / ".coding-stats" / "coding-stats.sql"
    art.write_bytes(_hostile_artifact(repo, "ATTACH DATABASE '/tmp/opencode/pwned.db' AS v;"))
    union = tmp_path / "union.db"
    with pytest.raises(SystemExit) as excinfo:
        main(["gather", "--db", str(union), str(repo)])
    assert "alpha-repo" in str(excinfo.value)
    assert not union.exists()


def test_recovery_rebuild_refuses_hostile_anchor(tmp_path):
    repo = _updated_repo(tmp_path)
    data = _hostile_artifact(repo, "DROP TABLE commits;")
    old = sqlite3.connect(tmp_path / "old.db")
    old.row_factory = sqlite3.Row
    with pytest.raises(artifact.MaterializeError):
        recovery.rebuild(tmp_path / "work.db", data, old, repo, "main")
    old.close()


def test_benign_artifact_materializes_with_curated_shape(tmp_path):
    repo = _updated_repo(tmp_path)
    art = repo / ".coding-stats" / "coding-stats.sql"
    assert artifact.verify(art) is None
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    artifact.materialize(conn, art.read_bytes())
    # shape drift guard: the replayed tables carry exactly the curated columns
    for name, spec in artifact.KNOWN_TABLES.items():
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({name})")]
        assert cols == list(spec), name
    assert conn.execute("SELECT COUNT(*) c FROM features").fetchone()["c"] == 1
    conn.close()
