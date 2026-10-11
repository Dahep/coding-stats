"""Gather tests: artifact ingest into a union DB (ADR-0003 gather rules) —
deterministic ingest order, idempotence, last-wins upsert, slug-collision
refusal, non-verifying refusal. Artifacts are REAL: update wrote them."""

import sqlite3
from pathlib import Path

import pytest

from conftest import commit_files, run
from skill_stats.cli import main
from skill_stats.metrics import churn_per_feature, snapshot


class Scripted:
    """Scripted classifier; pops one reply per send."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.session_id = None

    def send(self, message: str) -> str:
        return self.replies.pop(0) if self.replies else '{"k":"cleanup","why":"w"}'


def _updated_repo(tmp_path, name, files, replies, branch="main", no_classify=False) -> Path:
    """A scratch repo whose artifact was written by a real update run."""
    repo = tmp_path / name
    repo.mkdir(parents=True)
    run(repo, "init", "-q", "-b", branch)
    run(repo, "config", "user.name", "T")
    run(repo, "config", "user.email", "t@t.io")
    run(repo, "config", "commit.gpgsign", "false")
    for msg, content in files:
        commit_files(repo, msg, content)
    db_path = str(tmp_path / f"{name}.db")
    main(["init", str(repo), "--branch", branch, "--db", db_path])
    import unittest.mock as mock

    with mock.patch("skill_stats.cli.Session", lambda model, sid=None: Scripted(list(replies))):
        flags = ["--no-detect", "--no-lineage", "--no-report"]
        if no_classify:
            flags.append("--no-classify")
        main(["update", "--db", db_path, *flags])
    return repo


def _rows(db_path, sql):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute(sql)]
    conn.close()
    return out


def test_gather_unions_two_repos_deterministically(tmp_path, monkeypatch):
    a = _updated_repo(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\na2\n"})],
        ['{"k":"new","title":"alpha module","about":"x","why":"y"}'],
    )
    b = _updated_repo(
        tmp_path,
        "beta-repo",
        [("feature two", {"b.txt": "b1\n"})],
        ['{"k":"new","title":"beta index","about":"x","why":"y"}'],
    )
    union = tmp_path / "union.db"
    # one source as repo checkout, one as explicit artifact file path
    main(["gather", "--db", str(union), str(a), str(b / ".skill-stats" / "skill-stats.sql")])

    repos = _rows(union, "SELECT * FROM repositories ORDER BY repo_key")
    assert [r["repo_key"] for r in repos] == ["local:alpha-repo@main", "local:beta-repo@main"]
    assert [r["slug"] for r in repos] == ["alpha-repo", "beta-repo"]
    commits = _rows(union, "SELECT repo_id, sha, added_lines, deleted_lines FROM commits")
    assert len(commits) == 2
    feats = _rows(union, "SELECT id, repo_id, live_lines FROM features ORDER BY id")
    assert [f["id"] for f in feats] == ["alpha-repo-1", "beta-repo-1"]  # no remapping
    churn = {fid: n for fid, _title, n in churn_per_feature(_connect(union))}
    assert churn["alpha-repo-1"] == 2 and churn["beta-repo-1"] == 1  # no contamination
    snap = snapshot(_connect(union))
    assert snap.total_features == 2 and snap.total_commits == 2
    meta = _rows(union, "SELECT * FROM union_artifacts ORDER BY repo_key")
    assert len(meta) == 2
    for m in meta:
        assert len(m["artifact_sha256"]) == 64
        assert m["covered_through"] and m["backfill_state"]

    # determinism: two fresh unions from the same sources carry identical
    # rows AND identical id assignment
    union2 = tmp_path / "union2.db"
    main(["gather", "--db", str(union2), str(a), str(b / ".skill-stats" / "skill-stats.sql")])
    for table in (
        "repositories",
        "commits",
        "commit_verdicts",
        "features",
        "commits_features",
        "fix_touches",
        "fix_targets",
        "fixes_features",
        "feature_line_samples",
        "union_artifacts",
    ):
        assert _rows(union, f"SELECT * FROM {table}") == _rows(union2, f"SELECT * FROM {table}"), (
            table
        )

    # re-gather into the same union: idempotent no-op (same rows, same ids)
    main(["gather", "--db", str(union), str(a), str(b)])
    for table in ("repositories", "commits", "features", "union_artifacts"):
        assert _rows(union, f"SELECT * FROM {table}") == _rows(union2, f"SELECT * FROM {table}"), (
            table
        )


def test_gather_upserts_newer_artifact_of_same_key(tmp_path):
    repo = _updated_repo(
        tmp_path,
        "solo-repo",
        [("feature one", {"a.txt": "a1\n"}), ("feature two", {"b.txt": "b1\n"})],
        [],
        no_classify=True,
    )
    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(repo)])
    assert _rows(union, "SELECT COUNT(*) c FROM commit_verdicts")[0]["c"] == 0
    first_digest = _rows(union, "SELECT artifact_sha256 d FROM union_artifacts")[0]["d"]

    # a newer update run classifies the backlog: same key, different artifact
    import unittest.mock as mock

    db_path = str(tmp_path / "solo-repo.db")
    with mock.patch(
        "skill_stats.cli.Session",
        lambda model, sid=None: Scripted(
            [
                '{"k":"new","title":"one","about":"x","why":"y"}',
                '{"k":"new","title":"two","about":"x","why":"y"}',
            ]
        ),
    ):
        main(["update", "--db", db_path, "--no-detect", "--no-lineage", "--no-report"])
    main(["gather", "--db", str(union), str(repo)])

    assert _rows(union, "SELECT COUNT(*) c FROM commit_verdicts")[0]["c"] == 2  # last-wins
    assert _rows(union, "SELECT COUNT(*) c FROM commits")[0]["c"] == 2  # no duplication
    assert _rows(union, "SELECT COUNT(*) c FROM repositories")[0]["c"] == 1
    assert _rows(union, "SELECT artifact_sha256 d FROM union_artifacts")[0]["d"] != first_digest


def test_gather_refuses_slug_collision(tmp_path):
    a = _updated_repo(
        tmp_path / "x",
        "dotfiles",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    b = _updated_repo(
        tmp_path / "y",
        "dotfiles",
        [("feature two", {"b.txt": "b1\n"})],
        ['{"k":"new","title":"two","about":"x","why":"y"}'],
        branch="dev",
    )
    union = tmp_path / "union.db"
    with pytest.raises(SystemExit) as excinfo:
        main(["gather", "--db", str(union), str(a), str(b)])
    msg = str(excinfo.value)
    assert "local:dotfiles@main" in msg and "local:dotfiles@dev" in msg  # offending keys named
    assert "dotfiles" in msg  # the colliding slug named
    assert not union.exists()  # refused before anything was written


def test_gather_refuses_non_verifying_artifact(tmp_path):
    a = _updated_repo(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    art = a / ".skill-stats" / "skill-stats.sql"
    art.write_bytes(art.read_bytes() + b"\n-- tampered\n")
    union = tmp_path / "union.db"
    with pytest.raises(SystemExit) as excinfo:
        main(["gather", "--db", str(union), str(a)])
    msg = str(excinfo.value)
    assert "alpha-repo" in msg and "digest mismatch" in msg
    assert not union.exists()  # nothing half-written


def test_gather_decodes_hdr_non_ascii_key(tmp_path):
    repo = _updated_repo(
        tmp_path,
        "café-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(repo)])
    repos = _rows(union, "SELECT repo_key, slug FROM repositories")
    # the non-ASCII key survives the \xNN header round-trip; the slug is
    # ASCII-sanitized by identity._slug (é -> '-')
    assert repos == [{"repo_key": "local:café-repo@main", "slug": "caf--repo"}]


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------- backfill merge rule + org report surface ---


def _classifying_repo(tmp_path, name, files, replies, fail_after=None):
    """update run whose classifier raises after N successful sends (Q13
    backlog semantics: the failing commit stays pending)."""
    import unittest.mock as mock

    repo = tmp_path / name
    repo.mkdir(parents=True)
    run(repo, "init", "-q", "-b", "main")
    run(repo, "config", "user.name", "T")
    run(repo, "config", "user.email", "t@t.io")
    run(repo, "config", "commit.gpgsign", "false")
    for msg, content in files:
        commit_files(repo, msg, content)
    db_path = str(tmp_path / f"{name}.db")
    main(["init", str(repo), "--db", db_path])
    sent = []

    class Flaky(Scripted):
        def send(self, message):
            if fail_after is not None and len(sent) >= fail_after:
                raise RuntimeError("opencode CLI failed")
            sent.append(message)
            return super().send(message)

    with mock.patch("skill_stats.cli.Session", lambda model, sid=None: Flaky(list(replies))):
        main(["update", "--db", db_path, "--no-detect", "--no-lineage", "--no-report"])
    return repo, db_path


def test_union_report_repo_column_and_org_branding(tmp_path):
    a = _updated_repo(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\na2\n"})],
        ['{"k":"new","title":"alpha module","about":"x","why":"y"}'],
    )
    b = _updated_repo(
        tmp_path,
        "beta-repo",
        [("feature two", {"b.txt": "b1\n"})],
        ['{"k":"new","title":"beta index","about":"x","why":"y"}'],
    )
    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(a), str(b)])
    conn = _connect(union)
    from skill_stats import htmlreport
    from skill_stats.report import text_report

    page = htmlreport.build(conn)
    assert "skill-stats report · 2 repos" in page  # org branding
    assert "<th>Repo</th>" in page  # REPO column in the ranking table
    assert "alpha-repo" in page and "beta-repo" in page
    text = text_report(conn)
    assert "#alpha-repo-1" in text and "#beta-repo-1" in text
    assert "alpha-module" not in text  # sanity: no stray remap
    conn.close()


def test_single_repo_report_renders_unchanged(tmp_path):
    a = _updated_repo(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"alpha module","about":"x","why":"y"}'],
    )
    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(a)])
    conn = _connect(union)
    from skill_stats import htmlreport
    from skill_stats.report import text_report

    page = htmlreport.build(conn)
    text = text_report(conn)
    assert "<th>Repo</th>" not in page  # single-repo: zero visual diff
    assert "repos" not in page.split("<title>")[1].split("</title>")[0]
    assert "skill-stats report · alpha-repo" in page  # slug branding when single
    assert "alpha-repo " not in text.split("top 15 features by fixes:")[1].splitlines()[1]
    conn.close()


def test_union_sampling_complete_is_least_complete(tmp_path):
    import unittest.mock as mock

    # backlog repo: one commit classified (feature exists -> samples), one
    # left pending -> its backfill_state stays "pending"
    a, a_db = _classifying_repo(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"}), ("later", {"a.txt": "a1\na2\n"})],
        ['{"k":"new","title":"alpha module","about":"x","why":"y"}'],
        fail_after=1,
    )
    b = _updated_repo(
        tmp_path,
        "beta-repo",
        [("feature two", {"b.txt": "b1\n"})],
        ['{"k":"new","title":"beta index","about":"x","why":"y"}'],
    )
    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(a), str(b)])
    conn = _connect(union)
    from skill_stats import htmlreport

    page = htmlreport.build(conn)
    assert "Feature size over time" in page
    assert "sampling incomplete" in page.lower()  # least-complete rule
    conn.close()

    # clear the backlog -> re-gather -> the union is complete
    with mock.patch(
        "skill_stats.cli.Session",
        lambda model, sid=None: Scripted(['{"k":"new","title":"later","about":"x","why":"y"}']),
    ):
        main(["update", "--db", a_db, "--no-detect", "--no-lineage", "--no-report"])
    main(["gather", "--db", str(union), str(a), str(b)])
    conn = _connect(union)
    page = htmlreport.build(conn)
    assert "sampling incomplete" not in page.lower()
    conn.close()


# ------------------------- ownership preflight + input error boundaries -----


def test_preflight_rejects_multi_repository_artifact(tmp_path):
    from test_artifact_security import _rechecksum

    repo = _updated_repo(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    art = repo / ".skill-stats" / "skill-stats.sql"
    stray = (
        "\nINSERT INTO repositories (id, repo_key, slug, branch, clone_url, added_at)"
        " VALUES (99, 'local:other@main', 'other', 'main', NULL, '');\n"
    )
    art.write_bytes(_rechecksum(art.read_bytes() + stray.encode()))
    union = tmp_path / "union.db"
    with pytest.raises(SystemExit) as excinfo:
        main(["gather", "--db", str(union), str(repo)])
    msg = str(excinfo.value)
    assert "alpha-repo" in msg  # source named
    assert "repositories" in msg and "1" in msg  # the anomaly named
    assert not union.exists()


def test_preflight_rejects_stray_foreign_commit_id(tmp_path):
    from test_artifact_security import _rechecksum

    repo = _updated_repo(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    art = repo / ".skill-stats" / "skill-stats.sql"
    stray = (
        "\nINSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
        " model, run_id, classified_at) VALUES (999, 'fix', 'r', 'raw', 'm', NULL, 'd');\n"
    )
    art.write_bytes(_rechecksum(art.read_bytes() + stray.encode()))
    union = tmp_path / "union.db"
    with pytest.raises(SystemExit) as excinfo:
        main(["gather", "--db", str(union), str(repo)])
    msg = str(excinfo.value)
    assert "commit_verdicts" in msg  # the stray edge named
    assert not union.exists()


def test_gather_dest_not_skill_stats_database(tmp_path):
    repo = _updated_repo(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    dest = tmp_path / "other.db"
    conn = sqlite3.connect(dest)
    conn.execute("CREATE TABLE other_tool(x)")
    conn.commit()
    conn.close()
    with pytest.raises(SystemExit) as excinfo:
        main(["gather", "--db", str(dest), str(repo)])
    msg = str(excinfo.value)
    assert str(dest) in msg  # destination named
    assert "not a skill-stats database" in msg  # actionable hint


def test_gather_malformed_artifact_clean_error(tmp_path):
    from test_artifact_security import _rechecksum

    repo = _updated_repo(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    art = repo / ".skill-stats" / "skill-stats.sql"
    art.write_bytes(_rechecksum(art.read_bytes() + b"\nINSERT INTO features (id\n"))
    union = tmp_path / "union.db"
    with pytest.raises(SystemExit) as excinfo:
        main(["gather", "--db", str(union), str(repo)])
    assert "alpha-repo" in str(excinfo.value)  # clean error naming the source
    assert not union.exists()

    art.write_bytes(b"")  # empty file
    with pytest.raises(SystemExit) as excinfo:
        main(["gather", "--db", str(union), str(repo)])
    assert "alpha-repo" in str(excinfo.value)
    assert not union.exists()
