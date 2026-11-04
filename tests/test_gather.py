"""Gather tests: artifact ingest into a union DB (ADR-0003 gather rules) —
deterministic ingest order, idempotence, last-wins upsert, slug-collision
refusal, non-verifying refusal. Artifacts are REAL: update wrote them."""

import sqlite3
import subprocess
from pathlib import Path

import pytest

from coding_stats import artifact
from coding_stats.cli import main
from coding_stats.metrics import churn_per_feature, snapshot
from conftest import add_feature, commit_files, make_db, rechecksum, run, store_worktree


class Scripted:
    """Scripted classifier; pops one reply per send."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.session_id = None

    def send(self, message: str) -> str:
        return self.replies.pop(0) if self.replies else '{"k":"cleanup","why":"w"}'


def _repo_with_artifact(
    tmp_path,
    name,
    files,
    replies,
    branch="main",
    no_classify=False,
    origin=None,
    fail_after=None,
) -> Path:
    """Shared scratch-repo factory: builds the repo and runs a real update to
    produce its artifact. `fail_after` makes the classifier raise after N
    sends (the Q13 backlog case); `no_classify` skips classification."""
    repo = tmp_path / name
    repo.mkdir(parents=True)
    run(repo, "init", "-q", "-b", branch)
    if origin:
        run(repo, "remote", "add", "origin", origin)
    run(repo, "config", "user.name", "T")
    run(repo, "config", "user.email", "t@t.io")
    run(repo, "config", "commit.gpgsign", "false")
    for msg, content in files:
        commit_files(repo, msg, content)
    db_path = str(tmp_path / f"{name}.db")
    main(["init", str(repo), "--branch", branch, "--db", db_path])
    import unittest.mock as mock

    sent: list[str] = []

    class Session(Scripted):
        def send(self, message):
            if fail_after is not None and len(sent) >= fail_after:
                raise RuntimeError("opencode CLI failed")
            sent.append(message)
            return super().send(message)

    with mock.patch("coding_stats.cli.Session", lambda model, sid=None: Session(list(replies))):
        flags = ["--no-detect", "--no-lineage"]
        if no_classify:
            flags.append("--no-classify")
        main(["update", "--db", db_path, *flags])
    return store_worktree(repo, branch)  # the Data-branch checkout gathers as a source


def _rows(db_path, sql):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute(sql)]
    conn.close()
    return out


def _canonical(db_path, table, exclude=()):
    """Ordered canonical projection for determinism comparisons (a fixed
    ORDER BY — unordered SELECT * would only compare as sets)."""
    rows = _rows(db_path, f"SELECT * FROM {table} ORDER BY rowid")
    for r in rows:
        for key in exclude:
            r.pop(key, None)
    return rows


def test_gather_unions_two_repos_deterministically(tmp_path, monkeypatch):
    a = _repo_with_artifact(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\na2\n"})],
        ['{"k":"new","title":"alpha module","about":"x","why":"y"}'],
    )
    b = _repo_with_artifact(
        tmp_path,
        "beta-repo",
        [("feature two", {"b.txt": "b1\n"})],
        ['{"k":"new","title":"beta index","about":"x","why":"y"}'],
    )
    union = tmp_path / "union.db"
    # one source as repo checkout, one as explicit artifact file path
    main(["gather", "--db", str(union), str(a), str(b / ".coding-stats" / "coding-stats.sql")])

    repos = _rows(union, "SELECT * FROM repositories ORDER BY repo_key")
    assert [r["repo_key"] for r in repos] == ["local:alpha-repo@main", "local:beta-repo@main"]
    assert [r["slug"] for r in repos] == ["alpha-repo@main", "beta-repo@main"]
    commits = _rows(union, "SELECT repo_id, sha, added_lines, deleted_lines FROM commits")
    assert len(commits) == 2
    feats = _rows(union, "SELECT id, repo_id, live_lines FROM features ORDER BY id")
    assert [f["id"] for f in feats] == ["alpha-repo@main-1", "beta-repo@main-1"]  # no remapping
    churn = {fid: n for fid, _title, n in churn_per_feature(_connect(union))}
    assert churn["alpha-repo@main-1"] == 2 and churn["beta-repo@main-1"] == 1  # no contamination
    snap = snapshot(_connect(union))
    assert snap.total_features == 2 and snap.total_commits == 2
    meta = _rows(union, "SELECT * FROM union_artifacts ORDER BY repo_key")
    assert len(meta) == 2
    for m in meta:
        assert len(m["artifact_sha256"]) == 64
        assert m["covered_through"] and m["backfill_state"]

    # determinism: two fresh unions of the same artifacts carry identical
    # rows AND identical id assignment (ordered canonical projection). The
    # second union crosses source TYPES for the same artifacts (explicit file
    # vs checkout dir): rows match exactly except union_artifacts.source_path
    # — intentional provenance of where each artifact was read from, which
    # differs by construction and is excluded on that basis.
    union2 = tmp_path / "union2.db"
    main(["gather", "--db", str(union2), str(a / ".coding-stats" / "coding-stats.sql"), str(b)])
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
        exclude = ("source_path",) if table == "union_artifacts" else ()
        assert _canonical(union, table, exclude) == _canonical(union2, table, exclude), table

    # re-gather into the same union: idempotent no-op (same rows, same ids)
    main(["gather", "--db", str(union), str(a), str(b)])
    for table in ("repositories", "commits", "features", "union_artifacts"):
        exclude = ("source_path",) if table == "union_artifacts" else ()
        assert _canonical(union, table, exclude) == _canonical(union2, table, exclude), table


def test_gather_upserts_newer_artifact_of_same_key(tmp_path):
    repo = _repo_with_artifact(
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
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            [
                '{"k":"new","title":"one","about":"x","why":"y"}',
                '{"k":"new","title":"two","about":"x","why":"y"}',
            ]
        ),
    ):
        main(["update", "--db", db_path, "--no-detect", "--no-lineage"])
    main(["gather", "--db", str(union), str(repo)])

    assert _rows(union, "SELECT COUNT(*) c FROM commit_verdicts")[0]["c"] == 2  # last-wins
    assert _rows(union, "SELECT COUNT(*) c FROM commits")[0]["c"] == 2  # no duplication
    assert _rows(union, "SELECT COUNT(*) c FROM repositories")[0]["c"] == 1
    assert _rows(union, "SELECT artifact_sha256 d FROM union_artifacts")[0]["d"] != first_digest


def test_gather_refuses_slug_collision(tmp_path):
    """The slug-collision refusal lives at the {repo}@{branch} slug: two
    different hosts publishing the same owner/repo and branch mint colliding
    feature-id prefixes and still fail loudly. (A same-repo/two-branch pair
    is legal now — see test_gather_two_data_branches_union_deterministically.)"""
    a = _repo_with_artifact(
        tmp_path / "x",
        "dotfiles",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
        origin="https://github.com/dahep/dotfiles.git",
    )
    b = _repo_with_artifact(
        tmp_path / "y",
        "dotfiles",
        [("feature two", {"b.txt": "b1\n"})],
        ['{"k":"new","title":"two","about":"x","why":"y"}'],
        origin="https://gitlab.com/dahep/dotfiles.git",
    )
    union = tmp_path / "union.db"
    with pytest.raises(SystemExit) as excinfo:
        main(["gather", "--db", str(union), str(a), str(b)])
    msg = str(excinfo.value)
    assert "https://github.com/dahep/dotfiles@main" in msg
    assert "https://gitlab.com/dahep/dotfiles@main" in msg  # offending keys named
    assert "dahep-dotfiles@main" in msg  # the colliding slug named
    assert not union.exists()  # refused before anything was written


def test_gather_refuses_non_verifying_artifact(tmp_path):
    a = _repo_with_artifact(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    art = a / ".coding-stats" / "coding-stats.sql"
    art.write_bytes(art.read_bytes() + b"\n-- tampered\n")
    union = tmp_path / "union.db"
    with pytest.raises(SystemExit) as excinfo:
        main(["gather", "--db", str(union), str(a)])
    msg = str(excinfo.value)
    assert "alpha-repo" in msg and "digest mismatch" in msg
    assert not union.exists()  # nothing half-written


def test_gather_decodes_hdr_non_ascii_key(tmp_path):
    repo = _repo_with_artifact(
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
    assert repos == [{"repo_key": "local:café-repo@main", "slug": "caf--repo@main"}]


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------- backfill merge rule + org report surface ---


def test_union_report_repo_column_and_org_branding(tmp_path):
    a = _repo_with_artifact(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\na2\n"})],
        ['{"k":"new","title":"alpha module","about":"x","why":"y"}'],
    )
    b = _repo_with_artifact(
        tmp_path,
        "beta-repo",
        [("feature two", {"b.txt": "b1\n"})],
        ['{"k":"new","title":"beta index","about":"x","why":"y"}'],
    )
    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(a), str(b)])
    conn = _connect(union)
    from coding_stats import htmlreport
    from coding_stats.report import text_report

    page = htmlreport.build(conn)
    assert "coding-stats report · 2 repos" in page  # org branding
    assert "<th>Repo</th>" in page  # REPO column in the ranking table
    assert "alpha-repo" in page and "beta-repo" in page
    text = text_report(conn)
    assert "#alpha-repo@main-1" in text and "#beta-repo@main-1" in text
    assert "alpha-module" not in text  # sanity: no stray remap
    conn.close()


def test_single_repo_report_renders_unchanged(tmp_path):
    a = _repo_with_artifact(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"alpha module","about":"x","why":"y"}'],
    )
    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(a)])
    conn = _connect(union)
    from coding_stats import htmlreport
    from coding_stats.report import text_report

    page = htmlreport.build(conn)
    text = text_report(conn)
    assert "<th>Repo</th>" not in page  # single-repo: zero visual diff
    assert "repos" not in page.split("<title>")[1].split("</title>")[0]
    assert "coding-stats report · alpha-repo" in page  # slug branding when single
    assert "alpha-repo " not in text.split("top 15 features by fix events:")[1].splitlines()[1]
    conn.close()


def test_union_sampling_complete_is_least_complete(tmp_path):
    import unittest.mock as mock

    # backlog repo: one commit classified (feature exists -> samples), one
    # left pending -> its backfill_state stays "pending"
    a = _repo_with_artifact(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"}), ("later", {"a.txt": "a1\na2\n"})],
        ['{"k":"new","title":"alpha module","about":"x","why":"y"}'],
        fail_after=1,
    )
    a_db = str(tmp_path / "alpha-repo.db")
    b = _repo_with_artifact(
        tmp_path,
        "beta-repo",
        [("feature two", {"b.txt": "b1\n"})],
        ['{"k":"new","title":"beta index","about":"x","why":"y"}'],
    )
    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(a), str(b)])
    conn = _connect(union)
    from coding_stats import htmlreport

    page = htmlreport.build(conn)
    assert "Feature size over time" in page
    assert "sampling incomplete" in page.lower()  # least-complete rule
    conn.close()

    # clear the backlog -> re-gather -> the union is complete
    with mock.patch(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(['{"k":"new","title":"later","about":"x","why":"y"}']),
    ):
        main(["update", "--db", a_db, "--no-detect", "--no-lineage"])
    main(["gather", "--db", str(union), str(a), str(b)])
    conn = _connect(union)
    page = htmlreport.build(conn)
    assert "sampling incomplete" not in page.lower()
    conn.close()


# ------------------------- ownership preflight + input error boundaries -----


def test_preflight_rejects_multi_repository_artifact(tmp_path):
    repo = _repo_with_artifact(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    art = repo / ".coding-stats" / "coding-stats.sql"
    stray = (
        "\nINSERT INTO repositories (id, repo_key, slug, branch, clone_url, added_at)"
        " VALUES (99, 'local:other@main', 'other', 'main', NULL, '');\n"
    )
    art.write_bytes(rechecksum(art.read_bytes() + stray.encode()))
    union = tmp_path / "union.db"
    with pytest.raises(SystemExit) as excinfo:
        main(["gather", "--db", str(union), str(repo)])
    msg = str(excinfo.value)
    assert "alpha-repo" in msg  # source named
    assert "repositories" in msg and "1" in msg  # the anomaly named
    assert not union.exists()


def test_preflight_rejects_stray_foreign_commit_id(tmp_path):
    repo = _repo_with_artifact(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    art = repo / ".coding-stats" / "coding-stats.sql"
    stray = (
        "\nINSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
        " model, run_id, classified_at) VALUES (999, 'fix', 'r', 'raw', 'm', NULL, 'd');\n"
    )
    art.write_bytes(rechecksum(art.read_bytes() + stray.encode()))
    union = tmp_path / "union.db"
    with pytest.raises(SystemExit) as excinfo:
        main(["gather", "--db", str(union), str(repo)])
    msg = str(excinfo.value)
    assert "commit_verdicts" in msg  # the stray edge named
    assert not union.exists()


def test_gather_dest_not_coding_stats_database(tmp_path):
    repo = _repo_with_artifact(
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
    assert "not a coding-stats database" in msg  # actionable hint


def test_gather_malformed_artifact_clean_error(tmp_path):
    repo = _repo_with_artifact(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    art = repo / ".coding-stats" / "coding-stats.sql"
    art.write_bytes(rechecksum(art.read_bytes() + b"\nINSERT INTO features (id\n"))
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


# ------------------------- union chart, links, and branding correctness -----


def _crafted_union(path):
    """Union DB with two repositories sharing one SHA (a fork) and staggered
    sample points — crafted directly in the union shape."""
    from coding_stats.db import connect

    conn = connect(path)
    conn.executescript(
        """
        INSERT INTO repositories (id, repo_key, slug, branch, clone_url, added_at)
          VALUES (1, 'local:a@main', 'a', 'main', NULL, ''),
                 (2, 'local:b@main', 'b', 'dev', NULL, '');
        INSERT INTO commits (id, repo_id, sha, tree_sha, committed_at, author_name, title,
                             message, walk_index, kind, diff, added_lines, deleted_lines,
                             churn_lines)
          VALUES (1, 1, 'x40', 't', '2026-01-01T00:00:00Z', 't', 'one', 'm', 1,
                  'normal', '', 1, 0, 1),
                 (2, 2, 'x40', 't', '2026-01-01T00:00:00Z', 't', 'one', 'm', 1,
                  'normal', '', 1, 0, 1),
                 (3, 1, 'y40', 't', '2026-01-02T00:00:00Z', 't', 'two', 'm', 2,
                  'normal', '', 1, 0, 1),
                 (4, 2, 'z40', 't', '2026-01-03T00:00:00Z', 't', 'three', 'm', 2,
                  'normal', '', 1, 0, 1);
        INSERT INTO features (id, title, about, title_history_json, created_at, repo_id, live_lines)
          VALUES ('a@main-1', 'alpha', '', '[]', '2026-01-01T00:00:00Z', 1, 8),
                 ('b@main-1', 'beta', '', '[]', '2026-01-01T00:00:00Z', 2, 4);
        INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines)
          VALUES ('a@main-1', 'x40', 5), ('b@main-1', 'x40', 7),
                 ('a@main-1', 'y40', 8), ('b@main-1', 'z40', 4);
        """
    )
    conn.commit()
    return conn


def test_union_chart_repo_scoped_totals_and_staggered_lines(tmp_path):
    from coding_stats.htmlreport import _size_series

    conn = _crafted_union(tmp_path / "union.db")
    labels, series, total = _size_series(conn)
    assert total[1] == [12.0, 8.0, 4.0]  # fork-shared SHA counted once per row, not doubled
    assert dict((name, vals) for name, vals, _c in series)["#a@main-1 alpha"] == [5.0, 8.0, None]
    assert dict((name, vals) for name, vals, _c in series)["#b@main-1 beta"] == [7.0, None, 4.0]
    # each line spans only its OWN sample dates (staggered sampling invents
    # no zeros); the axis is chronological (committed_at)
    assert labels == ["2026-01-01", "2026-01-02", "2026-01-03"]
    conn.close()


def test_union_chart_single_feature_regression(tmp_path):
    from coding_stats.htmlreport import _size_series

    conn = _crafted_union(tmp_path / "union.db")
    conn.execute("DELETE FROM feature_line_samples WHERE feature_id = 'b@main-1'")
    conn.execute("DELETE FROM features WHERE id = 'b@main-1'")
    conn.commit()
    labels, series, total = _size_series(conn)
    assert [vals for _n, vals, _c in series] == [[5.0, 8.0]]
    assert total[1] == [5.0, 8.0]  # points with no remaining rows vanish
    assert labels == ["2026-01-01", "2026-01-02"]
    conn.close()


def test_union_report_links_by_feature_repository(tmp_path):
    a = _repo_with_artifact(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
        origin="git@github.com:dahep/alpha.git",
    )
    b = _repo_with_artifact(
        tmp_path,
        "beta-repo",
        [("feature two", {"b.txt": "b1\n"})],
        ['{"k":"new","title":"two","about":"x","why":"y"}'],
        origin="https://github.com/beta-org/beta-repo.git",
    )
    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(a), str(b)])
    conn = _connect(union)
    from coding_stats import htmlreport

    page = htmlreport.build(conn)
    conn.close()
    assert "/dahep/alpha/commit/" in page  # per-feature link domains
    assert "/beta-org/beta-repo/commit/" in page


def test_machine_db_link_falls_back_to_settings_repo(tmp_path):
    repo = _repo_with_artifact(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    # the DB row is a shim (no clone_url) but settings.repo's origin resolves
    run(repo, "remote", "add", "origin", "git@github.com:dahep/alpha.git")
    db_path = tmp_path / "alpha-repo.db"
    conn = _connect(db_path)
    from coding_stats import htmlreport

    page = htmlreport.build(conn)
    conn.close()
    assert "/dahep/alpha/commit/" in page  # fallback via settings.repo path


def test_union_branch_branding(tmp_path):
    a = _repo_with_artifact(
        tmp_path,
        "alpha-repo",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
        branch="dev",
    )
    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(a)])
    conn = _connect(union)
    from coding_stats import htmlreport

    page = htmlreport.build(conn)
    conn.close()
    # the {repo}@{branch} slug brands the lone repo's Target branch itself
    # (not a 'main' default) — and the parenthesised branch tag is never
    # repeated next to it
    assert "alpha-repo@dev" in page
    assert "(main)" not in page
    assert "(dev)" not in page
    assert "alpha-repo@dev (dev)" not in page

    b = _repo_with_artifact(
        tmp_path,
        "beta-repo",
        [("feature two", {"b.txt": "b1\n"})],
        ['{"k":"new","title":"two","about":"x","why":"y"}'],
        branch="release",
    )
    main(["gather", "--db", str(union), str(a), str(b)])
    conn = _connect(union)
    page = htmlreport.build(conn)
    conn.close()
    assert "(dev)" not in page and "(release)" not in page and "(main)" not in page


# ------------------------------------ canonical backfill of shim-keyed rows --


def test_gather_adopts_canonical_key_for_shim_artifact(tmp_path):
    # artifact produced WITHOUT origin -> shim key; the checkout resolves a
    # canonical identity at gather time -> the union adopts it and rewrites
    # feature-id prefixes (local numbers preserved)
    repo = _repo_with_artifact(
        tmp_path,
        "dotfiles",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    art = repo / ".coding-stats" / "coding-stats.sql"
    before = art.read_bytes()
    run(repo, "remote", "add", "origin", "git@github.com:dahep/dotfiles.git")
    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(repo)])
    repos = _rows(union, "SELECT repo_key, slug, clone_url FROM repositories")
    assert repos == [
        {
            "repo_key": "https://github.com/dahep/dotfiles@main",
            "slug": "dahep-dotfiles@main",
            "clone_url": "https://github.com/dahep/dotfiles",
        }
    ]
    assert [f["id"] for f in _rows(union, "SELECT id FROM features ORDER BY id")] == [
        "dahep-dotfiles@main-1"
    ]
    assert art.read_bytes() == before  # union-only normalization: store untouched
    from coding_stats import htmlreport

    assert "dahep-dotfiles@main-1" in htmlreport.build(_connect(union))


def test_gather_keeps_shim_when_no_origin_resolves(tmp_path):
    repo = _repo_with_artifact(
        tmp_path,
        "dotfiles",
        [("feature one", {"a.txt": "a1\n"})],
        ['{"k":"new","title":"one","about":"x","why":"y"}'],
    )
    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(repo)])
    assert _rows(union, "SELECT repo_key, slug FROM repositories") == [
        {"repo_key": "local:dotfiles@main", "slug": "dotfiles@main"}
    ]
    # an explicit artifact file has no checkout context: stays shim-keyed too
    union2 = tmp_path / "union2.db"
    main(["gather", "--db", str(union2), str(repo / ".coding-stats" / "coding-stats.sql")])
    assert _rows(union2, "SELECT repo_key, slug FROM repositories") == [
        {"repo_key": "local:dotfiles@main", "slug": "dotfiles@main"}
    ]


def _populate_every_table(conn, repo_id: int, sha: str, feature_id: str) -> None:
    """One row in every Artifact table (settings is derived at gather, never
    copied): the full-shape fixture for the per-table round-trip test."""
    conn.execute(
        "INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at,"
        " author_name, title, message, subject_patch_id, walk_index, kind, diff,"
        " added_lines, deleted_lines, churn_lines)"
        " VALUES (1, ?, ?, NULL, 't', '2026-01-01T00:00:00Z', 'a', 'one', 'm', NULL, 1,"
        " 'normal', '', 1, 0, 1)",
        (repo_id, sha),
    )
    conn.execute(
        "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
        " model, classified_at) VALUES (1, 'feature', 'why-old', 'raw', 'm-old',"
        " '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ai_attribution (commit_id, verdict, harness, model)"
        " VALUES (1, 'possible', 'h-old', 'model-old')"
    )
    conn.execute(
        "INSERT INTO ai_evidence (commit_id, rule, raw, verdict) VALUES (1, 'r', 'x', 'yes')"
    )
    conn.execute(
        "INSERT INTO pr_evidence (repo_id, pr_number, commit_id, rule, raw, verdict,"
        " harness, model, channel, observed_at, suspicious)"
        " VALUES (?, 1, 1, 'r1', 'x', 'yes', NULL, NULL, 'pr', 't0', 0)",
        (repo_id,),
    )
    conn.execute(
        "INSERT INTO pr_watermark (repo_id, last_pr_number, last_merged_at)"
        " VALUES (?, 1, '2026-10-01T00:00:00Z')",
        (repo_id,),
    )
    conn.execute("INSERT INTO pr_disclosure (repo_id) VALUES (?)", (repo_id,))
    conn.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role) VALUES (1, ?, 'defines')",
        (feature_id,),
    )
    conn.execute(
        "INSERT INTO fix_touches (fix_commit_id, source_commit_id, hit_lines)"
        " VALUES (1, 1, 3)"
    )
    conn.execute("INSERT INTO fix_targets (fix_commit_id, target_commit_id) VALUES (1, 1)")
    conn.execute(
        "INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id)"
        " VALUES (1, ?, NULL)",
        (feature_id,),
    )
    conn.execute(
        "INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines)"
        " VALUES (?, ?, ?)",
        (feature_id, sha, 5),
    )


def test_every_table_round_trips_through_the_purge_and_reinsert(tmp_path, repo):
    """Per-table insert/purge round-trip on a two-key union (the catalogue
    regression test): re-gathering key A replaces its rows WHOLESALE in every
    Artifact table — purge deletes by the generated repo-scoped rules, the
    copy plans re-insert — never touching key B's rows, never leaving stale
    ones, and removing A's rows from tables its new artifact no longer
    carries (the disclosure marker)."""
    # key B: a second repo with the same full-shape rows
    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    run(repo_b, "init", "-q", "-b", "main")
    run(repo_b, "config", "user.name", "T")
    run(repo_b, "config", "user.email", "t@t.io")
    run(repo_b, "config", "commit.gpgsign", "false")
    commit_files(repo_b, "seed", {"b.txt": "b\n"})

    conn_a = make_db(tmp_path / "a.db", repo)
    conn_b = make_db(tmp_path / "b.db", repo_b)
    fa = add_feature(conn_a, "feature a")
    fb = add_feature(conn_b, "feature b")
    _populate_every_table(conn_a, 1, "x40", fa)
    _populate_every_table(conn_b, 1, "y40", fb)
    conn_a.commit()
    conn_b.commit()
    art_a = artifact.write(conn_a, repo)
    art_b = artifact.write(conn_b, repo_b)

    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(art_a), str(art_b)])
    populated = (
        "repositories",
        "commits",
        "commit_verdicts",
        "ai_attribution",
        "ai_evidence",
        "pr_evidence",
        "pr_watermark",
        "pr_disclosure",
        "features",
        "commits_features",
        "fix_touches",
        "fix_targets",
        "fixes_features",
        "feature_line_samples",
    )
    for table in populated:
        assert _rows(union, f"SELECT COUNT(*) c FROM {table}")[0]["c"] == 2, table

    # A's artifact changes in every mutable row; its disclosure marker is
    # GONE (the new artifact never ran the PR tier's bookkeeping)
    conn_a.execute("UPDATE commits SET added_lines = 9 WHERE id = 1")
    conn_a.execute("UPDATE commit_verdicts SET rationale = 'why-new', model = 'm-new'")
    conn_a.execute("UPDATE ai_attribution SET verdict = 'yes', model = 'model-new'")
    conn_a.execute("UPDATE ai_evidence SET verdict = 'possible'")
    conn_a.execute("UPDATE pr_evidence SET observed_at = 't1'")
    conn_a.execute(
        "UPDATE pr_watermark SET last_pr_number = 2, last_merged_at = '2026-10-02T00:00:00Z'"
    )
    conn_a.execute("DELETE FROM pr_disclosure")
    conn_a.execute("UPDATE features SET title = 'renamed' WHERE id = ?", (fa,))
    conn_a.execute("UPDATE commits_features SET role = 'touches'")
    conn_a.execute("UPDATE fix_touches SET hit_lines = 7")
    conn_a.execute("UPDATE fixes_features SET via_fix_commit_id = 1")
    conn_a.execute("UPDATE feature_line_samples SET live_lines = 11")
    conn_a.commit()
    artifact.write(conn_a, repo)

    main(["gather", "--db", str(union), str(art_a), str(art_b)])

    rid_a = _rows(union, "SELECT id FROM repositories WHERE repo_key = 'local:repo@main'")[0][
        "id"
    ]
    rid_b = _rows(union, "SELECT id FROM repositories WHERE repo_key = 'local:repo-b@main'")[0][
        "id"
    ]
    rows = _rows(union, "SELECT repo_id, added_lines FROM commits")
    assert {r["repo_id"]: r["added_lines"] for r in rows} == {rid_a: 9, rid_b: 1}
    verdicts = {
        r["model"]: r["rationale"]
        for r in _rows(union, "SELECT model, rationale FROM commit_verdicts")
    }
    assert verdicts == {"m-new": "why-new", "m-old": "why-old"}
    attr = {
        r["repo_id"]: (r["verdict"], r["model"])
        for r in _rows(
            union,
            "SELECT c.repo_id, a.verdict, a.model FROM ai_attribution a"
            " JOIN commits c ON c.id = a.commit_id",
        )
    }
    assert attr == {rid_a: ("yes", "model-new"), rid_b: ("possible", "model-old")}
    evidence = {
        r["repo_id"]: r["verdict"]
        for r in _rows(
            union,
            "SELECT c.repo_id, e.verdict FROM ai_evidence e JOIN commits c ON c.id = e.commit_id",
        )
    }
    assert evidence == {rid_a: "possible", rid_b: "yes"}
    pr = {
        r["repo_id"]: r["observed_at"]
        for r in _rows(union, "SELECT repo_id, observed_at FROM pr_evidence")
    }
    assert pr == {rid_a: "t1", rid_b: "t0"}
    marks = _rows(union, "SELECT repo_id, last_pr_number, last_merged_at FROM pr_watermark")
    assert {(m["repo_id"], m["last_pr_number"]) for m in marks} == {
        (rid_a, 2),
        (rid_b, 1),
    }
    # A's marker was purged with its rows: the new artifact lacks it
    disclosure = _rows(union, "SELECT repo_id FROM pr_disclosure")
    assert [d["repo_id"] for d in disclosure] == [rid_b]
    feats = {
        r["repo_id"]: r["title"] for r in _rows(union, "SELECT repo_id, title FROM features")
    }
    assert feats == {rid_a: "renamed", rid_b: "feature b"}
    roles = {r["role"] for r in _rows(union, "SELECT DISTINCT role FROM commits_features")}
    assert roles == {"touches", "defines"}
    hits = {
        r["repo_id"]: r["hit_lines"]
        for r in _rows(
            union,
            "SELECT c.repo_id, t.hit_lines FROM fix_touches t"
            " JOIN commits c ON c.id = t.fix_commit_id",
        )
    }
    assert hits == {rid_a: 7, rid_b: 3}
    targets = _rows(union, "SELECT COUNT(*) c FROM fix_targets")[0]["c"]
    assert targets == 2  # no mutable column: presence is the round-trip assert
    fixes = {
        r["repo_id"]: r["via_fix_commit_id"]
        for r in _rows(
            union,
            "SELECT c.repo_id, f.via_fix_commit_id FROM fixes_features f"
            " JOIN commits c ON c.id = f.fix_commit_id",
        )
    }
    # A's via was set (artifact-local commit id 1) and remapped onto A's
    # union-space commit with walk_index 1; B's stays NULL.
    a_via = _rows(
        union, f"SELECT id FROM commits WHERE repo_id = {rid_a} AND walk_index = 1"
    )[0]["id"]
    assert fixes[rid_a] == a_via and fixes[rid_b] is None
    samples = {
        r["live_lines"]
        for r in _rows(union, "SELECT DISTINCT live_lines FROM feature_line_samples")
    }
    assert samples == {11, 5}
    # no stale rows anywhere, no duplication (pr_disclosure's round trip is
    # its REMOVAL: only B's marker remains, asserted above)
    for table in populated:
        expected = 1 if table == "pr_disclosure" else 2
        assert _rows(union, f"SELECT COUNT(*) c FROM {table}")[0]["c"] == expected, table
    conn_a.close()
    conn_b.close()


def test_gather_aliases_shim_and_canonical_artifacts_of_one_repo(tmp_path):
    import unittest.mock as mock

    repo1 = _repo_with_artifact(
        tmp_path,
        "dotfiles",
        [("feature one", {"a.txt": "a1\n"}), ("feature two", {"b.txt": "b1\n"})],
        [
            '{"k":"new","title":"one","about":"x","why":"y"}',
            '{"k":"new","title":"two","about":"x","why":"y"}',
        ],
    )  # shim-keyed artifact, range = two commits
    repo2 = tmp_path / "dotfiles-clone"
    subprocess.run(["git", "clone", "-q", str(repo1), str(repo2)], check=True)
    run(repo2, "config", "user.name", "T")
    run(repo2, "config", "user.email", "t@t.io")
    run(repo2, "checkout", "-q", "-B", "main", "origin/main")  # clone-head hygiene
    commit_files(repo2, "feature three", {"c.txt": "c1\n"})
    for r in (repo1, repo2):
        if r == repo1:
            run(r, "remote", "add", "origin", "git@github.com:dahep/dotfiles.git")
        else:
            run(r, "remote", "set-url", "origin", "git@github.com:dahep/dotfiles.git")
        subprocess.run(["git", "-C", r, "config", "commit.gpgsign", "false"], check=True)
    db2 = str(tmp_path / "dotfiles-clone.db")
    main(["init", str(repo2), "--db", db2])  # canonical: origin resolves now
    with mock.patch(
        "coding_stats.cli.Session",
        lambda model, sid=None: Scripted(
            [
                '{"k":"new","title":"one","about":"x","why":"y"}',
                '{"k":"new","title":"two","about":"x","why":"y"}',
                '{"k":"new","title":"three","about":"x","why":"y"}',
            ]
        ),
    ):
        main(["update", "--db", db2, "--no-detect", "--no-lineage"])

    union = tmp_path / "union.db"
    main(["gather", "--db", str(union), str(repo1), str(repo2)])
    # one key: the shim artifact aliased into the canonical identity
    assert len(_rows(union, "SELECT * FROM repositories")) == 1
    assert _rows(union, "SELECT repo_key FROM repositories")[0]["repo_key"] == (
        "https://github.com/dahep/dotfiles@main"
    )
    # overlapping rows resolved by last-ingest-wins: three commits, three ids
    assert _rows(union, "SELECT COUNT(*) c FROM commits")[0]["c"] == 3
    assert [f["id"] for f in _rows(union, "SELECT id FROM features ORDER BY id")] == [
        "dahep-dotfiles@main-1",
        "dahep-dotfiles@main-2",
        "dahep-dotfiles@main-3",
    ]
    assert len(_rows(union, "SELECT * FROM union_artifacts")) == 1
