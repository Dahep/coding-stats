"""The org-level projection (ADR-0006 clause 4): dedup across one repo's
Data branches.

Per-key rows stay intact in the union DB (each branch keeps its own walk,
live lines and views); every read that aggregates ACROSS keys — the union
report — reads through `org(conn)`, an in-memory projection of the same
curated shape:

- A **Shared commit** (a sha held by several Repository keys of one repo) is
  one commit row: the key whose verdict has the earliest ``classified_at``
  wins, ties broken by the lexicographic Repository key strings. Keys
  without a verdict for the sha lose to any classified one; a sha classified
  nowhere collapses to the lowest Repository key (deterministic).
- The loser's rows for the deduped sha are dropped; its *dependents* —
  fix_touches / fix_targets / fixes_features rows pointing at the loser's
  row — are re-pointed to the winner's projected row (they target the same
  sha).
- A feature the losing verdict defined (and only it) has no defining commit
  left: its dependents (fixes_features rows) are re-pointed to "whatever the
  winner attributes that sha to" (the winner's defines ∪ fix attribution),
  or dropped when that set is empty; the feature itself is then purged.
- A feature id duplicated across the repo's keys (seeded ones) survives as
  ONE row: the owner is the key that won its defining sha (least Repository
  key as the deterministic fallback).

Single-repo DBs pass through unchanged (org rules never fire).
"""

import sqlite3


def org(conn: sqlite3.Connection) -> sqlite3.Connection:
    """The union-wide dedup projection for reads; pass-through for a
    single-repository DB."""
    n = conn.execute("SELECT COUNT(*) c FROM repositories").fetchone()["c"]
    if n <= 1:
        return conn
    return _project(conn)


def _base(key: str) -> str:
    """The URL part of a Repository key (the branch is after the last @;
    slugs can never contain @)."""
    return key.rsplit("@", 1)[0]


def _project(conn: sqlite3.Connection) -> sqlite3.Connection:
    from . import artifact  # lazy: artifact→htmlreport→metrics→orgview cycle

    out = sqlite3.connect(":memory:")
    out.row_factory = sqlite3.Row
    for table in artifact._TABLES:
        out.execute(table.ddl.strip())

    _copy_repositories(conn, out)
    commit_map = _copy_commits(conn, out)  # dedup shas per repo base
    _copy_verdicts(conn, out, commit_map)
    owner_of = _copy_features(conn, out, commit_map)
    _copy_links(conn, out, commit_map)
    _repoint_dropped_features(conn, out, commit_map, owner_of)
    _purge_orphaned_features(out)
    for r in conn.execute("SELECT key, value FROM settings ORDER BY key"):
        out.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (r["key"], r["value"]))
    out.commit()
    return out


def _copy_repositories(conn: sqlite3.Connection, out: sqlite3.Connection) -> None:
    for r in conn.execute("SELECT * FROM repositories ORDER BY id"):
        out.execute(
            "INSERT INTO repositories (id, repo_key, slug, branch, clone_url, added_at)"
            " VALUES (?, ?, ?, ?, ?, '')",
            (r["id"], r["repo_key"], r["slug"], r["branch"], r["clone_url"]),
        )


def _copy_commits(conn: sqlite3.Connection, out: sqlite3.Connection) -> dict[int, int]:
    """Dedup Shared commits per (repo base, sha); returns loser-id -> winner-id
    re-pointing map (self-mapped when the winner)."""
    bases = {
        int(r["id"]): _base(str(r["repo_key"]))
        for r in out.execute("SELECT id, repo_key FROM repositories")
    }
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT * FROM commits ORDER BY id"):
        groups.setdefault((bases[int(r["repo_id"])], str(r["sha"])), []).append(r)
    repoint: dict[int, int] = {}
    for _key, rows in sorted(groups.items()):
        winner = _winner_of(conn, rows)
        out.execute(
            "INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at,"
            " author_name, title, message, subject_patch_id, walk_index, kind, diff,"
            " added_lines, deleted_lines, churn_lines)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(winner[c] for c in (
                "id", "repo_id", "sha", "parent_sha", "tree_sha", "committed_at",
                "author_name", "title", "message", "subject_patch_id", "walk_index",
                "kind", "diff", "added_lines", "deleted_lines", "churn_lines",
            )),
        )
        for r in rows:
            repoint[int(r["id"])] = int(winner["id"])
    return repoint


def _winner_of(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> sqlite3.Row:
    """Earliest classified_among the group's verdicts; ties by Repository
    key; a key with no verdict loses to any classified key; an entirely
    unclassified group falls to the lowest Repository key."""
    keys = {
        int(r["id"]): str(k["repo_key"])
        for r in rows
        for k in conn.execute("SELECT repo_key FROM repositories WHERE id = ?", (r["repo_id"],))
    }
    best = None
    for r in rows:
        v = conn.execute(
            "SELECT classified_at FROM commit_verdicts WHERE commit_id = ?", (r["id"],)
        ).fetchone()
        if v is None:
            continue
        cand = (str(v["classified_at"]), keys[int(r["id"])])
        if best is None or cand < best[0]:
            best = (cand, r)
    if best is not None:
        return best[1]
    return min(rows, key=lambda r: keys[int(r["id"])])


def _copy_verdicts(
    conn: sqlite3.Connection, out: sqlite3.Connection, commit_map: dict[int, int]
) -> None:
    """Winner verdicts only: a projected commit keeps exactly one.``"""
    for r in conn.execute("SELECT * FROM commit_verdicts ORDER BY commit_id"):
        cid = int(r["commit_id"])
        if commit_map[cid] != cid:  # loser row: its verdict was superseded
            continue
        out.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
            " model, run_id, classified_at) VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (
                cid,
                r["verdict"],
                r["rationale"],
                r["raw_llm_output"],
                r["model"],
                r["classified_at"],
            ),
        )


def _copy_features(
    conn: sqlite3.Connection, out: sqlite3.Connection, commit_map: dict[int, int]
) -> dict[str, int]:
    """One feature row per feature id (duplicated, seeded ids collapse over
    a repo's keys); returns fid -> the owner's projected repo_id (the key
    that won its defining sha's dedup; least Repository key fallback)."""
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT * FROM features ORDER BY repo_id, id"):
        groups.setdefault(str(r["id"]), []).append(r)
    owner: dict[str, int] = {}
    for fid, rows in sorted(groups.items()):
        defining = conn.execute(
            """SELECT cf.commit_id FROM commits_features cf
               WHERE cf.feature_id = ? AND cf.role = 'defines'
               ORDER BY cf.commit_id LIMIT 1""",
            (fid,),
        ).fetchone()
        if defining is not None:
            projected = commit_map.get(int(defining["commit_id"]), int(defining["commit_id"]))
            winner_row = out.execute(
                "SELECT repo_id FROM commits WHERE id = ?", (projected,)
            ).fetchone()
            fid_owner = int(winner_row["repo_id"]) if winner_row else int(rows[0]["repo_id"])
        else:
            fid_owner = int(rows[0]["repo_id"])  # ORDER BY repo_id: least id wins
        picked = next((r for r in rows if int(r["repo_id"]) == fid_owner), rows[0])
        owner[fid] = fid_owner
        out.execute(
            "INSERT INTO features (id, title, about, title_history_json, created_at,"
            " created_run_id, repo_id, live_lines) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                fid,
                picked["title"],
                picked["about"],
                picked["title_history_json"],
                picked["created_at"],
                fid_owner,
                int(picked["live_lines"]),
            ),
        )
    return owner


def _copy_links(
    conn: sqlite3.Connection, out: sqlite3.Connection, commit_map: dict[int, int]
) -> None:
    """commits_features: winner rows verbatim (loser rows dropped — the
    winner's links represent the Shared commit). fix-side rows re-point at
    every commit reference onto its projected row."""
    for r in conn.execute("SELECT * FROM commits_features ORDER BY commit_id, feature_id"):
        cid = int(r["commit_id"])
        if commit_map[cid] != cid:
            continue  # loser row: the winner's links apply
        out.execute(
            "INSERT OR IGNORE INTO commits_features (commit_id, feature_id, role)"
            " VALUES (?, ?, ?)",
            (cid, str(r["feature_id"]), r["role"]),
        )
    for r in conn.execute("SELECT * FROM fix_touches ORDER BY fix_commit_id, source_commit_id"):
        out.execute(
            "INSERT OR IGNORE INTO fix_touches (fix_commit_id, source_commit_id, hit_lines)"
            " VALUES (?, ?, ?)",
            (
                commit_map[int(r["fix_commit_id"])],
                commit_map[int(r["source_commit_id"])],
                int(r["hit_lines"]),
            ),
        )
    for r in conn.execute("SELECT * FROM fix_targets ORDER BY fix_commit_id, target_commit_id"):
        out.execute(
            "INSERT OR IGNORE INTO fix_targets (fix_commit_id, target_commit_id) VALUES (?, ?)",
            (commit_map[int(r["fix_commit_id"])], commit_map[int(r["target_commit_id"])]),
        )
    for r in conn.execute(
        "SELECT * FROM fixes_features ORDER BY fix_commit_id, feature_id, via_fix_commit_id"
    ):
        via = r["via_fix_commit_id"]
        out.execute(
            "INSERT OR IGNORE INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id)"
            " VALUES (?, ?, ?)",
            (
                commit_map[int(r["fix_commit_id"])],
                str(r["feature_id"]),
                commit_map[int(via)] if via is not None else None,
            ),
        )


def _repoint_dropped_features(
    conn: sqlite3.Connection,
    out: sqlite3.Connection,
    commit_map: dict[int, int],
    owner_of: dict[str, int],
) -> None:
    """fixes_features rows naming a feature whose ONLY defining commit was a
    losing dedup row re-point to each fid the winner attributes the deduped
    sha to — or the row drops when the winner attributes it to nothing."""
    orphans = [
        str(r["id"])
        for r in out.execute(
            """SELECT id FROM features f WHERE NOT EXISTS
               (SELECT 1 FROM commits_features cf
                WHERE cf.feature_id = f.id AND cf.role = 'defines')"""
        )
    ]
    if not orphans:
        return
    winners_for: dict[str, list[str]] = {}
    for fid in orphans:
        defining = conn.execute(
            """SELECT cf.commit_id FROM commits_features cf
               WHERE cf.feature_id = ? AND cf.role = 'defines'
               ORDER BY cf.commit_id LIMIT 1""",
            (fid,),
        ).fetchone()
        if defining is None:
            continue
        winner_id = commit_map.get(int(defining["commit_id"]), int(defining["commit_id"]))
        attributed = out.execute(
            """SELECT feature_id FROM commits_features WHERE commit_id = ? AND role = 'defines'
               UNION SELECT feature_id FROM fixes_features WHERE fix_commit_id = ?""",
            (winner_id, winner_id),
        ).fetchall()
        winners_for[fid] = sorted({str(r["feature_id"]) for r in attributed})
    with out:
        for fid, replacements in winners_for.items():
            if not replacements:
                out.execute("DELETE FROM fixes_features WHERE feature_id = ?", (fid,))
                continue
            rows_p = out.execute(
                "SELECT fix_commit_id, feature_id, via_fix_commit_id FROM fixes_features"
                " WHERE feature_id = ?",
                (fid,),
            ).fetchall()
            for r in rows_p:
                out.execute(
                    "DELETE FROM fixes_features WHERE fix_commit_id = ? AND feature_id = ?",
                    (r["fix_commit_id"], fid),
                )
                for rep in replacements:
                    out.execute(
                        "INSERT OR IGNORE INTO fixes_features (fix_commit_id, feature_id,"
                        " via_fix_commit_id) VALUES (?, ?, ?)",
                        (r["fix_commit_id"], rep, r["via_fix_commit_id"]),
                    )


def _purge_orphaned_features(out: sqlite3.Connection) -> None:
    """Features without any defining commit left never enter stats (the same
    purge rule the walk uses). Dependent rows first, then the feature rows."""
    out.execute(
        "DELETE FROM feature_line_samples"
        " WHERE feature_id NOT IN (SELECT id FROM features)"
    )
    out.execute(
        "DELETE FROM fixes_features WHERE feature_id NOT IN (SELECT id FROM features)"
    )
    out.execute(
        "DELETE FROM commits_features WHERE feature_id NOT IN (SELECT id FROM features)"
    )
    out.execute(
        """DELETE FROM features WHERE NOT EXISTS
           (SELECT 1 FROM commits_features cf
            WHERE cf.feature_id = features.id AND cf.role = 'defines')"""
    )
    out.execute(
        "DELETE FROM fixes_features WHERE feature_id NOT IN (SELECT id FROM features)"
    )
    out.execute("DELETE FROM commits_features WHERE feature_id NOT IN (SELECT id FROM features)")
