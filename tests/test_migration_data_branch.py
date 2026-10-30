"""Migration to the Data-branch layout (ADR-0006 clause 6, ticket #26):
verify the committed ADR-0003 store, refuse damage, re-prefix ids, create
the orphan coding-stats/<branch> holding only the artifact, push only it,
print the removal-PR steps; idempotent."""

import sqlite3

import pytest

from coding_stats import artifact, db, identity
from coding_stats.cli import main
from conftest import commit_files, run, store_dir
from test_seeding import _mat_rows


def _committed_store(repo, replies):
    """A repo whose .coding-stats/ store is COMMITTED on the Target branch
    (the ADR-0003 layout), written with the engine's own pieces."""
    from coding_stats import gitwalk as _gw
    from coding_stats.classify import create_feature

    conn = db.connect(str(repo.parent / "m.db"))
    identity.register_repository(conn, repo, "main")
    _gw.walk(repo, conn, "main")
    for c in conn.execute("SELECT id FROM commits ORDER BY walk_index").fetchall():
        fid = create_feature(conn, 1, "feature", "", "2026-01-01T00:00:00Z")
        conn.execute(
            "INSERT INTO commits_features (commit_id, feature_id, role) VALUES (?, ?, 'defines')",
            (c["id"], fid),
        )
        conn.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
            " model, classified_at) VALUES (?, 'feature', 'committed', '{}', 'm', "
            "datetime('now'))",
            (c["id"],),
        )
    data = artifact.serialize(conn)
    conn.close()
    store = repo / ".coding-stats"
    store.mkdir(parents=True, exist_ok=True)
    (store / "coding-stats.sql").write_bytes(data)
    run(repo, "add", "-A", "--", ".coding-stats")
    run(repo, "commit", "-m", "coding-stats artifact update (auto)")
    return "m.db"


def test_migration_moves_the_committed_store_to_the_data_branch(
    tmp_path, repo, capsys, monkeypatch
):
    """End-to-end: ids re-prefixed; the orphan data branch created and
    pushed as its only change; the Target branch untouched by migration."""
    commit_files(repo, "feature one", {"a.txt": "alpha\n"})
    commit_files(repo, "feature two", {"b.txt": "beta\n"})
    _committed_store(
        repo,
        [
            '{"k":"new","title":"one","about":"","why":"y"}',
            '{"k":"new","title":"two","about":"","why":"y"}',
        ],
    )
    target_head = subprocess_head(repo)

    main(["migrate", str(repo), "--data-branch"])

    out = capsys.readouterr().out
    assert "landing" in out
    # data branch now holds ONLY the artifact and verifies
    art = store_dir(repo, "main") / "coding-stats.sql"
    assert artifact.verify(art) is None
    ids = _mat_rows(art.read_text(encoding="utf-8"), "SELECT id FROM features")
    assert ids == [("repo@main-1",), ("repo@main-2",)]  # minted with the branch prefix
    # the landing only pushed data-branch refs
    assert subprocess_head(repo) == target_head  # Target head untouched
    conn = sqlite3.connect(":memory:")
    conn.executescript(art.read_text(encoding="utf-8"))
    assert (
        conn.execute("SELECT COUNT(*) FROM commit_verdicts").fetchone()[0] == 2
    )  # classified history preserved, nothing re-paid
    conn.close()


def test_migration_result_header_carries_the_migrated_step(tmp_path, repo, capsys):
    """#31: the schema stamp travels into the artifact header. derive-and-land
    without the stamp would emit schema-step 0 while the content is the fully
    migrated layout — and a CI restore (materialize + stamp + connect) would
    then re-run step 1 against a step-n DB and crash on the absent
    settings.repo."""
    commit_files(repo, "feature one", {"a.txt": "alpha\n"})
    legacy = repo / ".coding-stats" / "coding-stats.sql"
    _committed_store(repo, ['{"k":"new","title":"o","about":"","why":"y"}'])
    legacy_step = artifact.parse_header(legacy.read_bytes()).schema_step
    assert legacy_step > 0  # the fixture must stamp too, or the compare is vacuous

    main(["migrate", str(repo), "--data-branch"])
    capsys.readouterr()

    data = (store_dir(repo, "main") / "coding-stats.sql").read_bytes()
    assert artifact.parse_header(data).schema_step == legacy_step
    # the exact CI restore path: materialize + stamp + connect must not
    # re-run step 1 (which demands settings.repo — absent in a pure artifact)
    assert artifact.verify_bytes(data) is None
    work = tmp_path / "restore.sql"
    work.write_bytes(b"")
    raw = sqlite3.connect(work)
    raw.row_factory = sqlite3.Row
    try:
        artifact.materialize(raw, data)
        artifact.stamp_schema_steps(raw, data)
    finally:
        raw.close()
    db.connect(work)  # RuntimeError under the bug: header said 0, data said n


def subprocess_head(repo) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_migration_is_idempotent(tmp_path, repo, capsys, monkeypatch):
    commit_files(repo, "one", {"a.txt": "a\n"})
    _committed_store(repo, ['{"k":"new","title":"o","about":"","why":"y"}'])
    main(["migrate", str(repo), "--data-branch"])
    capsys.readouterr()
    main(["migrate", str(repo), "--data-branch"])
    out = capsys.readouterr().out
    assert "already migrated" in out


def test_migration_refuses_non_verifying_store(tmp_path, repo, capsys, monkeypatch):
    commit_files(repo, "one", {"a.txt": "a\n"})
    _committed_store(repo, ['{"k":"new","title":"o","about":"","why":"y"}'])
    art = repo / ".coding-stats" / "coding-stats.sql"
    art.write_bytes(art.read_bytes() + b"\ntamper\n")
    run(repo, "add", "-A", "--", ".coding-stats")
    run(repo, "commit", "-m", "tampered")
    with pytest.raises(SystemExit) as excinfo:
        main(["migrate", str(repo), "--data-branch"])
    assert "does not verify" in str(excinfo.value)


def test_migration_prints_removal_pr_steps(tmp_path, repo, capsys, monkeypatch):
    commit_files(repo, "one", {"a.txt": "a\n"})
    _committed_store(repo, ['{"k":"new","title":"o","about":"","why":"y"}'])
    main(["migrate", str(repo), "--data-branch"])
    out = capsys.readouterr().out
    assert "git rm -r .coding-stats" in out
    assert "git rm -r .github/workflows" not in out  # no workflow: nothing else to remove


def test_migration_never_offers_to_remove_an_actions_based_workflow(tmp_path, repo, capsys):
    """The PR's declared guarantee pinned: an ACTIONS-BASED caller workflow
    (the shape every adopter writes) is never offered for deletion — the
    detection reads the Target branch's COMMITTED workflow (so the fixture
    commits what it writes)."""
    from coding_stats.ci import render_workflow

    commit_files(repo, "one", {"a.txt": "a\n"})
    (repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (repo / ".github" / "workflows" / "coding-stats.yml").write_text(
        render_workflow("main"), encoding="utf-8"
    )
    run(repo, "add", "-A", "--", ".github")
    run(repo, "commit", "-m", "adopter workflow")
    _committed_store(repo, [])
    main(["migrate", str(repo), "--data-branch"])
    out = capsys.readouterr().out
    assert "git rm -r .coding-stats" in out
    assert ".github/workflows" not in out.split("next steps", 1)[1]


def test_migration_offers_to_remove_the_old_rendered_scaffold(tmp_path, repo, capsys):
    """The old rendered scaffold (uv tool install) goes WITH the store: its
    git-rm line appears — staged, so the printed list is safe to run
    verbatim."""
    commit_files(repo, "one", {"a.txt": "a\n"})
    (repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (repo / ".github" / "workflows" / "coding-stats.yml").write_text(
        "# rendered scaffold\n- run: uv tool install 'coding-stats @ git+x'\n",
        encoding="utf-8",
    )
    run(repo, "add", "-A", "--", ".github")
    run(repo, "commit", "-m", "old scaffold")
    _committed_store(repo, [])
    main(["migrate", str(repo), "--data-branch"])
    out = capsys.readouterr().out
    assert "git rm -r .github/workflows/coding-stats.yml" in out
