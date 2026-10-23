"""Legacy skill-stats migration tests (ADR-0005): DB reuse across the rename,
artifact-only rebuild, identity-rule interplay, CI scaffold swap, idempotency.

The legacy artifact is produced by the CURRENT serializer, then re-stamped to
the old header line with a recomputed digest — pre-rename artifacts differ
from new-format ones in exactly the header tag.
"""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from coding_stats import artifact
from coding_stats.cli import main
from conftest import commit_files, run


class FakeSession:
    """Scripted classifier: pops one reply per send (no LLM)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.session_id = None

    def send(self, message: str) -> str:
        return self.replies.pop(0)


def old_format_bytes(conn: sqlite3.Connection) -> bytes:
    """Serialize with the new header tag, then re-stamp to the legacy one:
    blank the digest, swap the tag, recompute — byte-for-byte what the
    pre-rename tool wrote."""
    content = artifact.serialize(conn)
    digest = content.split(b"-- digest: ", 1)[1].split(b"\n", 1)[0]
    blank = content.replace(b"-- digest: " + digest, b"-- digest:", 1)
    old_blank = blank.replace(b"-- coding-stats artifact", b"-- skill-stats artifact", 1)
    old_digest = hashlib.sha256(old_blank).hexdigest()
    return old_blank.replace(b"-- digest:", b"-- digest: " + old_digest.encode(), 1)


def _legacy_repo(tmp_path, repo, monkeypatch=None, old_origin=True):
    """A repo dogfooding the OLD layout: committed .skill-stats/ artifact,
    legacy workflow file, machine-local legacy DB with classified history.
    Without a monkeypatch the fixture run skips classification (tests that
    don't assert verdicts stay offline)."""
    commit_files(repo, "feature a", {"f.txt": "a1\n"})
    commit_files(repo, "feature b", {"g.txt": "b1\n"})
    if old_origin:
        run(repo, "remote", "add", "origin", "https://github.com/Dahep/skill-stats.git")
    legacy_db = tmp_path / "legacy.db"
    main(["init", str(repo), "--db", str(legacy_db), "--branch", "main"])
    extra = ["--no-classify"]
    if monkeypatch is not None:
        replies = [
            '{"k":"new","title":"feature a","about":"x","why":"y"}',
            '{"k":"new","title":"feature b","about":"x","why":"y"}',
        ]
        monkeypatch.setattr(
            "coding_stats.cli.Session", lambda model, sid=None: FakeSession(replies)
        )
        extra = []
    main(["update", "--db", str(legacy_db), "--no-serialize", "--no-report", *extra])
    conn = sqlite3.connect(legacy_db)
    conn.row_factory = sqlite3.Row
    store = repo / ".skill-stats"
    store.mkdir()
    (store / "skill-stats.sql").write_bytes(old_format_bytes(conn))
    conn.close()
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "skill-stats.yml").write_text(
        "env:\n  SKILL_STATS_BRANCH: 'main'\n", encoding="utf-8"
    )
    return legacy_db


def _rename(repo):
    run(repo, "remote", "set-url", "origin", "https://github.com/Dahep/coding-stats.git")


def _db_key(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT repo_key FROM repositories").fetchone()[0]
    finally:
        conn.close()


def _counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        commits = conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
        verdicts = conn.execute("SELECT COUNT(*) FROM commit_verdicts").fetchone()[0]
    finally:
        conn.close()
    return commits, verdicts


def _snapshot_db(source: Path, dest: Path) -> None:
    """A consistent copy of a (possibly WAL-mode) SQLite DB: the backup API
    materializes main-file + WAL content into `dest` — a raw file copy of a
    live WAL DB would trail whatever is not checkpointed yet."""
    import sqlite3 as _sq

    src = _sq.connect(source)
    try:
        dst = _sq.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def test_migrate_renamed_repo_reuses_db_and_relabels(tmp_path, repo, monkeypatch, capsys):
    # Pin canonical identity resolution BEFORE the fixture init: on machines
    # where the origin URL rewrites to an ssh alias, identity falls back to
    # the local: shim (normalize has its own tests). With the pin, the legacy
    # DB and artifact are keyed on the canonical pre-rename URL, so the
    # rename-relabel branch executes.
    monkeypatch.setattr(
        "coding_stats.identity.normalize_clone_url",
        lambda url, ssh_config=None: (
            "https://github.com/Dahep/" + url.rpartition("/")[2].removesuffix(".git")
        ),
    )
    legacy_db = _legacy_repo(tmp_path, repo, monkeypatch)
    _rename(repo)
    dest = tmp_path / "dest" / "coding-stats.db"
    monkeypatch.setattr("coding_stats.cli.Session", lambda model, sid=None: FakeSession([]))

    main(["migrate", str(repo), "--db", str(dest), "--legacy-db", str(legacy_db)])
    out = capsys.readouterr().out
    assert "seed: reuse-db" in out
    assert (
        "relabel repository key 'https://github.com/Dahep/skill-stats@main'"
        " -> 'https://github.com/Dahep/coding-stats@main' (rename)" in out
    )
    assert "migrated: " in out and "legacy files removed" in out
    assert _db_key(dest) == "https://github.com/Dahep/coding-stats@main"
    assert _counts(dest) == (2, 2)  # verdicts survived the migration
    assert artifact.verify(repo / ".coding-stats" / "coding-stats.sql") is None
    # the store holds only the SQL artifact (ADR-0006 clause 5)
    assert not (repo / ".skill-stats").exists()
    assert not (repo / ".github" / "workflows" / "skill-stats.yml").exists()


def test_migrate_without_db_rebuilds_from_artifact(tmp_path, repo, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))  # pin the implicit legacy-DB lookup
    monkeypatch.setattr(
        "coding_stats.identity.normalize_clone_url",
        lambda url, ssh_config=None: (
            "https://github.com/Dahep/" + url.rpartition("/")[2].removesuffix(".git")
        ),
    )
    _legacy_repo(tmp_path, repo, monkeypatch)
    _rename(repo)
    dest = tmp_path / "fresh-rebuild.db"
    monkeypatch.setattr("coding_stats.cli.Session", lambda model, sid=None: FakeSession([]))

    main(["migrate", str(repo), "--db", str(dest)])
    out = capsys.readouterr().out
    assert "seed: rebuild-artifact" in out
    assert "no legacy machine DB found" in out
    assert _counts(dest) == (2, 2)  # verdict rows came from the legacy artifact
    assert _db_key(dest) == "https://github.com/Dahep/coding-stats@main"
    assert artifact.verify(repo / ".coding-stats" / "coding-stats.sql") is None
    assert not (repo / ".skill-stats").exists()


def test_migrate_alias_spelled_key_is_kept_with_a_notice(tmp_path, repo, monkeypatch, capsys):
    """The dotfiles case: the legacy DB is keyed on a canonical-looking
    spelling; with no canonicalizable origin the resolved identity is the
    local: shim. Canonical -> shim keeps the stored key (warn at update)."""
    monkeypatch.setattr(
        "coding_stats.identity.normalize_clone_url",
        lambda url, ssh_config=None: (
            "https://github.com/Dahep/" + url.rpartition("/")[2].removesuffix(".git")
        ),
    )
    legacy_db = _legacy_repo(tmp_path, repo, monkeypatch)
    run(repo, "remote", "remove", "origin")  # no origin at all -> local: shim
    dest = tmp_path / "alias.db"
    monkeypatch.setattr("coding_stats.cli.Session", lambda model, sid=None: FakeSession([]))

    main(["migrate", str(repo), "--db", str(dest), "--legacy-db", str(legacy_db)])
    out = capsys.readouterr().out
    assert "the stored key 'https://github.com/Dahep/skill-stats@main' is kept" in out
    assert "relabel" not in out
    assert _db_key(dest) == "https://github.com/Dahep/skill-stats@main"
    assert _counts(dest) == (2, 2)
    assert artifact.verify(repo / ".coding-stats" / "coding-stats.sql") is None


def test_migrate_refuses_explicit_foreign_legacy_db(tmp_path, repo):
    _legacy_repo(tmp_path, repo)
    foreign = tmp_path / "foreign.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE repositories (id INTEGER PRIMARY KEY, repo_key TEXT)")
    conn.execute("INSERT INTO repositories (repo_key) VALUES ('https://github.com/other/x@main')")
    conn.commit()
    conn.close()
    dest = tmp_path / "never.db"

    with pytest.raises(SystemExit, match="do not correspond"):
        main(["migrate", str(repo), "--db", str(dest), "--legacy-db", str(foreign)])
    assert not dest.exists()
    assert (repo / ".skill-stats" / "skill-stats.sql").exists()  # untouched


def test_migrate_falls_back_to_fresh_on_corrupt_legacy_artifact(
    tmp_path, repo, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))  # pin the implicit legacy-DB lookup
    _legacy_repo(tmp_path, repo)
    art = repo / ".skill-stats" / "skill-stats.sql"
    data = art.read_bytes()
    art.write_bytes(data.replace(b"feature a", b"feather a", 1))
    dest = tmp_path / "corrupt.db"

    main(["migrate", str(repo), "--db", str(dest), "--no-classify"])
    out = capsys.readouterr().out
    assert "legacy artifact fails verification" in out
    assert "seed: fresh" in out
    assert _counts(dest)[0] == 2  # walked fresh
    assert artifact.verify(repo / ".coding-stats" / "coding-stats.sql") is None
    assert not (repo / ".skill-stats").exists()


def test_migrate_dry_run_changes_nothing(tmp_path, repo):
    legacy_db = _legacy_repo(tmp_path, repo)
    _rename(repo)
    dest = tmp_path / "dry.db"

    main(["migrate", str(repo), "--db", str(dest), "--legacy-db", str(legacy_db), "--dry-run"])
    assert not dest.exists()
    assert (repo / ".skill-stats" / "skill-stats.sql").exists()
    assert (repo / ".github" / "workflows" / "skill-stats.yml").exists()


def test_migrate_is_idempotent(tmp_path, repo, monkeypatch, capsys):
    legacy_db = _legacy_repo(tmp_path, repo, monkeypatch)
    _rename(repo)
    dest = tmp_path / "again.db"
    monkeypatch.setattr("coding_stats.cli.Session", lambda model, sid=None: FakeSession([]))
    main(["migrate", str(repo), "--db", str(dest), "--legacy-db", str(legacy_db)])
    commits, verdicts = _counts(dest)

    main(["migrate", str(repo), "--db", str(dest), "--legacy-db", str(legacy_db)])
    out = capsys.readouterr().out
    assert "already migrated" in out
    assert _counts(dest) == (commits, verdicts)


def test_migrate_ci_writes_new_workflow(tmp_path, repo, monkeypatch, capsys):
    legacy_db = _legacy_repo(tmp_path, repo, monkeypatch)
    _rename(repo)
    dest = tmp_path / "ci.db"
    monkeypatch.setattr("coding_stats.cli.Session", lambda model, sid=None: FakeSession([]))

    main(["migrate", str(repo), "--db", str(dest), "--legacy-db", str(legacy_db), "--ci"])
    out = capsys.readouterr().out
    assert ".github/workflows/coding-stats.yml: written" in out
    text = (repo / ".github" / "workflows" / "coding-stats.yml").read_text("utf-8")
    assert "Dahep/coding-stats@v1" in text  # the caller workflow adopts the actions
    assert not (repo / ".github" / "workflows" / "skill-stats.yml").exists()


def test_old_format_artifact_is_the_new_one_with_swapped_header(tmp_path):
    """The migration premise, pinned as a test: pre-rename artifacts are the
    current format with only the header tag changed — one replace apart.
    Ingestion (require_tag default) refuses them; migration reads them past
    the tag check on purpose."""
    from coding_stats.db import connect

    conn = connect(tmp_path / "x.db")
    conn.row_factory = sqlite3.Row
    data = artifact.serialize(conn)
    old = old_format_bytes(conn)
    assert artifact.parse_header(old) is None  # ingestion refuses the old marker
    header = artifact.parse_header(old, require_tag=False)
    assert header is not None
    assert artifact.verify_bytes(old) is None
    assert header.repository_key == artifact.parse_header(data).repository_key


def test_gather_rejects_old_format(tmp_path, repo):
    """The promised one-way format break: gather refuses a legacy artifact,
    even though its digest self-verifies."""
    from coding_stats.gather import GatherError
    from coding_stats.gather import gather as run_gather

    _legacy_repo(tmp_path, repo)
    union = tmp_path / "union.db"
    with pytest.raises(GatherError):
        run_gather(union, [repo / ".skill-stats" / "skill-stats.sql"])


def test_migrate_db_behind_artifact_seeds_from_artifact(tmp_path, repo, monkeypatch, capsys):
    """A machine-local DB that lags the committed artifact must not win:
    its migration would drop the artifact's newer verdicts. The artifact
    side wins (verdict rows travel inside it)."""
    monkeypatch.setattr(
        "coding_stats.identity.normalize_clone_url",
        lambda url, ssh_config=None: (
            "https://github.com/Dahep/" + url.rpartition("/")[2].removesuffix(".git")
        ),
    )
    _legacy_repo(tmp_path, repo, monkeypatch)
    stale = tmp_path / "stale.db"
    _snapshot_db(tmp_path / "legacy.db", stale)  # consistent WAL-safe copy
    commit_files(repo, "feature c", {"h.txt": "c1\n"})
    commit_files(repo, "feature d", {"i.txt": "d1\n"})
    replies = [
        '{"k":"new","title":"feature c","about":"x","why":"y"}',
        '{"k":"new","title":"feature d","about":"x","why":"y"}',
    ]
    monkeypatch.setattr("coding_stats.cli.Session", lambda model, sid=None: FakeSession(replies))
    main(["update", "--db", str(tmp_path / "legacy.db"), "--no-serialize", "--no-report"])
    monkeypatch.setattr("coding_stats.cli.Session", lambda model, sid=None: FakeSession([]))
    store = repo / ".skill-stats"
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    (store / "skill-stats.sql").write_bytes(old_format_bytes(conn))
    conn.close()
    dest = tmp_path / "behind.db"

    main(["migrate", str(repo), "--db", str(dest), "--legacy-db", str(stale)])
    out = capsys.readouterr().out
    assert "seed: rebuild-artifact" in out
    assert "classifies ahead" in out
    assert _counts(dest) == (4, 4)  # the artifact's newer verdicts, not the stale DB's two
    assert artifact.verify(repo / ".coding-stats" / "coding-stats.sql") is None


def test_migrate_refuses_to_overwrite_a_foreign_destination(tmp_path, repo, capsys):
    _legacy_repo(tmp_path, repo)
    dest = tmp_path / "dest.db"
    conn = sqlite3.connect(dest)
    conn.execute("CREATE TABLE repositories (id INTEGER PRIMARY KEY, repo_key TEXT)")
    conn.execute("CREATE TABLE commits (id INTEGER PRIMARY KEY, sha TEXT, walk_index INTEGER)")
    conn.execute("INSERT INTO repositories (repo_key) VALUES ('https://github.com/other/y@main')")
    conn.execute("INSERT INTO commits (sha, walk_index) VALUES ('abc', 1)")
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit, match="refusing to overwrite"):
        main(["migrate", str(repo), "--db", str(dest)])
    assert _db_key(dest) == "https://github.com/other/y@main"  # destination untouched
    assert (repo / ".skill-stats" / "skill-stats.sql").exists()


def test_migrate_fresh_refuses_an_existing_destination(tmp_path, repo):
    _legacy_repo(tmp_path, repo)
    dest = tmp_path / "occupied.db"
    conn = sqlite3.connect(dest)
    conn.execute("CREATE TABLE commits (id INTEGER PRIMARY KEY, sha TEXT, walk_index INTEGER)")
    conn.execute("INSERT INTO commits (sha, walk_index) VALUES ('abc', 1)")
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit, match="unknown content"):
        main(["migrate", str(repo), "--db", str(dest), "--fresh"])
    assert dest.exists()


def test_migrate_fresh_overrides_an_already_migrated_repo(tmp_path, repo, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))  # pin the implicit legacy-DB lookup
    _legacy_repo(tmp_path, repo, monkeypatch)
    dest = tmp_path / "first.db"
    monkeypatch.setattr("coding_stats.cli.Session", lambda model, sid=None: FakeSession([]))
    main(["migrate", str(repo), "--db", str(dest)])
    assert artifact.verify(repo / ".coding-stats" / "coding-stats.sql") is None

    monkeypatch.setattr("coding_stats.cli.Session", lambda model, sid=None: FakeSession([]))
    dest2 = tmp_path / "second.db"
    main(["migrate", str(repo), "--db", str(dest2), "--fresh", "--no-classify"])
    out = capsys.readouterr().out
    assert "seed: fresh" in out  # --fresh recomputes even though 'already' would apply
    assert artifact.verify(repo / ".coding-stats" / "coding-stats.sql") is None


def test_migrate_explicit_absent_legacy_db_is_an_error(tmp_path, repo):
    _legacy_repo(tmp_path, repo)
    with pytest.raises(SystemExit, match="does not exist"):
        main(["migrate", str(repo), "--legacy-db", str(tmp_path / "nothere.db"), "--dry-run"])


def test_migrate_default_destination_mirror_respects_home(tmp_path, repo, monkeypatch):
    """The documented layout mirror: a legacy DB at the default location and
    no --db migrates it to ~/.coding-stats/coding-stats.db (HOME-pinned)."""
    monkeypatch.setenv("HOME", str(tmp_path))  # pin the implicit lookup AND the mirror
    monkeypatch.setattr(
        "coding_stats.identity.normalize_clone_url",
        lambda url, ssh_config=None: (
            "https://github.com/Dahep/" + url.rpartition("/")[2].removesuffix(".git")
        ),
    )
    legacy_db = tmp_path / ".skill-stats" / "skill-stats.db"
    commit_files(repo, "feature a", {"f.txt": "a1\n"})
    run(repo, "remote", "add", "origin", "https://github.com/Dahep/skill-stats.git")
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: FakeSession(
            ['{"k":"new","title":"feature a","about":"x","why":"y"}']
        ),
    )
    legacy_db.parent.mkdir()
    main(["init", str(repo), "--db", str(legacy_db), "--branch", "main"])
    main(["update", "--db", str(legacy_db), "--no-serialize", "--no-report"])
    conn = sqlite3.connect(legacy_db)
    conn.row_factory = sqlite3.Row
    store = repo / ".skill-stats"
    store.mkdir()
    (store / "skill-stats.sql").write_bytes(old_format_bytes(conn))
    conn.close()
    # implicit both ways: legacy DB at the default location, destination at the mirror
    main(["migrate", str(repo), "--no-classify"])
    dest = tmp_path / ".coding-stats" / "coding-stats.db"
    assert dest.exists()
    assert _counts(dest) == (1, 1)
    assert artifact.verify(repo / ".coding-stats" / "coding-stats.sql") is None


def test_legacy_repo_fixture_cleans_new_format_store(tmp_path, repo, monkeypatch):
    """Guard the fixture's own assumption: no .coding-stats/ store may exist
    before migration runs, or detect would report 'already'."""
    _legacy_repo(tmp_path, repo, monkeypatch)
    assert not (repo / ".coding-stats").exists()
    assert artifact.verify(repo / ".skill-stats" / "skill-stats.sql") is None
