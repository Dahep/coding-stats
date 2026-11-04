"""Table-catalogue tests: the ONE declaration and the SQL surfaces derived
from it. The catalogue is a leaf (zero package imports), self-validating at
import (plans match declarations), and these tests pin the generated SQL's
shape where consumers rely on it: DDL executes and yields the declared
columns, ownership predicates cover every reference, the purge is complete
over the catalogue, and the copy plans' conflict/sentinel knobs carry the
decided divergence values (decision Q9)."""

import sqlite3

from coding_stats import tablecatalog as tc


def _fresh_db(*ddls: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for ddl in ddls:
        conn.executescript(ddl)
    return conn


def test_catalogue_is_a_leaf():
    """Zero package imports: the declaration can never depend on a consumer."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(tc.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"collections", "dataclasses", "sqlite3", "typing"}


def test_every_generated_ddl_executes_and_yields_the_declared_columns():
    for t in tc.tables():
        for ddl in (tc.artifact_ddl(t), tc.fk_ddl(t), tc.fk_ddl(t, if_not_exists=True)):
            conn = _fresh_db(ddl)
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t.name})")]
            assert cols == list(t.names), t.name
            conn.close()


def test_artifact_ddl_strips_references_and_fits_the_strip_tables_convention():
    """The Artifact DDL is FK-stripped (ADR-0003 clause 2) and structurally
    compatible with conftest.strip_tables: 'CREATE TABLE <name> (' first line,
    ');' terminator last, no interior line stripping to the terminator."""
    for t in tc.artifact_tables():
        ddl = tc.artifact_ddl(t)
        assert "REFERENCES" not in ddl, t.name
        lines = ddl.splitlines()
        assert lines[0] == f"CREATE TABLE {t.name} ("
        assert lines[-1] == ");"
        assert not any(line.strip() == ");" for line in lines[1:-1]), t.name
        # and the FK-ful form carries exactly the physical references
        fk = tc.fk_ddl(t)
        physical = [f"REFERENCES {c.ref.table}({c.ref.column})"
                    for c in t.columns if c.ref is not None and c.ref.physical]
        for ref in physical:
            assert ref in fk, (t.name, ref)
        logical = [c.name for c in t.columns
                   if c.ref is not None and not c.ref.physical]
        if logical:
            assert fk.count("REFERENCES features(id)") == 0, t.name


def test_physical_fk_ddl_enforces_the_working_graph(tmp_path):
    """The FK-ful DDL builds a working graph: parent rows must exist (insert
    into commit_verdicts without a parent commit refuses under FK)."""
    conn = _fresh_db(
        tc.fk_ddl(tc.table("repositories")),
        tc.fk_ddl(tc.table("commits")),
        tc.fk_ddl(tc.table("commit_verdicts")),
        # runs is machine-local and NOT catalogued (single DDL in schema.py);
        # the physical graph needs it to exist
        "CREATE TABLE runs (id INTEGER PRIMARY KEY)",
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO repositories (id, repo_key, slug, branch, clone_url, added_at)"
        " VALUES (1, 'k', 's', 'main', NULL, '')"
    )
    conn.execute(
        "INSERT INTO commits (id, repo_id, sha, tree_sha, committed_at, author_name,"
        " title, message, walk_index, kind, diff) VALUES (1, 1, 's', 't', 'd', 'a', 't',"
        " 'm', 1, 'normal', '')"
    )
    try:
        conn.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
            " model, classified_at) VALUES (99, 'fix', 'r', 'raw', 'm', 'd')"
        )
        raise AssertionError("dangling commit_id accepted")
    except sqlite3.IntegrityError:
        pass
    conn.close()


def test_canonical_selects_are_deterministic_and_run():
    for t in tc.tables():
        sql = tc.canonical_select(t)
        assert sql.endswith(f"ORDER BY {', '.join(t.order_by)}"), t.name
    # the settings whitelist WHERE is the curation knob, byte-stable
    assert tc.canonical_select(tc.table("settings")) == (
        "SELECT key, value FROM settings WHERE key IN"
        " ('backfill_state', 'branch', 'min_target_lines', 'model',"
        " 'unattributed_lines') ORDER BY key"
    )


def test_shape_completion_maps():
    """Post-core tables and late columns only: core tables are never
    resurrected by shape completion."""
    assert tc.complete_steps() == {
        "ai_attribution": 4,
        "ai_evidence": 4,
        "pr_evidence": 5,
        "pr_watermark": 6,
        "pr_disclosure": 8,
    }
    assert tc.complete_columns() == {("pr_watermark", "last_merged_at"): 7}
    # the completing ALTER carries the column's declared type
    col = tc.table("pr_watermark").column("last_merged_at")
    assert col.type == "TEXT"


def test_ownership_checks_cover_every_in_artifact_reference():
    """One predicate per reference to an in-artifact table: repositories by
    value, commits/features by membership, nullable columns guarded."""
    checks = dict(tc.ownership_checks(7))
    expected_tables = {
        t.name
        for t in tc.artifact_tables()
        if any(
            c.ref is not None and c.ref.table in ("repositories", "commits", "features")
            for c in t.columns
        )
    }
    assert set(checks) == expected_tables
    assert "settings" not in checks and "repositories" not in checks
    assert checks["pr_evidence"] == (
        "SELECT COUNT(*) c FROM pr_evidence WHERE repo_id != 7"
        " OR (commit_id IS NOT NULL AND commit_id NOT IN (SELECT id FROM commits))"
    )
    assert checks["fixes_features"] == (
        "SELECT COUNT(*) c FROM fixes_features WHERE"
        " fix_commit_id NOT IN (SELECT id FROM commits)"
        " OR feature_id NOT IN (SELECT id FROM features)"
        " OR (via_fix_commit_id IS NOT NULL AND via_fix_commit_id NOT IN"
        " (SELECT id FROM commits))"
    )
    # logical-only refs (feature_id after schema step 3) are still checked
    assert "feature_id NOT IN (SELECT id FROM features)" in checks["commits_features"]
    assert "feature_id NOT IN (SELECT id FROM features)" in checks["feature_line_samples"]


def test_purge_statements_are_children_first_and_complete():
    stmts = tc.purge_statements()
    tables = [sql.split()[2] for sql, _n in stmts]
    # parents last, features before commits (the working purge's order)
    assert tables[-2:] == ["features", "commits"]
    # every table that holds a repo's rows is purged exactly once
    expected = {
        t.name
        for t in tc.artifact_tables()
        if t.name not in ("repositories", "settings", "feature_line_samples")
    }
    assert set(tables) == expected
    assert len(tables) == len(set(tables)) == 12
    # placeholder count matches each statement
    for sql, n in stmts:
        assert sql.count("?") == n
        assert "repo_id = ?" in sql or "IN (SELECT id FROM commits WHERE repo_id = ?)" in sql


def test_feature_sweep_tables():
    """Feature-scoped rows with no repo dimension: the orphan sweep's set."""
    assert tc.feature_sweep_tables() == (
        "commits_features",
        "fixes_features",
        "feature_line_samples",
    )


def test_copy_plans_match_their_declarations_and_knobs():
    # every artifact table but settings carries a gather plan; the org
    # projection plans cover every table its copies write
    gather = tc.copy_tables("gather")
    assert "settings" not in gather and "union_artifacts" not in gather
    assert set(gather) == {t.name for t in tc.artifact_tables()} - {"settings"}
    for direction in ("gather", "orgview"):
        for name in tc.copy_tables(direction):
            plan = tc.copy_plan(name, direction)
            assert tuple(c.name for c in plan.columns) == tc.table(name).names, name
    # the decided divergence knobs (decision Q9)
    assert tc.copy_plan("feature_line_samples", "gather").conflict == "OR REPLACE"
    assert tc.copy_plan("feature_line_samples", "orgview").conflict == "OR IGNORE"
    assert tc.copy_plan("feature_line_samples", "orgview").order_by == (
        "feature_id",
        "at_commit_sha",
        "live_lines",
    )
    for name in ("commits_features", "fix_touches", "fix_targets", "fixes_features", "ai_evidence"):
        assert tc.copy_plan(name, "gather").conflict == ""  # drift guard: plain INSERT
        assert tc.copy_plan(name, "orgview").conflict == "OR IGNORE"
    assert tc.copy_plan("ai_attribution", "orgview").conflict == "OR REPLACE"
    assert tc.copy_plan("repositories", "gather").conflict == "OR REPLACE"
    # per-direction sentinels
    assert tc.copy_plan("features", "gather").columns[5] == tc.CopyColumn(
        "created_run_id", "verbatim"
    )
    assert tc.copy_plan("features", "orgview").columns[5] == tc.CopyColumn(
        "created_run_id", "literal", None
    )
    assert tc.copy_plan("repositories", "gather").columns[5] == tc.CopyColumn(
        "added_at", "verbatim"
    )
    assert tc.copy_plan("repositories", "orgview").columns[5] == tc.CopyColumn(
        "added_at", "literal", ""
    )
    assert tc.copy_plan("commit_verdicts", "gather").columns[5] == tc.CopyColumn(
        "run_id", "literal", None
    )


def test_plan_values_modes(tmp_path):
    conn = _fresh_db(
        tc.artifact_ddl(tc.table("commits")),
        tc.artifact_ddl(tc.table("features")),
    )
    conn.execute(
        "INSERT INTO commits (id, repo_id, sha, tree_sha, committed_at, author_name,"
        " title, message, walk_index, kind, diff) VALUES (3, 1, 's', 't', 'd', 'a',"
        " 'title', 'msg', 1, 'normal', 'x')"
    )
    row = conn.execute("SELECT * FROM commits WHERE id = 3").fetchone()
    plan = tc.copy_plan("commits", "gather")
    values = tc.plan_values(
        plan, row, repo_id=9, commit_ids={3: 30}, overrides={"id": 30}
    )
    assert values[0] == 30 and values[1] == 9 and values[2] == "s" and values[12] == "x"

    # nullable remap: NULL passes through, non-NULL remaps — created_run_id
    # carries a runs reference (physical) but rides verbatim in gather
    conn.execute(
        "INSERT INTO features (id, title, about, title_history_json, created_at,"
        " created_run_id, repo_id, live_lines) VALUES ('f', 't', '', '[]', 'd', 7, 1, 4)"
    )
    assert tc.table("features").column("created_run_id").ref is not None
    conn.close()

    # missing override is a loud error, not a silent NULL
    try:
        tc.plan_values(plan, row)
        raise AssertionError("missing override accepted")
    except RuntimeError:
        pass


def test_union_only_tables_never_enter_artifact_surfaces():
    names = {t.name for t in tc.artifact_tables()}
    assert "union_artifacts" not in names
    assert "runs" not in names  # machine-local: single handwritten DDL
    assert tc.table("union_artifacts").minted_at is None
