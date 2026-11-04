"""Table-catalogue tests: the ONE declaration and the SQL surfaces derived
from it. The catalogue is a leaf (zero package imports), self-validating at
import (plans match declarations), and these tests pin the generated SQL's
shape where consumers rely on it: DDL executes and yields the declared
columns, ownership predicates cover every reference, the purge is complete
over the catalogue, and the copy plans' conflict/sentinel knobs carry the
decided divergence values (decision Q9)."""

import re
import sqlite3

import pytest

from coding_stats import db
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
    assert imported <= {"collections", "dataclasses", "re", "sqlite3", "typing"}


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


def test_physical_fk_ddl_enforces_the_working_graph():
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
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
            " model, classified_at) VALUES (99, 'fix', 'r', 'raw', 'm', 'd')"
        )
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
    assert len(tables) == len(set(tables)) == len(expected)
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
    # per-direction sentinels (looked up by column NAME: order is the
    # declaration's, never the assertion's business)
    def planned(table: str, direction: str, column: str) -> tc.CopyColumn:
        return next(
            c for c in tc.copy_plan(table, direction).columns if c.name == column
        )

    assert planned("features", "gather", "created_run_id") == tc.CopyColumn(
        "created_run_id", "verbatim"
    )
    assert planned("features", "orgview", "created_run_id") == tc.CopyColumn(
        "created_run_id", "literal", None
    )
    assert planned("repositories", "gather", "added_at") == tc.CopyColumn(
        "added_at", "verbatim"
    )
    assert planned("repositories", "orgview", "added_at") == tc.CopyColumn(
        "added_at", "literal", ""
    )
    assert planned("commit_verdicts", "gather", "run_id") == tc.CopyColumn(
        "run_id", "literal", None
    )
    assert planned("commit_verdicts", "orgview", "run_id") == tc.CopyColumn(
        "run_id", "literal", None
    )


def test_plan_values_modes():
    """Every mode dispatches as declared: override, repo_id, verbatim, the
    commit remaps (NULL passes only through the nullable one), the literal
    sentinels — and every missing-input error is loud."""
    conn = _fresh_db(
        tc.artifact_ddl(tc.table("commits")),
        tc.artifact_ddl(tc.table("pr_evidence")),
        tc.artifact_ddl(tc.table("commit_verdicts")),
    )
    conn.execute(
        "INSERT INTO commits (id, repo_id, sha, tree_sha, committed_at, author_name,"
        " title, message, walk_index, kind, diff) VALUES (3, 1, 's', 't', 'd', 'a',"
        " 'title', 'msg', 1, 'normal', 'x')"
    )
    row = conn.execute("SELECT * FROM commits WHERE id = 3").fetchone()
    plan = tc.copy_plan("commits", "gather")
    values = tc.plan_values(plan, row, repo_id=9, overrides={"id": 30})
    assert values[0] == 30 and values[1] == 9 and values[2] == "s" and values[12] == "x"
    with pytest.raises(RuntimeError, match="override for id missing"):
        tc.plan_values(plan, row, repo_id=9)

    # repo_id mode needs its argument; commit_ids mode needs the map
    conn.execute(
        "INSERT INTO pr_evidence (repo_id, pr_number, commit_id, rule, raw, verdict,"
        " harness, model, channel, observed_at, suspicious)"
        " VALUES (5, 7, 3, 'r', 'x', 'yes', NULL, NULL, 'pr', 't0', 0)"
    )
    mapped = conn.execute("SELECT * FROM pr_evidence WHERE commit_id = 3").fetchone()
    pe = tc.copy_plan("pr_evidence", "gather")
    values = tc.plan_values(pe, mapped, repo_id=11, commit_ids={3: 30})
    assert values[0] == 11 and values[2] == 30 and values[4] == "x" and values[6] is None
    with pytest.raises(RuntimeError, match="repo_id mode needs repo_id"):
        tc.plan_values(pe, mapped, commit_ids={3: 30})
    with pytest.raises(RuntimeError, match="commit_ids mode needs the map"):
        tc.plan_values(pe, mapped, repo_id=11)

    # nullable remap: NULL passes through untouched, non-NULL remaps
    conn.execute(
        "INSERT INTO pr_evidence (repo_id, pr_number, commit_id, rule, raw, verdict,"
        " harness, model, channel, observed_at, suspicious)"
        " VALUES (5, 8, NULL, 'r2', 'y', 'possible', NULL, NULL, 'pr', 't1', 1)"
    )
    suspicious = conn.execute("SELECT * FROM pr_evidence WHERE commit_id IS NULL").fetchone()
    values = tc.plan_values(pe, suspicious, repo_id=11, commit_ids={})
    assert values[2] is None

    # literal mode: run provenance writes the NULL sentinel in both directions
    conn.execute(
        "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
        " model, run_id, classified_at) VALUES (3, 'fix', 'why', 'raw', 'm', 77, 'd')"
    )
    vrow = conn.execute("SELECT * FROM commit_verdicts").fetchone()
    cv = tc.copy_plan("commit_verdicts", "gather")
    values = tc.plan_values(cv, vrow, commit_ids={3: 30})
    assert values[0] == 30 and values[5] is None and values[6] == "d"
    conn.close()


def test_union_only_tables_never_enter_artifact_surfaces():
    names = {t.name for t in tc.artifact_tables()}
    assert "union_artifacts" not in names
    assert "runs" not in names  # machine-local: single handwritten DDL
    assert tc.table("union_artifacts").minted_at is None


def test_working_schema_matches_the_declaration(tmp_path):
    """The catalogue IS the migrated working schema: every declared table's
    columns (name/type/NOT NULL/DEFAULT/PK) and physical FK edge set equal
    the migrated DB's PRAGMAs — the physical flags are pinned, not assumed.
    union_artifacts is excluded (gather mints it; never in the ladder)."""
    conn = db.connect(tmp_path / "working.db")
    for t in tc.tables():
        if t.name == "union_artifacts":
            continue
        info = {
            r[1]: (r[2], r[3], r[4], r[5])
            for r in conn.execute(f"PRAGMA table_info({t.name})")
        }
        assert set(info) == set(t.names), t.name
        pk_cols: set[str] = set()
        for c in t.columns:
            if "PRIMARY KEY" in c.decl:
                pk_cols.add(c.name)
        for con in t.table_constraints:
            m = re.match(r"PRIMARY KEY \(([^)]*)\)$", con)
            if m:
                pk_cols.update(x.strip() for x in m.group(1).split(","))
        for c in t.columns:
            typ, notnull, dflt, pk = info[c.name]
            assert typ == c.type, (t.name, c.name, typ, c.type)
            assert bool(notnull) == ("NOT NULL" in c.decl), (t.name, c.name)
            m = re.search(r"DEFAULT (\S+|'[^']*')", c.decl)
            assert (m.group(1) if m else None) == dflt, (t.name, c.name, dflt)
            assert bool(pk) == (c.name in pk_cols), (t.name, c.name)
        edges = {
            (r[2], r[3], r[4], r[5], r[6])
            for r in conn.execute(f"PRAGMA foreign_key_list({t.name})")
        }
        physical = {
            (c.ref.table, c.name, c.ref.column, "NO ACTION", "NO ACTION")
            for c in t.columns
            if c.ref is not None and c.ref.physical
        }
        assert edges == physical, (t.name, edges, physical)
    conn.close()


def test_step0_generated_tables_are_frozen_history(tmp_path):
    """schema step 0 derives settings, commit_verdicts, fix_touches and
    fix_targets from the declaration — these literals pin that derivation to
    the historical shape. A catalogue edit to any of the four lands HERE
    first (a deliberate, reviewed change), never silently in fresh DBs."""
    expected = {
        "settings": (
            ("key", "value"),
            set(),
        ),
        "commit_verdicts": (
            (
                "commit_id", "verdict", "rationale", "raw_llm_output", "model",
                "run_id", "classified_at",
            ),
            {("commits", "commit_id", "id"), ("runs", "run_id", "id")},
        ),
        "fix_touches": (
            ("fix_commit_id", "source_commit_id", "hit_lines"),
            {("commits", "fix_commit_id", "id"), ("commits", "source_commit_id", "id")},
        ),
        # TWO edges: the duplicate table-level FOREIGN KEY on target_commit_id
        # (a byte-for-byte copy of the inline REFERENCES) is gone — the
        # enforced constraint set is unchanged.
        "fix_targets": (
            ("fix_commit_id", "target_commit_id"),
            {("commits", "fix_commit_id", "id"), ("commits", "target_commit_id", "id")},
        ),
    }
    conn = db.connect(tmp_path / "working.db")
    for name, (columns, edges) in expected.items():
        cols = tuple(r[1] for r in conn.execute(f"PRAGMA table_info({name})"))
        assert cols == columns, name
        got = {(r[2], r[3], r[4]) for r in conn.execute(f"PRAGMA foreign_key_list({name})")}
        assert got == edges, (name, got)
    conn.close()
