"""The table catalogue: every SQLite table's shape declared exactly once.

The Artifact's row shape used to have no owner — it was declared 6-8 times by
hand (artifact DDL + column tuples + canonical SELECT, the schema's FK-ful
DDL, the shape-completion maps, gather's union INSERT column lists and
repo-scoped purge, the ingest ownership predicates, orgview's projection
copies) and the copies were deliberately non-uniform. This module is the ONE
owner: every consumer derives its SQL from here.

It is a LEAF: zero imports from the coding-stats package. artifact, schema,
gather and orgview consume it directly (db, recovery and migration reach it
through artifact); it imports nothing back, so the declaration can never
depend on a consumer.

Declared per table:

* ``columns`` — name, type + constraints (``decl``, WITHOUT any REFERENCES
  clause), and ``ref``: the FK graph. A reference carries ``physical`` —
  True when the working schema's DDL emits the REFERENCES clause; False for
  the logical-only feature-id references (schema step 3 dropped them: SQLite
  cannot FK a composite target from a single column — but artifact ingest
  still enforces the ownership they name, and the union purge sweeps by it).
* ``table_constraints`` — table-level PRIMARY KEY / UNIQUE clauses.
* ``order_by`` — the canonical deterministic ordering (the Artifact SELECT's
  ORDER BY; stable across machines, per the digest requirement).
* ``minted_at`` — the schema step whose shape this declaration IS; on a
  column, when the column arrived later than its table (pr_watermark.
  last_merged_at, step 7).
* ``artifact_sentinel`` — the value the Artifact inserts for a curated-out
  column (ADR-0003 clause 2): wall-clock (repositories.added_at -> ''),
  machine-derivable payload (commits.diff -> ''), run provenance
  (commit_verdicts.run_id / features.created_run_id -> NULL).

Divergence knobs (decision Q9: explicit knobs over policy functions). The
copy plans are declared per direction — "gather" (union ingest) and
"orgview" (org projection) — and each knob's semantics live at its
declaration:

* Conflict policy per direction. gather's plain INSERT on the fix-edge
  tables is a drift guard: the repo's rows were purged moments earlier, so a
  collision means drift and must fail loudly, while orgview keeps INSERT OR
  IGNORE there (re-pointing several loser rows onto one winner row collides
  by design). feature_line_samples is INSERT OR REPLACE in gather (a
  re-gathered key replaces the sample row with its newest artifact's value)
  but INSERT OR IGNORE in orgview (the lowest-live_lines-first read order
  picks the deterministic winner). ai_attribution is INSERT OR REPLACE in
  orgview only (the copies' claims merge into one row); repositories is
  INSERT OR REPLACE in gather (re-ingest rewrites the identity row in
  place).
* Per-direction sentinels. features.created_run_id rides gather verbatim
  (the artifact already carries the NULL sentinel) but orgview writes NULL;
  repositories.added_at rides gather verbatim (the artifact's '' sentinel)
  but orgview writes ''.
* The settings whitelist WHERE: settings rows other than the whitelisted
  keys never reach the Artifact (machine-local or derived state).

``runs`` is deliberately NOT catalogued: machine-local, never in an
Artifact, and declared exactly once (schema.RUNS_DDL) — no copies to unify.
``union_artifacts`` IS catalogued (it was declared twice: gather's DDL and
orgview's projection copy) but flagged ``in_artifact=False`` — union-only
metadata, never minted by a schema step (gather creates it), never in an
Artifact.

Generators (one per surface that used to re-type shapes by hand):
``artifact_ddl`` (FK-stripped, ADR-0003 clause 2), ``fk_ddl`` (working-schema
DDL with physical references), ``canonical_select``, ``complete_steps`` /
``complete_columns`` (shape completion), ``ownership_checks`` (artifact
ingest's one-repository defense), ``purge_statements`` +
``feature_sweep_tables`` (gather's repo-scoped DELETEs), and the per-table
copy plans (``plan_insert_sql`` / ``plan_select_sql`` / ``plan_values``).
"""

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["gather", "orgview"]

# The schema step the "core" settles at: tables minted after it are the ones
# shape completion may resurrect empty (an artifact stamped before the
# table's step); core tables are NEVER resurrected — a replay missing one is
# refused.
CORE_STEP = 3

# The ownership graph: every in-artifact reference must target one of these
# (or ``runs``, whose provenance columns are machine-local and NULL in an
# artifact). ownership_checks derives one predicate per reference to an owner.
_OWNERS = frozenset({"repositories", "commits", "features"})

# Artifact tables the repo-scoped purge never touches: repositories itself
# (the union's identity rows are global), settings (derived, never copied),
# and feature_line_samples (no repo dimension — swept by feature instead).
_PURGE_UNSCOPED = ("repositories", "settings", "feature_line_samples")

# The closed set of copy-plan modes (plan_values dispatches on these).
_MODES = ("verbatim", "literal", "repo_id", "commit_ids", "nullable_commit_ids", "override")

_UNSET = object()  # marks "column carries no Artifact sentinel"


@dataclass(frozen=True)
class Ref:
    """A foreign-key reference: ownership-checked on artifact ingest;
    emitted in the working DDL only when ``physical``."""

    table: str
    column: str = "id"
    physical: bool = True


@dataclass(frozen=True)
class Column:
    name: str
    # type + constraints, REFERENCES excluded (refs are declared separately)
    decl: str
    ref: Ref | None = None
    # schema step minting this column, when LATER than its table's step
    minted_at: int | None = None
    # the curated Artifact's value for this column (see module docstring)
    artifact_sentinel: object = _UNSET

    @property
    def has_sentinel(self) -> bool:
        return self.artifact_sentinel is not _UNSET

    @property
    def nullable(self) -> bool:
        """No NOT NULL and no PRIMARY KEY in the decl — an ownership check
        must guard it with IS NOT NULL (NULL never matches NOT IN)."""
        return "NOT NULL" not in self.decl and "PRIMARY KEY" not in self.decl

    @property
    def type(self) -> str:
        """The declared type: the decl's first token (ALTER TABLE ADD COLUMN
        needs exactly that)."""
        return self.decl.split()[0]


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    table_constraints: tuple[str, ...] = ()
    # canonical deterministic ordering (the Artifact SELECT's ORDER BY)
    order_by: tuple[str, ...] = ()
    # the schema step whose shape this declaration is (None: not in the
    # migration ladder — union_artifacts)
    minted_at: int | None = None
    # the Artifact's settings whitelist WHERE (settings only)
    settings_filter: tuple[str, ...] = ()
    in_artifact: bool = True

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"{self.name}.{name} is not declared")


@dataclass(frozen=True)
class CopyColumn:
    """One column of a copy plan. Modes (semantics at the declaration):

    * ``verbatim`` — the source row's value, copied as stored.
    * ``literal`` — a fixed value: the per-direction sentinel knob
      (NULL for run provenance, '' for wall-clock).
    * ``repo_id`` — the union-space repository id the caller computed.
    * ``commit_ids`` — remap through the caller's commit-id map (total over
      the source's commits by construction; a miss is a dangling edge).
    * ``nullable_commit_ids`` — the same remap, NULL passing through.
    * ``override`` — a per-call value from the caller's loop (union-assigned
      ids, canonicalized identity fields, merged rows).
    """

    name: str
    mode: str
    literal: object = None


@dataclass(frozen=True)
class CopyPlan:
    table: str
    direction: Direction
    # conflict knob: "" (plain INSERT), "OR REPLACE" or "OR IGNORE" —
    # semantics documented per table in the plans below
    conflict: str = ""
    columns: tuple[CopyColumn, ...] = field(default_factory=tuple)
    # source SELECT ordering override; None = the table's canonical order_by
    order_by: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# THE declaration. One Table per table, in the Artifact's canonical body
# order (serialize emits them in this order; it must never change: the
# digest covers the body layout).
# ---------------------------------------------------------------------------

_TABLES: tuple[Table, ...] = (
    Table(
        "repositories",
        (
            Column("id", "INTEGER PRIMARY KEY"),
            Column("repo_key", "TEXT NOT NULL UNIQUE"),
            Column("slug", "TEXT NOT NULL"),
            Column("branch", "TEXT"),
            Column("clone_url", "TEXT"),
            # wall-clock -> '' sentinel (impl-w1-hazard obligation). Knob:
            # gather copies the artifact's '' verbatim; orgview writes ''.
            Column("added_at", "TEXT", artifact_sentinel=""),
        ),
        order_by=("id",),
        minted_at=1,  # minted by migration step 1 (repository identity)
    ),
    Table(
        "commits",
        (
            Column("id", "INTEGER PRIMARY KEY"),
            Column("repo_id", "INTEGER NOT NULL", Ref("repositories")),
            Column("sha", "TEXT NOT NULL"),
            Column("parent_sha", "TEXT"),
            Column("tree_sha", "TEXT NOT NULL"),
            Column("committed_at", "TEXT NOT NULL"),
            Column("author_name", "TEXT NOT NULL"),
            Column("title", "TEXT NOT NULL"),
            Column("message", "TEXT NOT NULL"),
            Column("subject_patch_id", "TEXT"),
            Column("walk_index", "INTEGER NOT NULL"),
            Column("kind", "TEXT NOT NULL CHECK (kind IN ('normal', 'merge'))"),
            # raw diff text is machine-derivable, never committed
            Column("diff", "TEXT NOT NULL", artifact_sentinel=""),
            Column("added_lines", "INTEGER NOT NULL DEFAULT 0"),
            Column("deleted_lines", "INTEGER NOT NULL DEFAULT 0"),
            Column("churn_lines", "INTEGER NOT NULL DEFAULT 0"),
        ),
        table_constraints=("UNIQUE (repo_id, sha)", "UNIQUE (repo_id, walk_index)"),
        order_by=("id",),
        minted_at=1,  # step 1 rebuilt it at this shape
    ),
    Table(
        "commit_verdicts",
        (
            Column("commit_id", "INTEGER PRIMARY KEY", Ref("commits")),
            Column(
                "verdict",
                "TEXT NOT NULL CHECK (verdict IN ('feature', 'fix', 'refactor',"
                " 'revert', 'cleanup', 'merge', 'unknown'))",
            ),
            Column("rationale", "TEXT NOT NULL"),
            Column("raw_llm_output", "TEXT NOT NULL"),
            Column("model", "TEXT NOT NULL"),
            # run provenance is machine-local: NULL in the Artifact; NULL in
            # both copy directions (the union has no runs rows)
            Column("run_id", "INTEGER", Ref("runs"), artifact_sentinel=None),
            Column("classified_at", "TEXT NOT NULL"),
        ),
        order_by=("commit_id",),
        minted_at=0,
    ),
    Table(
        "ai_attribution",
        (
            Column("commit_id", "INTEGER PRIMARY KEY", Ref("commits")),
            Column(
                "verdict",
                "TEXT NOT NULL CHECK (verdict IN ('yes', 'possible', 'no-evidence'))",
            ),
            Column("harness", "TEXT"),
            Column("model", "TEXT"),
        ),
        order_by=("commit_id",),
        minted_at=4,
    ),
    Table(
        "ai_evidence",
        (
            Column("commit_id", "INTEGER NOT NULL", Ref("commits")),
            Column("rule", "TEXT NOT NULL"),
            Column("raw", "TEXT NOT NULL"),
            Column("verdict", "TEXT NOT NULL CHECK (verdict IN ('yes', 'possible'))"),
        ),
        table_constraints=("PRIMARY KEY (commit_id, rule, raw)",),
        order_by=("commit_id", "rule", "raw"),
        minted_at=4,
    ),
    Table(
        "pr_evidence",
        (
            Column("repo_id", "INTEGER NOT NULL", Ref("repositories")),
            Column("pr_number", "INTEGER NOT NULL"),
            # NULL exactly for suspicious rows (head-parentage check failed)
            Column("commit_id", "INTEGER", Ref("commits")),
            Column("rule", "TEXT NOT NULL"),
            Column("raw", "TEXT NOT NULL"),
            Column("verdict", "TEXT NOT NULL CHECK (verdict IN ('yes', 'possible'))"),
            Column("harness", "TEXT"),
            Column("model", "TEXT"),
            Column("channel", "TEXT NOT NULL CHECK (channel = 'pr')"),
            Column("observed_at", "TEXT NOT NULL"),
            Column("suspicious", "INTEGER NOT NULL CHECK (suspicious IN (0, 1))"),
        ),
        table_constraints=("UNIQUE (repo_id, pr_number, commit_id, rule, raw)",),
        order_by=("repo_id", "pr_number", "commit_id", "rule", "raw"),
        minted_at=5,
    ),
    Table(
        "pr_watermark",
        (
            Column("repo_id", "INTEGER NOT NULL", Ref("repositories")),
            Column("last_pr_number", "INTEGER NOT NULL"),
            # the resume marker's merge date; minted at step 7, NULL on a
            # step-6 row (a marker that claims no merge date)
            Column("last_merged_at", "TEXT", minted_at=7),
        ),
        table_constraints=("PRIMARY KEY (repo_id)",),
        order_by=("repo_id",),
        minted_at=6,
    ),
    Table(
        "pr_disclosure",
        (
            Column("repo_id", "INTEGER NOT NULL", Ref("repositories")),
        ),
        table_constraints=("PRIMARY KEY (repo_id)",),
        order_by=("repo_id",),
        minted_at=8,
    ),
    Table(
        "features",
        (
            Column("id", "TEXT NOT NULL"),
            Column("title", "TEXT NOT NULL"),
            Column("about", "TEXT NOT NULL DEFAULT ''"),
            Column("title_history_json", "TEXT NOT NULL DEFAULT '[]'"),
            Column("created_at", "TEXT NOT NULL"),
            # Knob: gather copies the artifact's NULL verbatim; orgview
            # writes NULL (a projected feature keeps no run provenance).
            Column("created_run_id", "INTEGER", Ref("runs"), artifact_sentinel=None),
            Column("repo_id", "INTEGER NOT NULL", Ref("repositories")),
            Column("live_lines", "INTEGER NOT NULL DEFAULT 0"),
        ),
        table_constraints=("PRIMARY KEY (repo_id, id)",),
        order_by=("repo_id", "id"),
        minted_at=3,  # step 3 rebuilt it at this shape
    ),
    Table(
        "commits_features",
        (
            Column("commit_id", "INTEGER NOT NULL", Ref("commits")),
            # step 3 dropped the physical REFERENCES features(id) (composite
            # target un-FK-able from one column); ownership is still checked
            Column("feature_id", "TEXT NOT NULL", Ref("features", physical=False)),
            Column("role", "TEXT NOT NULL CHECK (role IN ('defines', 'touches'))"),
        ),
        table_constraints=(
            "PRIMARY KEY (commit_id, feature_id)",
            "UNIQUE (feature_id, commit_id)",
        ),
        order_by=("commit_id", "feature_id"),
        minted_at=3,
    ),
    Table(
        "fix_touches",
        (
            Column("fix_commit_id", "INTEGER NOT NULL", Ref("commits")),
            Column("source_commit_id", "INTEGER NOT NULL", Ref("commits")),
            Column("hit_lines", "INTEGER NOT NULL"),
        ),
        table_constraints=("PRIMARY KEY (fix_commit_id, source_commit_id)",),
        order_by=("fix_commit_id", "source_commit_id"),
        minted_at=0,
    ),
    Table(
        "fix_targets",
        (
            Column("fix_commit_id", "INTEGER NOT NULL", Ref("commits")),
            Column("target_commit_id", "INTEGER NOT NULL", Ref("commits")),
        ),
        table_constraints=("PRIMARY KEY (fix_commit_id, target_commit_id)",),
        order_by=("fix_commit_id", "target_commit_id"),
        minted_at=0,
    ),
    Table(
        "fixes_features",
        (
            Column("fix_commit_id", "INTEGER NOT NULL", Ref("commits")),
            Column("feature_id", "TEXT NOT NULL", Ref("features", physical=False)),
            # NULL when the first-hop target defines the feature directly
            Column("via_fix_commit_id", "INTEGER", Ref("commits")),
        ),
        table_constraints=(
            "PRIMARY KEY (fix_commit_id, feature_id)",
            "UNIQUE (fix_commit_id, feature_id, via_fix_commit_id)",
        ),
        order_by=("fix_commit_id", "feature_id", "via_fix_commit_id"),
        minted_at=3,
    ),
    Table(
        "feature_line_samples",
        (
            Column("feature_id", "TEXT NOT NULL", Ref("features", physical=False)),
            Column("at_commit_sha", "TEXT NOT NULL"),
            Column("live_lines", "INTEGER NOT NULL"),
        ),
        table_constraints=("PRIMARY KEY (feature_id, at_commit_sha)",),
        order_by=("feature_id", "at_commit_sha"),
        minted_at=3,
    ),
    Table(
        "settings",
        (
            Column("key", "TEXT PRIMARY KEY"),
            Column("value", "TEXT NOT NULL"),
        ),
        order_by=("key",),
        minted_at=0,
        # The Artifact's settings whitelist (knob): everything else — repo
        # (machine path), classify_session_id, repo_id, branch extras — is
        # machine-local or derived and never curated out.
        settings_filter=(
            "backfill_state",
            "branch",
            "min_target_lines",
            "model",
            "unattributed_lines",
        ),
    ),
    Table(
        "union_artifacts",
        (
            Column("repo_key", "TEXT PRIMARY KEY"),
            Column("artifact_sha256", "TEXT NOT NULL"),
            Column("covered_through", "TEXT NOT NULL"),
            Column("backfill_state", "TEXT NOT NULL"),
            Column("unattributed_lines", "INTEGER NOT NULL"),
            Column("model", "TEXT NOT NULL"),
            Column("min_target_lines", "INTEGER NOT NULL"),
            Column("source_path", "TEXT NOT NULL"),
        ),
        order_by=("repo_key",),
        minted_at=None,  # not in the migration ladder: gather mints it
        in_artifact=False,
    ),
)

# ---------------------------------------------------------------------------
# Copy plans — the per-direction divergence knobs. Column lists here are
# validated against their table at import (see _validate): a plan that
# drifts from the declaration fails at import, not in production.
# ---------------------------------------------------------------------------

_PLANS: tuple[CopyPlan, ...] = (
    # repositories, gather: OR REPLACE — re-ingest rewrites the identity row
    # in place (canonicalized key/slug/clone_url ride the ingest, not the
    # artifact row). added_at rides verbatim: the artifact already carries
    # the '' sentinel.
    CopyPlan(
        "repositories",
        "gather",
        conflict="OR REPLACE",
        columns=(
            CopyColumn("id", "override"),
            CopyColumn("repo_key", "override"),
            CopyColumn("slug", "override"),
            CopyColumn("branch", "verbatim"),
            CopyColumn("clone_url", "override"),
            CopyColumn("added_at", "verbatim"),
        ),
    ),
    # repositories, orgview: plain INSERT; added_at is the '' wall-clock
    # sentinel (a projection never carries the wall-clock value).
    CopyPlan(
        "repositories",
        "orgview",
        columns=(
            CopyColumn("id", "verbatim"),
            CopyColumn("repo_key", "verbatim"),
            CopyColumn("slug", "verbatim"),
            CopyColumn("branch", "verbatim"),
            CopyColumn("clone_url", "verbatim"),
            CopyColumn("added_at", "literal", ""),
        ),
    ),
    # commits, gather: the id is union-assigned (override), repo_id the
    # union's; everything else verbatim. Plain INSERT (drift guard: the
    # repo's commits were purged moments earlier).
    CopyPlan(
        "commits",
        "gather",
        columns=(
            CopyColumn("id", "override"),
            CopyColumn("repo_id", "repo_id"),
            CopyColumn("sha", "verbatim"),
            CopyColumn("parent_sha", "verbatim"),
            CopyColumn("tree_sha", "verbatim"),
            CopyColumn("committed_at", "verbatim"),
            CopyColumn("author_name", "verbatim"),
            CopyColumn("title", "verbatim"),
            CopyColumn("message", "verbatim"),
            CopyColumn("subject_patch_id", "verbatim"),
            CopyColumn("walk_index", "verbatim"),
            CopyColumn("kind", "verbatim"),
            CopyColumn("diff", "verbatim"),
            CopyColumn("added_lines", "verbatim"),
            CopyColumn("deleted_lines", "verbatim"),
            CopyColumn("churn_lines", "verbatim"),
        ),
    ),
    # commits, orgview: the winner row verbatim (ids are already union-space).
    CopyPlan(
        "commits",
        "orgview",
        columns=tuple(CopyColumn(name, "verbatim") for name in (
            "id", "repo_id", "sha", "parent_sha", "tree_sha", "committed_at",
            "author_name", "title", "message", "subject_patch_id", "walk_index",
            "kind", "diff", "added_lines", "deleted_lines", "churn_lines",
        )),
    ),
    # commit_verdicts: run provenance NULL in both directions (knob).
    CopyPlan(
        "commit_verdicts",
        "gather",
        columns=(
            CopyColumn("commit_id", "commit_ids"),
            CopyColumn("verdict", "verbatim"),
            CopyColumn("rationale", "verbatim"),
            CopyColumn("raw_llm_output", "verbatim"),
            CopyColumn("model", "verbatim"),
            CopyColumn("run_id", "literal", None),
            CopyColumn("classified_at", "verbatim"),
        ),
    ),
    CopyPlan(
        "commit_verdicts",
        "orgview",
        columns=(
            CopyColumn("commit_id", "verbatim"),
            CopyColumn("verdict", "verbatim"),
            CopyColumn("rationale", "verbatim"),
            CopyColumn("raw_llm_output", "verbatim"),
            CopyColumn("model", "verbatim"),
            CopyColumn("run_id", "literal", None),
            CopyColumn("classified_at", "verbatim"),
        ),
    ),
    CopyPlan(
        "ai_attribution",
        "gather",
        columns=(
            CopyColumn("commit_id", "commit_ids"),
            CopyColumn("verdict", "verbatim"),
            CopyColumn("harness", "verbatim"),
            CopyColumn("model", "verbatim"),
        ),
    ),
    # ai_attribution, orgview: OR REPLACE — the copies' claims merge into ONE
    # row (strongest verdict wins; harness/model fill winner-first).
    CopyPlan(
        "ai_attribution",
        "orgview",
        conflict="OR REPLACE",
        columns=(
            CopyColumn("commit_id", "override"),
            CopyColumn("verdict", "override"),
            CopyColumn("harness", "override"),
            CopyColumn("model", "override"),
        ),
    ),
    CopyPlan(
        "ai_evidence",
        "gather",
        columns=(
            CopyColumn("commit_id", "commit_ids"),
            CopyColumn("rule", "verbatim"),
            CopyColumn("raw", "verbatim"),
            CopyColumn("verdict", "verbatim"),
        ),
    ),
    # ai_evidence, orgview: OR IGNORE — every copy's git evidence lands on
    # the winner, deduped on (rule, raw).
    CopyPlan(
        "ai_evidence",
        "orgview",
        conflict="OR IGNORE",
        columns=(
            CopyColumn("commit_id", "override"),
            CopyColumn("rule", "verbatim"),
            CopyColumn("raw", "verbatim"),
            CopyColumn("verdict", "verbatim"),
        ),
    ),
    CopyPlan(
        "pr_evidence",
        "gather",
        columns=(
            CopyColumn("repo_id", "repo_id"),
            CopyColumn("pr_number", "verbatim"),
            CopyColumn("commit_id", "nullable_commit_ids"),
            CopyColumn("rule", "verbatim"),
            CopyColumn("raw", "verbatim"),
            CopyColumn("verdict", "verbatim"),
            CopyColumn("harness", "verbatim"),
            CopyColumn("model", "verbatim"),
            CopyColumn("channel", "verbatim"),
            CopyColumn("observed_at", "verbatim"),
            CopyColumn("suspicious", "verbatim"),
        ),
    ),
    # pr_evidence, orgview: repo_id verbatim (union ids already global);
    # commit re-pointed through the dedup map; plain INSERT (the loop's
    # seen-sets dedup first).
    CopyPlan(
        "pr_evidence",
        "orgview",
        columns=(
            CopyColumn("repo_id", "verbatim"),
            CopyColumn("pr_number", "verbatim"),
            CopyColumn("commit_id", "nullable_commit_ids"),
            CopyColumn("rule", "verbatim"),
            CopyColumn("raw", "verbatim"),
            CopyColumn("verdict", "verbatim"),
            CopyColumn("harness", "verbatim"),
            CopyColumn("model", "verbatim"),
            CopyColumn("channel", "verbatim"),
            CopyColumn("observed_at", "verbatim"),
            CopyColumn("suspicious", "verbatim"),
        ),
    ),
    CopyPlan(
        "pr_watermark",
        "gather",
        columns=(
            CopyColumn("repo_id", "repo_id"),
            CopyColumn("last_pr_number", "verbatim"),
            CopyColumn("last_merged_at", "verbatim"),
        ),
    ),
    CopyPlan(
        "pr_watermark",
        "orgview",
        columns=(
            CopyColumn("repo_id", "verbatim"),
            CopyColumn("last_pr_number", "verbatim"),
            CopyColumn("last_merged_at", "verbatim"),
        ),
    ),
    CopyPlan(
        "pr_disclosure",
        "gather",
        columns=(CopyColumn("repo_id", "repo_id"),),
    ),
    CopyPlan(
        "pr_disclosure",
        "orgview",
        columns=(CopyColumn("repo_id", "verbatim"),),
    ),
    # features, gather: created_run_id rides VERBATIM (the artifact carries
    # the NULL sentinel — knob); orgview writes NULL.
    CopyPlan(
        "features",
        "gather",
        columns=(
            CopyColumn("id", "verbatim"),
            CopyColumn("title", "verbatim"),
            CopyColumn("about", "verbatim"),
            CopyColumn("title_history_json", "verbatim"),
            CopyColumn("created_at", "verbatim"),
            CopyColumn("created_run_id", "verbatim"),
            CopyColumn("repo_id", "repo_id"),
            CopyColumn("live_lines", "verbatim"),
        ),
    ),
    CopyPlan(
        "features",
        "orgview",
        columns=(
            CopyColumn("id", "verbatim"),
            CopyColumn("title", "verbatim"),
            CopyColumn("about", "verbatim"),
            CopyColumn("title_history_json", "verbatim"),
            CopyColumn("created_at", "verbatim"),
            CopyColumn("created_run_id", "literal", None),
            CopyColumn("repo_id", "override"),
            CopyColumn("live_lines", "verbatim"),
        ),
    ),
    CopyPlan(
        "commits_features",
        "gather",
        columns=(
            CopyColumn("commit_id", "commit_ids"),
            CopyColumn("feature_id", "verbatim"),
            CopyColumn("role", "verbatim"),
        ),
    ),
    # commits_features, orgview: winner rows keep their ids verbatim (loser
    # rows are skipped before the insert); OR IGNORE for re-point collides.
    CopyPlan(
        "commits_features",
        "orgview",
        conflict="OR IGNORE",
        columns=(
            CopyColumn("commit_id", "verbatim"),
            CopyColumn("feature_id", "verbatim"),
            CopyColumn("role", "verbatim"),
        ),
    ),
    CopyPlan(
        "fix_touches",
        "gather",
        columns=(
            CopyColumn("fix_commit_id", "commit_ids"),
            CopyColumn("source_commit_id", "commit_ids"),
            CopyColumn("hit_lines", "verbatim"),
        ),
    ),
    CopyPlan(
        "fix_touches",
        "orgview",
        conflict="OR IGNORE",
        columns=(
            CopyColumn("fix_commit_id", "commit_ids"),
            CopyColumn("source_commit_id", "commit_ids"),
            CopyColumn("hit_lines", "verbatim"),
        ),
    ),
    CopyPlan(
        "fix_targets",
        "gather",
        columns=(
            CopyColumn("fix_commit_id", "commit_ids"),
            CopyColumn("target_commit_id", "commit_ids"),
        ),
    ),
    CopyPlan(
        "fix_targets",
        "orgview",
        conflict="OR IGNORE",
        columns=(
            CopyColumn("fix_commit_id", "commit_ids"),
            CopyColumn("target_commit_id", "commit_ids"),
        ),
    ),
    CopyPlan(
        "fixes_features",
        "gather",
        columns=(
            CopyColumn("fix_commit_id", "commit_ids"),
            CopyColumn("feature_id", "verbatim"),
            CopyColumn("via_fix_commit_id", "nullable_commit_ids"),
        ),
    ),
    CopyPlan(
        "fixes_features",
        "orgview",
        conflict="OR IGNORE",
        columns=(
            CopyColumn("fix_commit_id", "commit_ids"),
            CopyColumn("feature_id", "verbatim"),
            CopyColumn("via_fix_commit_id", "nullable_commit_ids"),
        ),
    ),
    # feature_line_samples, gather: OR REPLACE — a duplicated (seeded)
    # feature id's sample row is shared in the union; a re-gathered key
    # replaces it with the newest artifact's value. orgview: OR IGNORE with
    # the lowest-live_lines-first read order as the deterministic winner.
    CopyPlan(
        "feature_line_samples",
        "gather",
        conflict="OR REPLACE",
        columns=(
            CopyColumn("feature_id", "verbatim"),
            CopyColumn("at_commit_sha", "verbatim"),
            CopyColumn("live_lines", "verbatim"),
        ),
    ),
    CopyPlan(
        "feature_line_samples",
        "orgview",
        conflict="OR IGNORE",
        order_by=("feature_id", "at_commit_sha", "live_lines"),
        columns=(
            CopyColumn("feature_id", "verbatim"),
            CopyColumn("at_commit_sha", "verbatim"),
            CopyColumn("live_lines", "verbatim"),
        ),
    ),
    CopyPlan(
        "settings",
        "orgview",
        columns=(
            CopyColumn("key", "verbatim"),
            CopyColumn("value", "verbatim"),
        ),
    ),
    CopyPlan(
        "union_artifacts",
        "orgview",
        columns=tuple(CopyColumn(name, "verbatim") for name in (
            "repo_key", "artifact_sha256", "covered_through", "backfill_state",
            "unattributed_lines", "model", "min_target_lines", "source_path",
        )),
    ),
)

# settings has NO gather plan: the union's settings rows are DERIVED from the
# constituents (gather._write_union_settings), never copied.
_NO_GATHER_PLAN = ("settings",)


# ---------------------------------------------------------------------------
# Lookup and validation
# ---------------------------------------------------------------------------


def tables() -> tuple[Table, ...]:
    """Every declared table, declaration order."""
    return _TABLES


def artifact_tables() -> tuple[Table, ...]:
    """The tables that make up the Artifact's curated body (declaration
    order = the Artifact's body order)."""
    return tuple(t for t in _TABLES if t.in_artifact)


def table(name: str) -> Table:
    for t in _TABLES:
        if t.name == name:
            return t
    raise KeyError(f"table {name!r} is not declared in the catalogue")


def copy_plan(name: str, direction: Direction) -> CopyPlan:
    for plan in _PLANS:
        if plan.table == name and plan.direction == direction:
            return plan
    raise KeyError(f"no {direction} copy plan declared for {name!r}")


def copy_tables(direction: Direction) -> tuple[str, ...]:
    """Tables with a copy plan for the direction, declaration order."""
    return tuple(
        t.name for t in _TABLES if any(
            p.table == t.name and p.direction == direction for p in _PLANS
        )
    )


def _validate() -> None:
    seen: set[str] = set()
    for t in _TABLES:
        if t.name in seen:
            raise RuntimeError(f"table {t.name} declared twice")
        seen.add(t.name)
        names = [c.name for c in t.columns]
        if len(set(names)) != len(names):
            raise RuntimeError(f"table {t.name} declares a column twice")
        if not set(t.order_by) <= set(names):
            raise RuntimeError(f"table {t.name}: order_by outside its columns")
        if t.order_by and not any(
            uniq[: len(t.order_by)] == t.order_by for uniq in _unique_sets(t)
        ):
            raise RuntimeError(
                f"{t.name}: order_by {t.order_by} prefixes no declared UNIQUE set"
                " — the canonical SELECT would not be a total order"
            )
        if t.settings_filter and t.name != "settings":
            raise RuntimeError(f"settings_filter on non-settings table {t.name}")
        if t.in_artifact and t.minted_at is None:
            raise RuntimeError(f"{t.name}: artifact table without a minted_at step")
        if t.in_artifact and not any(
            c.ref is not None and c.ref.table in ("repositories", "commits")
            for c in t.columns
        ) and t.name not in _PURGE_UNSCOPED:
            raise RuntimeError(
                f"{t.name}: holds repo rows but the purge cannot scope it"
                " (no repositories/commits reference)"
            )
        for c in t.columns:
            if c.minted_at is not None and t.minted_at is not None and c.minted_at <= t.minted_at:
                raise RuntimeError(
                    f"{t.name}.{c.name}: column minted_at must exceed the table's"
                )
            if c.ref is not None and c.ref.table not in (*_seen_before(seen, t.name), "runs"):
                raise RuntimeError(f"{t.name}.{c.name} references undeclared {c.ref.table}")
            if t.in_artifact and c.ref is not None and c.ref.table not in (*_OWNERS, "runs"):
                raise RuntimeError(
                    f"{t.name}.{c.name}: reference to {c.ref.table} is outside the"
                    " ownership graph — the one-repository defense would not see it"
                )
            if c.ref is not None and c.ref.table == "features" and c.name != "feature_id":
                raise RuntimeError(
                    f"{t.name}.{c.name}: the feature sweep's DELETE hardcodes"
                    " feature_id — a features reference must carry that name"
                )
    for plan in _PLANS:
        t = table(plan.table)
        declared = t.names
        planned = tuple(c.name for c in plan.columns)
        if planned != declared:
            raise RuntimeError(
                f"{plan.direction} plan for {plan.table} does not match the declaration"
            )
        if plan.conflict not in ("", "OR REPLACE", "OR IGNORE"):
            raise RuntimeError(f"{plan.table}/{plan.direction}: bad conflict knob")
        if plan.direction == "gather" and plan.table in _NO_GATHER_PLAN:
            raise RuntimeError(f"{plan.table} must not carry a gather plan")
        if plan.order_by is not None and not set(plan.order_by) <= set(declared):
            raise RuntimeError(f"{plan.table}/{plan.direction}: order_by outside columns")
        for pc in plan.columns:
            if pc.mode not in _MODES:
                raise RuntimeError(
                    f"{plan.table}/{plan.direction}: unknown copy mode {pc.mode!r}"
                )
            if pc.mode in ("commit_ids", "nullable_commit_ids"):
                col = t.column(pc.name)
                if col.ref is None or col.ref.table != "commits":
                    raise RuntimeError(
                        f"{plan.table}/{plan.direction}.{pc.name}: commit remap on a"
                        " column that does not reference commits"
                    )
                if pc.mode == "nullable_commit_ids" and not col.nullable:
                    raise RuntimeError(
                        f"{plan.table}/{plan.direction}.{pc.name}: nullable remap on"
                        " a NOT NULL column (use commit_ids)"
                    )
            if pc.mode == "repo_id":
                col = t.column(pc.name)
                if col.ref is None or col.ref.table != "repositories":
                    raise RuntimeError(
                        f"{plan.table}/{plan.direction}.{pc.name}: repo_id mode on a"
                        " column that does not reference repositories"
                    )
    # Plan coverage: a declared artifact table without a copy plan is purged
    # but never re-inserted — silent data loss in the union. Required HERE,
    # at import, not only in tests.
    for t in artifact_tables():
        for direction in ("gather", "orgview"):
            if direction == "gather" and t.name in _NO_GATHER_PLAN:
                continue
            try:
                copy_plan(t.name, direction)
            except KeyError:
                raise RuntimeError(
                    f"{t.name}: no {direction} copy plan — every artifact table"
                    " declares both directions (settings excepted for gather)"
                ) from None
    # The purge's DELETE order must be FK-safe: every purged physical parent
    # comes after all its purged physical children.
    order = [sql.split()[2] for sql, _n in purge_statements()]
    position = {name: i for i, name in enumerate(order)}
    for t in artifact_tables():
        if t.name not in position:
            continue
        for c in t.columns:
            if (
                c.ref is not None
                and c.ref.physical
                and c.ref.table in position
                and position[t.name] > position[c.ref.table]
            ):
                raise RuntimeError(
                    f"purge deletes {c.ref.table} before its child {t.name}"
                )


def _seen_before(seen: set[str], name: str) -> set[str]:
    """Tables declared before ``name`` (references must point backwards)."""
    out: set[str] = set()
    for t in _TABLES:
        if t.name == name:
            break
        out.add(t.name)
    return seen & out


def _unique_sets(t: Table) -> tuple[tuple[str, ...], ...]:
    """Every declared unique column set: table-level PRIMARY KEY / UNIQUE
    constraints plus inline PRIMARY KEY decls."""
    out: list[tuple[str, ...]] = []
    for c in t.columns:
        if "PRIMARY KEY" in c.decl:
            out.append((c.name,))
    for cons in t.table_constraints:
        m = re.match(r"(?:PRIMARY KEY|UNIQUE) \(([^)]*)\)$", cons)
        if m:
            out.append(tuple(x.strip() for x in m.group(1).split(",")))
    return tuple(out)


# ---------------------------------------------------------------------------
# Surface 1+2: DDL generation
# ---------------------------------------------------------------------------


def _ddl_lines(t: Table, refs: bool, if_not_exists: bool) -> list[str]:
    head = f"CREATE TABLE {'IF NOT EXISTS ' if if_not_exists else ''}{t.name} ("
    lines = [head]
    for c in t.columns:
        decl = c.decl
        if refs and c.ref is not None and c.ref.physical:
            decl += f" REFERENCES {c.ref.table}({c.ref.column})"
        lines.append(f"    {c.name} {decl},")
    lines.extend(f"    {tc}," for tc in t.table_constraints)
    lines[-1] = lines[-1].removesuffix(",")
    lines.append(");")
    return lines


def artifact_ddl(t: Table) -> str:
    """The Artifact's CREATE TABLE: the curated shape, cross-table references
    STRIPPED so the dump materializes standalone (ADR-0003 clause 2)."""
    return "\n".join(_ddl_lines(t, refs=False, if_not_exists=False))


def fk_ddl(t: Table, if_not_exists: bool = False) -> str:
    """The working-schema CREATE TABLE: the same shape with its physical
    REFERENCES clauses (the FK graph's enforceable edges)."""
    return "\n".join(_ddl_lines(t, refs=True, if_not_exists=if_not_exists))


# ---------------------------------------------------------------------------
# Surface 1: canonical SELECT (the Artifact's read side)
# ---------------------------------------------------------------------------


def canonical_select(t: Table) -> str:
    """The deterministic SELECT behind the Artifact's rows: curated columns,
    canonical ORDER BY, the settings whitelist WHERE on settings."""
    sql = f"SELECT {', '.join(t.names)} FROM {t.name}"
    if t.settings_filter:
        sql += f" WHERE key IN ({', '.join(repr(k) for k in t.settings_filter)})"
    return f"{sql} ORDER BY {', '.join(t.order_by)}"


# ---------------------------------------------------------------------------
# Surface 6: shape completion (post-core tables and late columns)
# ---------------------------------------------------------------------------


def complete_steps() -> dict[str, int]:
    """Post-core tables (minted after CORE_STEP) with their steps: shape
    completion may resurrect exactly THESE empty, for artifacts whose header
    stamp predates the table's own step. Core tables are never resurrected."""
    return {
        t.name: t.minted_at
        for t in artifact_tables()
        if t.minted_at is not None and t.minted_at > CORE_STEP
    }


def complete_columns() -> dict[tuple[str, str], int]:
    """Columns minted after their table, with the step that minted them."""
    return {
        (t.name, c.name): c.minted_at
        for t in artifact_tables()
        for c in t.columns
        if c.minted_at is not None
    }


# ---------------------------------------------------------------------------
# Surface 5: ownership predicates (the one-repository defense)
# ---------------------------------------------------------------------------


def ownership_checks(rid: int) -> list[tuple[str, str]]:
    """(table, SQL) for every artifact table holding rows that must belong to
    the artifact's single repository: one predicate per reference to an
    in-artifact table. References to ``runs`` are skipped — the runs table is
    machine-local and absent from an artifact; its provenance columns are
    NULL there by curation."""
    out: list[tuple[str, str]] = []
    for t in artifact_tables():
        preds: list[str] = []
        for c in t.columns:
            if c.ref is None or c.ref.table not in _OWNERS:
                continue
            if c.ref.table == "repositories":
                preds.append(f"{c.name} != {rid}")
            else:
                subquery = f"(SELECT id FROM {c.ref.table})"
                if c.nullable:
                    preds.append(f"({c.name} IS NOT NULL AND {c.name} NOT IN {subquery})")
                else:
                    preds.append(f"{c.name} NOT IN {subquery}")
        if preds:
            sql = f"SELECT COUNT(*) c FROM {t.name} WHERE {' OR '.join(preds)}"
            out.append((t.name, sql))
    return out


# ---------------------------------------------------------------------------
# Surface 4: the repo-scoped purge (children first — FK enforcement is on)
# ---------------------------------------------------------------------------


def purge_statements() -> list[tuple[str, int]]:
    """(SQL, placeholder count) deleting ONE repository's union rows. Tables
    with a repositories reference delete by repo_id; the rest by their commit
    references (OR-joined). features and commits go last (children first);
    feature_line_samples is never repo-scoped (no repo dimension — its
    feature-scoped rows are swept separately, see feature_sweep_tables);
    settings and repositories itself are untouched."""
    sub = "(SELECT id FROM commits WHERE repo_id = ?)"
    out: list[tuple[str, str]] = []
    last: list[tuple[str, str]] = []
    for t in artifact_tables():
        if t.name in _PURGE_UNSCOPED:
            continue
        repo_scoped = any(
            c.ref is not None and c.ref.table == "repositories" for c in t.columns
        )
        if repo_scoped:
            where = "repo_id = ?"
        else:
            cols = [c for c in t.columns if c.ref is not None and c.ref.table == "commits"]
            if not cols:
                continue
            where = " OR ".join(f"{c.name} IN {sub}" for c in cols)
        sql = f"DELETE FROM {t.name} WHERE {where}"
        if t.name == "features":
            last.insert(0, (sql, t.name))  # features before commits: parents last
        elif t.name == "commits":
            last.append((sql, t.name))
        else:
            out.append((sql, t.name))
    return [(sql, sql.count("?")) for sql, _t in out + last]


def feature_sweep_tables() -> tuple[str, ...]:
    """Tables holding feature-scoped rows with no repo dimension: purged by
    the orphan sweep (rows whose feature id exists under no key anymore)."""
    return tuple(
        t.name
        for t in artifact_tables()
        if any(c.ref is not None and c.ref.table == "features" for c in t.columns)
        and not any(c.ref is not None and c.ref.table == "repositories" for c in t.columns)
    )


# ---------------------------------------------------------------------------
# Surface 3: the union INSERT (copy plans)
# ---------------------------------------------------------------------------


def plan_insert_sql(plan: CopyPlan) -> str:
    """The plan's INSERT: fixed conflict knob, declared column list, one
    placeholder per column."""
    conflict = f"{plan.conflict} " if plan.conflict else ""
    cols = ", ".join(c.name for c in plan.columns)
    marks = ", ".join("?" for _ in plan.columns)
    return f"INSERT {conflict}INTO {plan.table} ({cols}) VALUES ({marks})"


def plan_select_sql(plan: CopyPlan) -> str:
    """The plan's source SELECT (deterministic: canonical order, or the
    plan's declared override)."""
    order = plan.order_by if plan.order_by is not None else table(plan.table).order_by
    return f"SELECT * FROM {plan.table} ORDER BY {', '.join(order)}"


def plan_values(
    plan: CopyPlan,
    row: sqlite3.Row,
    *,
    repo_id: int | None = None,
    commit_ids: Mapping[int, int] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> tuple[object, ...]:
    """The INSERT's parameter tuple for one source row, per the plan's modes."""
    overrides = overrides or {}
    out: list[object] = []
    for c in plan.columns:
        if c.mode == "verbatim":
            out.append(row[c.name])
        elif c.mode == "literal":
            out.append(c.literal)
        elif c.mode == "repo_id":
            if repo_id is None:
                raise RuntimeError(f"{plan.table}/{plan.direction}: repo_id mode needs repo_id")
            out.append(repo_id)
        elif c.mode == "commit_ids":
            if commit_ids is None:
                raise RuntimeError(f"{plan.table}/{plan.direction}: commit_ids mode needs the map")
            out.append(commit_ids[int(row[c.name])])
        elif c.mode == "nullable_commit_ids":
            if commit_ids is None:
                raise RuntimeError(f"{plan.table}/{plan.direction}: commit_ids mode needs the map")
            value = row[c.name]
            out.append(None if value is None else commit_ids[int(value)])
        elif c.mode == "override":
            if c.name not in overrides:
                raise RuntimeError(f"{plan.table}/{plan.direction}: override for {c.name} missing")
            out.append(overrides[c.name])
        else:  # pragma: no cover - _validate keeps modes closed
            raise RuntimeError(f"unknown copy mode {c.mode!r}")
    return tuple(out)


# Import-time validation runs last: it consumes the generators
# (purge_statements) and every lookup above, so a drifting declaration or
# copy plan fails the import — in tests AND in production — before any
# consumer derives SQL from a shape that does not hold together.
_validate()
