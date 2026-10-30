"""SQLite schema for coding-stats.

Migrations are ordered steps applied to a fresh DB or an existing file; a step
is either SQL text or a function applied to the connection. db.connect applies
them in order and bookkeeps each in a ``_schema_step_NNNN`` table. Step 1 is a
rebuild (SQLite cannot alter PK/unique constraints) with runtime data mapping.
"""

import json
import sqlite3
from collections.abc import Callable

from . import gitwalk, identity

Step = str | Callable[[sqlite3.Connection], None]


STEP_0_SQL = """
    PRAGMA journal_mode = WAL;
    PRAGMA foreign_keys = ON;

    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        config_json TEXT NOT NULL,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        notes TEXT
    );

    -- settings as JSON key/value
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    -- Target-branch commits only, in chronological walk order.
    CREATE TABLE commits (
        id INTEGER PRIMARY KEY,
        sha TEXT NOT NULL UNIQUE,
        parent_sha TEXT,              -- first parent on Target branch; NULL for root
        tree_sha TEXT NOT NULL,
        committed_at TEXT NOT NULL,   -- ISO-8601
        author_name TEXT NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        subject_patch_id TEXT,        -- git patch-id of the commit subject-quoted diff
        walk_index INTEGER NOT NULL UNIQUE,  -- 1-based chronological order
        kind TEXT NOT NULL CHECK (kind IN ('normal', 'merge')),
        diff TEXT NOT NULL
    );

    -- LLM-assigned Commit verdict; blame evidence is stored as evidence, not verdict.
    CREATE TABLE commit_verdicts (
        commit_id INTEGER PRIMARY KEY REFERENCES commits(id),
        verdict TEXT NOT NULL CHECK (verdict IN ('feature', 'fix', 'refactor', 'revert',
                                                 'cleanup', 'merge', 'unknown')),
        rationale TEXT NOT NULL,
        raw_llm_output TEXT NOT NULL,
        model TEXT NOT NULL,
        run_id INTEGER REFERENCES runs(id),
        classified_at TEXT NOT NULL
    );

    CREATE TABLE features (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        about TEXT NOT NULL DEFAULT '',
        title_history_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        created_run_id INTEGER REFERENCES runs(id)
    );

    -- Many-to-many: a commit may define several features; feature set may span commits.
    CREATE TABLE commits_features (
        commit_id INTEGER NOT NULL REFERENCES commits(id),
        feature_id INTEGER NOT NULL REFERENCES features(id),
        role TEXT NOT NULL CHECK (role IN ('defines', 'touches')),
        PRIMARY KEY (commit_id, feature_id),
        UNIQUE (feature_id, commit_id)
    );

    -- Raw blame hits at commit granularity.
    CREATE TABLE fix_touches (
        fix_commit_id INTEGER NOT NULL REFERENCES commits(id),
        source_commit_id INTEGER NOT NULL REFERENCES commits(id),
        hit_lines INTEGER NOT NULL,
        PRIMARY KEY (fix_commit_id, source_commit_id)
    );

    -- Curated targets: sources with nontrivial overlap; may include other fixes.
    CREATE TABLE fix_targets (
        fix_commit_id INTEGER NOT NULL REFERENCES commits(id),
        target_commit_id INTEGER NOT NULL REFERENCES commits(id),
        PRIMARY KEY (fix_commit_id, target_commit_id),
        FOREIGN KEY (target_commit_id) REFERENCES commits(id)
    );

    -- Transitive closure fix -> original feature(s); one row per (fix, feature) reach.
    CREATE TABLE fixes_features (
        fix_commit_id INTEGER NOT NULL REFERENCES commits(id),
        feature_id INTEGER NOT NULL REFERENCES features(id),
        -- NULL when the first-hop target defines the Feature directly
        via_fix_commit_id INTEGER REFERENCES commits(id),
        PRIMARY KEY (fix_commit_id, feature_id),
        UNIQUE (fix_commit_id, feature_id, via_fix_commit_id)
    );

    CREATE INDEX idx_commits_patch_id ON commits(subject_patch_id)
        WHERE subject_patch_id IS NOT NULL;
    CREATE INDEX idx_commits_features_feature ON commits_features(feature_id);
    CREATE INDEX idx_fix_targets_target ON fix_targets(target_commit_id);
    CREATE INDEX idx_fixes_features_feature ON fixes_features(feature_id);
    """


def _setting(conn: sqlite3.Connection, key: str) -> object | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def _step_1(conn: sqlite3.Connection) -> None:
    """Migration step 1: repo-scoped commit ids and prefixed feature ids
    (ADR-0003 clause 5), plus the per-commit line columns of ADR-0003 clause 2.

    Repository identity shim: an old DB has only settings.repo, a machine-local
    path, and machine-local paths are NEVER identity (CONTEXT.md "Repository
    key"). So the repositories row gets the documented placeholder
    repo_key = "local:<basename>@<branch>" and slug = <basename>, kept honestly
    as a shim: canonical URL resolution happens at init/walk when the git repo
    is available (register_repository), and wave 2 can backfill the key. Feature
    ids minted under the shim keep their prefix after any later key upgrade.

    Rebuilt tables: commits (drop global sha/walk_index uniques for per-repo
    ones, add repo_id + added/deleted/churn lines), features (INTEGER ->
    repo-prefixed TEXT id, add repo_id), commits_features/fixes_features (their
    feature_id columns follow the new TEXT feature ids). Attribution tables
    keep referencing commits.id INTEGER; composite (repo, sha) references are
    the wave-2 union/serializability path.

    Store-only commits (stripped diff empty) are PURGED with every dependent
    row and their orphaned features — the spec says they never enter stats, so
    migrated DBs must agree with freshly walked ones. Old stored diffs are run
    through the same store-path exclusion and churn derivation the walk uses.
    Atomicity is owned by db._apply_migration: this function is pure DDL/DML
    inside its transaction (no pragmas, no commits of its own).
    """
    repo_value = _setting(conn, "repo")
    branch_value = _setting(conn, "branch")
    has_data = bool(
        conn.execute("SELECT 1 FROM commits LIMIT 1").fetchone()
        or conn.execute("SELECT 1 FROM features LIMIT 1").fetchone()
    )
    if has_data and not repo_value:
        raise RuntimeError(
            "migration step 1: commits/features exist but settings.repo is absent;"
            " cannot derive repository identity"
        )
    key = slug = None
    if isinstance(repo_value, str):
        base, plain_slug = identity.local_identity(repo_value)
        branch_v = branch_value if isinstance(branch_value, str) else None
        slug = identity.repo_slug(plain_slug, branch_v) if branch_v else plain_slug
        key = identity.repo_key(base, branch_v)

    stats: dict[int, gitwalk.DiffStat] = {}
    stale: set[int] = set()
    for r in conn.execute("SELECT id, diff FROM commits ORDER BY id"):
        stat = gitwalk.prepare_diff(r["diff"])
        stats[int(r["id"])] = stat
        if not stat.diff.strip():
            stale.add(int(r["id"]))
    _purge_stale(conn, stale)

    conn.execute(
        """CREATE TABLE repositories (
               id INTEGER PRIMARY KEY,
               repo_key TEXT NOT NULL UNIQUE,
               slug TEXT NOT NULL,
               branch TEXT,
               clone_url TEXT,
               added_at TEXT
           )"""
    )
    repo_id: int | None = None
    if key is not None:
        cur = conn.execute(
            "INSERT INTO repositories (repo_key, slug, branch, clone_url, added_at)"
            " VALUES (?, ?, ?, NULL, datetime('now'))",
            (key, slug, branch_value),
        )
        assert cur.lastrowid is not None
        repo_id = int(cur.lastrowid)
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('repo_id', ?)",
            (json.dumps(repo_id),),
        )
    _rebuild_commits(conn, repo_id, stats)
    _rebuild_features(conn, repo_id, slug or "")
    _rebuild_feature_links(conn, slug or "")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"migration step 1 left FK violations: {violations[:5]}")


def _purge_stale(conn: sqlite3.Connection, stale: set[int]) -> None:
    """Drop store-only commits' dependent rows and features left without any
    defining/touching commit (they would only pollute stats). The commit rows
    themselves are skipped by _rebuild_commits."""
    if stale:
        marks = ",".join("?" * len(stale))
        ids: tuple[int, ...] = tuple(sorted(stale))
        conn.execute(f"DELETE FROM commit_verdicts WHERE commit_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM commits_features WHERE commit_id IN ({marks})", ids)
        conn.execute(
            f"DELETE FROM fix_touches WHERE fix_commit_id IN ({marks})"
            f" OR source_commit_id IN ({marks})",
            ids + ids,
        )
        conn.execute(
            f"DELETE FROM fix_targets WHERE fix_commit_id IN ({marks})"
            f" OR target_commit_id IN ({marks})",
            ids + ids,
        )
        conn.execute(
            f"DELETE FROM fixes_features WHERE fix_commit_id IN ({marks})"
            f" OR via_fix_commit_id IN ({marks})",
            ids + ids,
        )
    conn.execute(
        "DELETE FROM fixes_features WHERE feature_id NOT IN"
        " (SELECT feature_id FROM commits_features)"
    )
    conn.execute("DELETE FROM features WHERE id NOT IN (SELECT feature_id FROM commits_features)")


def _rebuild_commits(
    conn: sqlite3.Connection, repo_id: int | None, stats: dict[int, gitwalk.DiffStat]
) -> None:
    conn.execute(
        """CREATE TABLE commits_migration (
               id INTEGER PRIMARY KEY,
               repo_id INTEGER NOT NULL REFERENCES repositories(id),
               sha TEXT NOT NULL,
               parent_sha TEXT,
               tree_sha TEXT NOT NULL,
               committed_at TEXT NOT NULL,
               author_name TEXT NOT NULL,
               title TEXT NOT NULL,
               message TEXT NOT NULL,
               subject_patch_id TEXT,
               walk_index INTEGER NOT NULL,
               kind TEXT NOT NULL CHECK (kind IN ('normal', 'merge')),
               diff TEXT NOT NULL,
               added_lines INTEGER NOT NULL DEFAULT 0,
               deleted_lines INTEGER NOT NULL DEFAULT 0,
               churn_lines INTEGER NOT NULL DEFAULT 0,
               UNIQUE (repo_id, sha),
               UNIQUE (repo_id, walk_index)
           )"""
    )
    for r in conn.execute("SELECT * FROM commits ORDER BY id"):
        stat = stats[int(r["id"])]  # same exclusion + churn rules as walk
        if not stat.diff.strip():
            continue  # store-only commit: purged from stats entirely
        conn.execute(
            """INSERT INTO commits_migration
               (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title,
                message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines,
                churn_lines)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r["id"],
                repo_id,
                r["sha"],
                r["parent_sha"],
                r["tree_sha"],
                r["committed_at"],
                r["author_name"],
                r["title"],
                r["message"],
                r["subject_patch_id"],
                r["walk_index"],
                r["kind"],
                stat.diff,
                stat.added,
                stat.deleted,
                stat.churn,
            ),
        )
    conn.execute("DROP TABLE commits")
    conn.execute("ALTER TABLE commits_migration RENAME TO commits")
    conn.execute(
        "CREATE INDEX idx_commits_patch_id ON commits(subject_patch_id)"
        " WHERE subject_patch_id IS NOT NULL"
    )


def _rebuild_features(conn: sqlite3.Connection, repo_id: int | None, slug: str) -> None:
    conn.execute(
        """CREATE TABLE features_migration (
               id TEXT PRIMARY KEY,
               title TEXT NOT NULL,
               about TEXT NOT NULL DEFAULT '',
               title_history_json TEXT NOT NULL DEFAULT '[]',
               created_at TEXT NOT NULL,
               created_run_id INTEGER REFERENCES runs(id),
               repo_id INTEGER NOT NULL REFERENCES repositories(id)
           )"""
    )
    # old integer ids become '{slug}-{local number}' (prefix, not remap)
    conn.execute(
        """INSERT INTO features_migration
               (id, title, about, title_history_json, created_at, created_run_id, repo_id)
           SELECT ? || '-' || id, title, about, title_history_json, created_at,
                  created_run_id, ?
           FROM features ORDER BY id""",
        (slug, repo_id),
    )
    conn.execute("DROP TABLE features")
    conn.execute("ALTER TABLE features_migration RENAME TO features")


def _rebuild_feature_links(conn: sqlite3.Connection, slug: str) -> None:
    # commits_features: commit_id, feature_id, role
    conn.execute(
        """CREATE TABLE commits_features_migration (
               commit_id INTEGER NOT NULL REFERENCES commits(id),
               feature_id TEXT NOT NULL REFERENCES features(id),
               role TEXT NOT NULL CHECK (role IN ('defines', 'touches')),
               PRIMARY KEY (commit_id, feature_id),
               UNIQUE (feature_id, commit_id)
           )"""
    )
    conn.execute(
        """INSERT INTO commits_features_migration (commit_id, feature_id, role)
           SELECT commit_id, ? || '-' || feature_id, role FROM commits_features
           ORDER BY commit_id, feature_id""",
        (slug,),
    )
    conn.execute("DROP TABLE commits_features")
    conn.execute("ALTER TABLE commits_features_migration RENAME TO commits_features")
    conn.execute("CREATE INDEX idx_commits_features_feature ON commits_features(feature_id)")
    # fixes_features: fix_commit_id, feature_id, via_fix_commit_id
    conn.execute(
        """CREATE TABLE fixes_features_migration (
               fix_commit_id INTEGER NOT NULL REFERENCES commits(id),
               feature_id TEXT NOT NULL REFERENCES features(id),
               via_fix_commit_id INTEGER REFERENCES commits(id),
               PRIMARY KEY (fix_commit_id, feature_id),
               UNIQUE (fix_commit_id, feature_id, via_fix_commit_id)
           )"""
    )
    conn.execute(
        """INSERT INTO fixes_features_migration (fix_commit_id, feature_id, via_fix_commit_id)
           SELECT fix_commit_id, ? || '-' || feature_id, via_fix_commit_id FROM fixes_features
           ORDER BY fix_commit_id, feature_id""",
        (slug,),
    )
    conn.execute("DROP TABLE fixes_features")
    conn.execute("ALTER TABLE fixes_features_migration RENAME TO fixes_features")
    conn.execute("CREATE INDEX idx_fixes_features_feature ON fixes_features(feature_id)")


def _step_2(conn: sqlite3.Connection) -> None:
    """Feature size (live lines) + Sample rows (grill-r3-Q7/Q9).

    features.live_lines: the feature's line count at the Target-branch tip as
    of the last live-lines run (NOT NULL DEFAULT 0 — the value is only ever a
    measurement, never unknown); feature_line_samples carries the history:
    one row per (feature, sample commit) for every feature alive at that
    commit — the same row family for forward samples (each update, at the
    tip) and for backfill sweeps (grill-r3-Q9). No timestamps anywhere: the
    at_commit_sha key is the Sample identity (CONTEXT.md "Sample")."""
    conn.execute("ALTER TABLE features ADD COLUMN live_lines INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """CREATE TABLE feature_line_samples (
               feature_id TEXT NOT NULL REFERENCES features(id),
               at_commit_sha TEXT NOT NULL,
               live_lines INTEGER NOT NULL,
               PRIMARY KEY (feature_id, at_commit_sha)
           )"""
    )


def _step_3(conn: sqlite3.Connection) -> None:
    """The union's feature key becomes (repository, id) (ADR-0006 clause 4).

    A seeded feature id (``{repo}@{branch}``-prefixed, minted by the branch
    that first classified it) legitimately appears in SEVERAL branch
    artifacts of one repo, each keeping its own live lines. Under a
    global-TEXT-id PK that second insert dies; the id alone is not a
    sufficient key. Dependent tables keep the feature_id column but lose the
    REFERENCES to features(id): SQLite cannot FK a composite target from a
    single column, while the commit_id-side references stay exact.
    For a single-repo DB the change is invisible (one repositories row; the
    id was already unique within it)."""
    # migration carriers first: copy functions cannot alter PKs, only rebuild
    conn.execute(
        """CREATE TABLE features_migration (
               id TEXT NOT NULL,
               title TEXT NOT NULL,
               about TEXT NOT NULL DEFAULT '',
               title_history_json TEXT NOT NULL DEFAULT '[]',
               created_at TEXT NOT NULL,
               created_run_id INTEGER REFERENCES runs(id),
               repo_id INTEGER NOT NULL REFERENCES repositories(id),
               live_lines INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (repo_id, id)
           )"""
    )
    conn.execute(
        """INSERT INTO features_migration (id, title, about, title_history_json,
               created_at, created_run_id, repo_id, live_lines)
           SELECT id, title, about, title_history_json, created_at, created_run_id,
                  repo_id, live_lines
           FROM features ORDER BY id, repo_id"""
    )
    conn.execute(
        """CREATE TABLE commits_features_migration (
               commit_id INTEGER NOT NULL,
               feature_id TEXT NOT NULL,
               role TEXT NOT NULL
           )"""
    )
    conn.execute(
        "INSERT INTO commits_features_migration SELECT commit_id, feature_id, role"
        " FROM commits_features ORDER BY commit_id, feature_id"
    )
    conn.execute(
        """CREATE TABLE fixes_features_migration (
               fix_commit_id INTEGER NOT NULL,
               feature_id TEXT NOT NULL,
               via_fix_commit_id INTEGER
           )"""
    )
    conn.execute(
        "INSERT INTO fixes_features_migration SELECT fix_commit_id, feature_id,"
        " via_fix_commit_id FROM fixes_features"
        " ORDER BY fix_commit_id, feature_id, via_fix_commit_id"
    )
    conn.execute(
        """CREATE TABLE feature_line_samples_migration (
               feature_id TEXT NOT NULL,
               at_commit_sha TEXT NOT NULL,
               live_lines INTEGER NOT NULL
           )"""
    )
    conn.execute(
        "INSERT INTO feature_line_samples_migration SELECT feature_id, at_commit_sha,"
        " live_lines FROM feature_line_samples ORDER BY feature_id, at_commit_sha"
    )
    # swap: carriers out, curated shape in (children of features gone too)
    conn.execute("DROP TABLE feature_line_samples")
    conn.execute("DROP TABLE fixes_features")
    conn.execute("DROP TABLE commits_features")
    conn.execute("DROP TABLE features")
    conn.execute("ALTER TABLE features_migration RENAME TO features")
    conn.execute(
        """CREATE TABLE commits_features (
               commit_id INTEGER NOT NULL REFERENCES commits(id),
               feature_id TEXT NOT NULL,
               role TEXT NOT NULL CHECK (role IN ('defines', 'touches')),
               PRIMARY KEY (commit_id, feature_id),
               UNIQUE (feature_id, commit_id)
           )"""
    )
    conn.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role)"
        " SELECT commit_id, feature_id, role FROM commits_features_migration"
        " ORDER BY commit_id, feature_id"
    )
    conn.execute(
        """CREATE TABLE fixes_features (
               fix_commit_id INTEGER NOT NULL REFERENCES commits(id),
               feature_id TEXT NOT NULL,
               via_fix_commit_id INTEGER REFERENCES commits(id),
               PRIMARY KEY (fix_commit_id, feature_id),
               UNIQUE (fix_commit_id, feature_id, via_fix_commit_id)
           )"""
    )
    conn.execute(
        "INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id)"
        " SELECT fix_commit_id, feature_id, via_fix_commit_id FROM fixes_features_migration"
        " ORDER BY fix_commit_id, feature_id, via_fix_commit_id"
    )
    conn.execute(
        """CREATE TABLE feature_line_samples (
               feature_id TEXT NOT NULL,
               at_commit_sha TEXT NOT NULL,
               live_lines INTEGER NOT NULL,
               PRIMARY KEY (feature_id, at_commit_sha)
           )"""
    )
    conn.execute(
        "INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines)"
        " SELECT feature_id, at_commit_sha, live_lines"
        " FROM feature_line_samples_migration ORDER BY feature_id, at_commit_sha"
    )
    for name in (
        "feature_line_samples_migration",
        "fixes_features_migration",
        "commits_features_migration",
    ):
        conn.execute(f"DROP TABLE {name}")
    conn.execute("CREATE INDEX idx_commits_features_feature ON commits_features(feature_id)")
    conn.execute("CREATE INDEX idx_fixes_features_feature ON fixes_features(feature_id)")


def _step_4(conn: sqlite3.Connection) -> None:
    """AI attribution state (ADR-0007 baseline tier, ticket #34).

    Per-commit git-only attribution verdict (yes / possible / no-evidence —
    the states of CONTEXT.md "AI attribution") plus its auditable evidence
    rows. Both are pure functions of the Target branch's commit objects:
    the walk records them per commit and re-derives any missing rows on the
    next walk (a restored pre-feature artifact carries none), so they ride
    the Artifact like every other recorded state. ``IF NOT EXISTS`` because
    artifact.materialize completes a pre-feature artifact's missing known
    tables before this step runs on the replayed DB."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ai_attribution (
               commit_id INTEGER PRIMARY KEY REFERENCES commits(id),
               verdict TEXT NOT NULL CHECK (verdict IN ('yes', 'possible', 'no-evidence')),
               harness TEXT,
               model TEXT
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ai_evidence (
               commit_id INTEGER NOT NULL REFERENCES commits(id),
               rule TEXT NOT NULL,
               raw TEXT NOT NULL,
               verdict TEXT NOT NULL CHECK (verdict IN ('yes', 'possible')),
               PRIMARY KEY (commit_id, rule, raw)
           )"""
    )


def _step_5(conn: sqlite3.Connection) -> None:
    """PR-channel evidence rows (ADR-0007 clauses 3-5, ticket #35).

    The channel-pr tier's RECORDED observations — PR number, rule id, raw
    matched string, the tier, the identity the raw names, ``observed_at`` and
    channel — attached at the mapped landed sha. Unlike ``ai_evidence`` these
    are never re-derived by recompute (they read GitHub data and PR head
    refs, not just the Target branch); a re-run replaces one PR's rows
    wholesale via (repo_id, pr_number). ``commit_id`` is NULL exactly for
    suspicious rows — the head-parentage check failed — which are audit
    records and never attribution. ``repo_id`` scopes rows whose commit is
    NULL (suspicious) in multi-repo unions. ``IF NOT EXISTS`` for the same
    replay reason as step 4."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pr_evidence (
               repo_id INTEGER NOT NULL REFERENCES repositories(id),
               pr_number INTEGER NOT NULL,
               commit_id INTEGER REFERENCES commits(id),
               rule TEXT NOT NULL,
               raw TEXT NOT NULL,
               verdict TEXT NOT NULL CHECK (verdict IN ('yes', 'possible')),
               harness TEXT,
               model TEXT,
               channel TEXT NOT NULL CHECK (channel = 'pr'),
               observed_at TEXT NOT NULL,
               suspicious INTEGER NOT NULL CHECK (suspicious IN (0, 1)),
               UNIQUE (repo_id, pr_number, commit_id, rule, raw)
           )"""
    )


def _step_6(conn: sqlite3.Connection) -> None:
    """PR sweep watermark (ADR-0007 clause 2, ticket #36).

    The Watermark — the last incorporated PR number of the PR-channel sweep
    — recorded state exactly like the ``pr_evidence`` rows it guards: it
    rides the Artifact, survives partial recompute and gather unchanged, and
    is never re-derived. One row per repository; the run machinery advances
    it after each PR's evidence is recorded (rows + watermark, one
    transaction), so every interruption leaves a contiguous processed
    prefix. ``IF NOT EXISTS`` for the same replay reason as steps 4-5."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pr_watermark (
               repo_id INTEGER NOT NULL REFERENCES repositories(id),
               last_pr_number INTEGER NOT NULL,
               PRIMARY KEY (repo_id)
           )"""
    )


SCHEMA_STEPS: list[Step] = [
    STEP_0_SQL,  # 0: initial schema
    _step_1,  # 1: repository identity, repo-scoped commits, prefixed feature ids
    _step_2,  # 2: features.live_lines + feature_line_samples (Sample rows)
    _step_3,  # 3: union-wide feature key (repository, id) — ADR-0006 clause 4
    _step_4,  # 4: AI attribution verdict + evidence rows — ADR-0007 baseline
    _step_5,  # 5: PR-channel evidence rows — ADR-0007 clauses 3-5 (ticket #35)
    _step_6,  # 6: PR sweep watermark — ADR-0007 clause 2 (ticket #36)
]
