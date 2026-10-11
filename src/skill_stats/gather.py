"""Gather: ingest N artifacts into a union DB keyed by Repository key (ADR-0003).

The union DB is DERIVED from artifacts — it is a normal skill-stats DB (same
tables, same joins) so report/metrics read it unchanged; feature ids are
already globally-unique prefixed TEXT and need no remapping (grill-r4-Q14).
Commit ids are artifact-local integers: the union remaps them into fresh
union-space ints in deterministic ingest order (sources sorted by Repository
key, stable within a key), rebuilding every attribution edge through the
(repo, sha) pairs. The union's cross-DB-id effect: commit ids are union-local
and meaningless outside the union; nothing displays them (reports surface
shas), and UNIQUE (repo_id, sha) catches surprises.

Rules the ingest enforces:
* verify first — a non-verifying artifact is refused (nothing is built on it);
* two clones of the same repository are ONE identity (same key): re-ingesting
  identical content is an idempotent no-op, a different artifact of the same
  key UPSERTS that repo's rows (last-ingest-wins; union_artifacts records the
  winning digest/coverage);
* distinct keys sharing a URL-derived slug (same repo on two Target branches)
  make gather FAIL with a clear error naming the keys — never rename
  (grill-r5-Q18.2);
* the \\xNN header convention is decoded and cross-checked against the rows.

Union-only metadata lives in `union_artifacts` (per-repo digest, coverage
horizon, backfill_state, display fields) — a separate table so the
repositories shape stays identical to a single-repo DB. The union has no
backfill_state of its own: sampling completeness is derived at generate time
as the LEAST complete constituent (see metrics.sampling_complete).
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import artifact, db

STORE_REL = ".skill-stats/skill-stats.sql"

_UNION_ARTIFACTS_DDL = """
CREATE TABLE IF NOT EXISTS union_artifacts (
    repo_key TEXT PRIMARY KEY,
    artifact_sha256 TEXT NOT NULL,
    covered_through TEXT NOT NULL,
    backfill_state TEXT NOT NULL,
    unattributed_lines INTEGER NOT NULL,
    model TEXT NOT NULL,
    min_target_lines INTEGER NOT NULL,
    source_path TEXT NOT NULL
)
"""


class GatherError(RuntimeError):
    """Refused ingest: bad artifact, colliding identities, or bad source."""


@dataclass(frozen=True)
class GatherResult:
    ingested: int
    skipped: int  # identical artifacts already in the union


@dataclass
class _Ingest:
    source: Path
    data: bytes
    header: artifact.ArtifactHeader
    key: str
    slug: str
    backfill_state: str
    unattributed_lines: int
    model: str
    min_target_lines: int
    rows: sqlite3.Connection  # materialized artifact rows (in-memory)


def gather(union_path: Path, sources: list[Path]) -> GatherResult:
    """Ingest artifacts (repo checkouts or artifact file paths) into the union
    DB at `union_path`. Refusals happen before anything is written."""
    if not sources:
        raise GatherError("no artifact sources given")
    ingests = [_load(source) for source in sources]
    ingests.sort(key=lambda i: (i.key,))  # deterministic ingest order

    existing = _existing_keys(union_path)
    _refuse_slug_collisions(ingests, existing)

    conn = db.connect(union_path)
    try:
        conn.execute(_UNION_ARTIFACTS_DDL)
        conn.commit()
        ingested = skipped = 0
        with conn:
            for item in ingests:
                current = conn.execute(
                    "SELECT artifact_sha256 FROM union_artifacts WHERE repo_key = ?",
                    (item.key,),
                ).fetchone()
                if current is not None and str(current["artifact_sha256"]) == item.header.digest:
                    skipped += 1  # identical artifact already unioned: no-op
                    continue
                _upsert(conn, item)
                ingested += 1
            _write_union_settings(conn)
        return GatherResult(ingested=ingested, skipped=skipped)
    finally:
        conn.close()


def _load(source: Path) -> _Ingest:
    path = source / STORE_REL if source.is_dir() else source
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GatherError(f"{source}: cannot read artifact ({exc})") from exc
    header = artifact.parse_header(data)
    if header is None:
        raise GatherError(f"{source}: not a skill-stats artifact (no valid header)")
    err = artifact.verify_bytes(data)
    if err is not None:
        raise GatherError(f"{source}: refusing artifact ({err})")

    rows = sqlite3.connect(":memory:")
    rows.row_factory = sqlite3.Row
    try:
        artifact.materialize(rows, data)
    except artifact.MaterializeError as exc:
        rows.close()
        raise GatherError(f"{source}: {exc}") from exc
    repo_row = rows.execute("SELECT * FROM repositories ORDER BY id LIMIT 1").fetchone()
    if repo_row is None:
        raise GatherError(f"{source}: artifact carries no repository row")
    key = str(repo_row["repo_key"])
    if key != header.repository_key:
        raise GatherError(
            f"{source}: inconsistent artifact (header says {header.repository_key!r},"
            f" rows say {key!r})"
        )
    return _Ingest(
        source=source,
        data=data,
        header=header,
        key=key,
        slug=str(repo_row["slug"]),
        backfill_state=_setting_text(rows, "backfill_state", "{}"),
        unattributed_lines=_as_int(_setting_json(rows, "unattributed_lines", 0)),
        model=str(_setting_json(rows, "model", "")),
        min_target_lines=_as_int(_setting_json(rows, "min_target_lines", 1)),
        rows=rows,
    )


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else int(str(value))


def _setting_text(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def _setting_json(conn: sqlite3.Connection, key: str, default: object) -> object:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(str(row["value"]))
    except (json.JSONDecodeError, TypeError):
        return default


def _existing_keys(union_path: Path) -> dict[str, str]:
    """repo_key -> slug already in the union (empty when the DB does not
    exist yet — read-only peek so refusals never create the file)."""
    if not union_path.exists():
        return {}
    try:
        conn = sqlite3.connect(union_path)
        conn.row_factory = sqlite3.Row
        try:
            return {
                str(r["repo_key"]): str(r["slug"])
                for r in conn.execute("SELECT repo_key, slug FROM repositories")
            }
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise GatherError(
            f"{union_path}: not a skill-stats database (does it have skill-stats tables?): {exc}"
        ) from exc


def _refuse_slug_collisions(ingests: list[_Ingest], existing: dict[str, str]) -> None:
    by_slug: dict[str, set[str]] = {}
    for key, slug in [(i.key, i.slug) for i in ingests] + list(existing.items()):
        by_slug.setdefault(slug, set()).add(key)
    for slug, keys in sorted(by_slug.items()):
        if len(keys) > 1:
            raise GatherError(
                f"slug {slug!r} is claimed by distinct repository keys"
                f" {sorted(keys)} — analyzing one repo on two Target branches is"
                " not supported; gather refuses instead of renaming ids"
            )


def _upsert(conn: sqlite3.Connection, item: _Ingest) -> None:
    """Replace (or add) one repository's rows in the union, remapping
    artifact-local commit ids into union-space ints (deterministic: assigned
    in ingest order). Feature ids are prefixed TEXT and copy verbatim."""
    old = conn.execute("SELECT id FROM repositories WHERE repo_key = ?", (item.key,)).fetchone()
    if old is not None:
        repo_id = int(old["id"])
    else:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 n FROM repositories").fetchone()
        repo_id = int(row["n"])
    _purge_repo(conn, repo_id)
    _insert_repo(conn, item, repo_id)

    src = item.rows
    next_id_row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 n FROM commits").fetchone()
    next_id = int(next_id_row["n"])
    commit_ids: dict[int, int] = {}
    for r in src.execute("SELECT * FROM commits ORDER BY id"):
        commit_ids[int(r["id"])] = next_id
        conn.execute(
            """INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at,
               author_name, title, message, subject_patch_id, walk_index, kind, diff,
               added_lines, deleted_lines, churn_lines)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                next_id,
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
                r["diff"],
                r["added_lines"],
                r["deleted_lines"],
                r["churn_lines"],
            ),
        )
        next_id += 1

    for r in src.execute("SELECT * FROM features ORDER BY rowid"):
        conn.execute(
            """INSERT INTO features (id, title, about, title_history_json, created_at,
               created_run_id, repo_id, live_lines) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r["id"],
                r["title"],
                r["about"],
                r["title_history_json"],
                r["created_at"],
                r["created_run_id"],
                repo_id,
                r["live_lines"],
            ),
        )
    for r in src.execute("SELECT * FROM commit_verdicts ORDER BY commit_id"):
        conn.execute(
            """INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,
               model, run_id, classified_at) VALUES (?, ?, ?, ?, ?, NULL, ?)""",
            (
                commit_ids[int(r["commit_id"])],
                r["verdict"],
                r["rationale"],
                r["raw_llm_output"],
                r["model"],
                r["classified_at"],
            ),
        )
    for r in src.execute("SELECT * FROM commits_features ORDER BY commit_id, feature_id"):
        conn.execute(
            "INSERT INTO commits_features (commit_id, feature_id, role) VALUES (?, ?, ?)",
            (commit_ids[int(r["commit_id"])], r["feature_id"], r["role"]),
        )
    for r in src.execute("SELECT * FROM fix_touches ORDER BY fix_commit_id, source_commit_id"):
        conn.execute(
            "INSERT INTO fix_touches (fix_commit_id, source_commit_id, hit_lines) VALUES (?, ?, ?)",
            (
                commit_ids[int(r["fix_commit_id"])],
                commit_ids[int(r["source_commit_id"])],
                r["hit_lines"],
            ),
        )
    for r in src.execute("SELECT * FROM fix_targets ORDER BY fix_commit_id, target_commit_id"):
        conn.execute(
            "INSERT INTO fix_targets (fix_commit_id, target_commit_id) VALUES (?, ?)",
            (commit_ids[int(r["fix_commit_id"])], commit_ids[int(r["target_commit_id"])]),
        )
    for r in src.execute(
        "SELECT * FROM fixes_features ORDER BY fix_commit_id, feature_id, via_fix_commit_id"
    ):
        via = r["via_fix_commit_id"]
        conn.execute(
            "INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id)"
            " VALUES (?, ?, ?)",
            (
                commit_ids[int(r["fix_commit_id"])],
                r["feature_id"],
                commit_ids[int(via)] if via is not None else None,
            ),
        )
    for r in src.execute("SELECT * FROM feature_line_samples ORDER BY feature_id, at_commit_sha"):
        conn.execute(
            "INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines)"
            " VALUES (?, ?, ?)",
            (r["feature_id"], r["at_commit_sha"], r["live_lines"]),
        )
    conn.execute(
        """INSERT OR REPLACE INTO union_artifacts (repo_key, artifact_sha256, covered_through,
           backfill_state, unattributed_lines, model, min_target_lines, source_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item.key,
            item.header.digest,
            item.header.covered_through,
            item.backfill_state,
            item.unattributed_lines,
            item.model,
            item.min_target_lines,
            str(item.source),
        ),
    )


def _insert_repo(conn: sqlite3.Connection, item: _Ingest, repo_id: int) -> None:
    row = item.rows.execute("SELECT * FROM repositories ORDER BY id LIMIT 1").fetchone()
    assert row is not None
    conn.execute(
        "INSERT OR REPLACE INTO repositories (id, repo_key, slug, branch, clone_url, added_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (repo_id, item.key, item.slug, row["branch"], row["clone_url"], row["added_at"]),
    )


def _purge_repo(conn: sqlite3.Connection, repo_id: int) -> None:
    """Drop one repository's union rows (children first: FK enforcement is on)."""
    where = "IN (SELECT id FROM commits WHERE repo_id = ?)"
    conn.execute(f"DELETE FROM commit_verdicts WHERE commit_id {where}", (repo_id,))
    conn.execute(f"DELETE FROM commits_features WHERE commit_id {where}", (repo_id,))
    conn.execute(
        f"DELETE FROM fix_touches WHERE fix_commit_id {where} OR source_commit_id {where}",
        (repo_id, repo_id),
    )
    conn.execute(
        f"DELETE FROM fix_targets WHERE fix_commit_id {where} OR target_commit_id {where}",
        (repo_id, repo_id),
    )
    conn.execute(
        f"DELETE FROM fixes_features WHERE fix_commit_id {where} OR via_fix_commit_id {where}"
        " OR feature_id IN (SELECT id FROM features WHERE repo_id = ?)",
        (repo_id, repo_id, repo_id),
    )
    conn.execute(
        "DELETE FROM feature_line_samples WHERE feature_id IN"
        " (SELECT id FROM features WHERE repo_id = ?)",
        (repo_id,),
    )
    conn.execute("DELETE FROM features WHERE repo_id = ?", (repo_id,))
    conn.execute("DELETE FROM commits WHERE repo_id = ?", (repo_id,))


def _write_union_settings(conn: sqlite3.Connection) -> None:
    """Union display/derived settings from the constituent metadata. No
    backfill_state and no branch: the union's completeness is derived at
    generate time (metrics.sampling_complete) and branches are per-repo."""
    rows = conn.execute("SELECT * FROM union_artifacts ORDER BY repo_key").fetchall()
    models = sorted({str(r["model"]) for r in rows if str(r["model"])})
    unattributed = sum(int(r["unattributed_lines"]) for r in rows)
    min_lines = min((int(r["min_target_lines"]) for r in rows), default=1)
    for key, value in (
        ("model", ", ".join(models)),
        ("min_target_lines", min_lines),
        ("unattributed_lines", unattributed),
    ):
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
