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

from . import identity

_PROJECTED_MARK = "_org_projected"


def org(conn: sqlite3.Connection) -> sqlite3.Connection:
    """The union-wide dedup projection for reads; pass-through for a
    single-repository DB. Idempotent per connection: a projected DB carries
    the marker table, so metric code can call org() freely without
    re-projecting (the projection is a fresh in-memory DB every time
    otherwise)."""
    n = conn.execute("SELECT COUNT(*) c FROM repositories").fetchone()["c"]
    if n <= 1:
        return conn
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_PROJECTED_MARK,)
    ).fetchone():
        return conn
    return _project(conn)


_base = identity.key_base


def _project(conn: sqlite3.Connection) -> sqlite3.Connection:
    from . import artifact  # lazy: artifact→htmlreport→metrics→orgview cycle

    out = sqlite3.connect(":memory:")
    out.row_factory = sqlite3.Row
    for table in artifact._TABLES:
        out.execute(table.ddl.strip())

    _copy_repositories(conn, out)
    commit_map = _copy_commits(conn, out)  # dedup shas per repo base
    _copy_verdicts(conn, out, commit_map)
    _copy_attribution(conn, out, commit_map)
    _copy_watermarks(conn, out)
    _copy_features(conn, out, commit_map)
    _copy_links(conn, out, commit_map)
    _repoint_dropped_features(conn, out, commit_map)
    _purge_orphaned_features(out)
    for r in conn.execute("SELECT key, value FROM settings ORDER BY key"):
        out.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (r["key"], r["value"]))
    # gather's provenance/completeness table travels too: the least-complete
    # sampling rule reads it on the projection exactly as on the union
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'union_artifacts'"
    ).fetchone():
        out.execute(
            """CREATE TABLE union_artifacts (
                   repo_key TEXT PRIMARY KEY,
                   artifact_sha256 TEXT NOT NULL,
                   covered_through TEXT NOT NULL,
                   backfill_state TEXT NOT NULL,
                   unattributed_lines INTEGER NOT NULL,
                   model TEXT NOT NULL,
                   min_target_lines INTEGER NOT NULL,
                   source_path TEXT NOT NULL
               )"""
        )
        for r in conn.execute("SELECT * FROM union_artifacts ORDER BY repo_key"):
            out.execute(
                "INSERT INTO union_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(r),
            )
    out.execute(f"CREATE TABLE {_PROJECTED_MARK} (ok)")
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
            tuple(
                winner[c]
                for c in (
                    "id",
                    "repo_id",
                    "sha",
                    "parent_sha",
                    "tree_sha",
                    "committed_at",
                    "author_name",
                    "title",
                    "message",
                    "subject_patch_id",
                    "walk_index",
                    "kind",
                    "diff",
                    "added_lines",
                    "deleted_lines",
                    "churn_lines",
                )
            ),
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


def _copy_watermarks(conn: sqlite3.Connection, out: sqlite3.Connection) -> None:
    """The recorded watermarks ride the projection per repository too
    (ticket #36): run state recorded like the evidence rows — the resume
    marker's merge date and PR number together — copied verbatim, never
    derived here."""
    for r in conn.execute("SELECT * FROM pr_watermark ORDER BY repo_id"):
        out.execute(
            "INSERT INTO pr_watermark (repo_id, last_pr_number, last_merged_at)"
            " VALUES (?, ?, ?)",
            (r["repo_id"], r["last_pr_number"], r["last_merged_at"]),
        )


_VERDICT_STRENGTH = {"yes": 0, "possible": 1, "no-evidence": 2}


def _copy_attribution(
    conn: sqlite3.Connection, out: sqlite3.Connection, commit_map: dict[int, int]
) -> None:
    """The attribution rows of every copy of a Shared commit are UNIONED onto
    its projected row (ticket #38) — one commit keeps ONE attribution,
    resolved later by channel precedence (prsweep's effective_* semantics):

    * git evidence rows from any copy all land on the winner (identical in
      practice — pure functions of the commit — and deduped on (rule, raw),
      winner copy first);
    * the copies' git-summary claims (ai_attribution) merge into one row:
      the strongest verdict wins, and harness/model fill winner-first then
      copies in ingest order (git claims never lose to a silent copy);
    * PR-channel rows follow in _copy_pr_evidence, all copies merged too.
    """
    copies: dict[int, list[int]] = {}
    for cid, wid in commit_map.items():
        copies.setdefault(wid, []).append(cid)
    for wid, cids in copies.items():
        cids.sort(key=lambda c: (0 if c == wid else 1, c))  # winner first, then ingest order
        _merge_git_attribution(conn, out, cids)
    _copy_pr_evidence(conn, out, commit_map)


def _merge_git_attribution(
    conn: sqlite3.Connection, out: sqlite3.Connection, cids: list[int]
) -> None:
    """One ai_attribution row + the unioned ai_evidence rows for the copies
    of one Shared commit (ids in winner-first order; all rows land on the
    first id)."""
    winner = cids[0]
    for cid in cids:
        for r in conn.execute(
            "SELECT rule, raw, verdict FROM ai_evidence WHERE commit_id = ? ORDER BY rule, raw",
            (cid,),
        ):
            out.execute(
                "INSERT OR IGNORE INTO ai_evidence (commit_id, rule, raw, verdict)"
                " VALUES (?, ?, ?, ?)",
                (winner, r["rule"], r["raw"], r["verdict"]),
            )
    summaries = [
        r
        for cid in cids
        for r in conn.execute(
            "SELECT verdict, harness, model FROM ai_attribution WHERE commit_id = ?",
            (cid,),
        )
    ]
    if not summaries:
        return
    verdict = min(summaries, key=lambda r: _VERDICT_STRENGTH[str(r["verdict"])])["verdict"]
    harness = next((r["harness"] for r in summaries if r["harness"] is not None), None)
    model = next((r["model"] for r in summaries if r["model"] is not None), None)
    out.execute(
        "INSERT OR REPLACE INTO ai_attribution (commit_id, verdict, harness, model)"
        " VALUES (?, ?, ?, ?)",
        (winner, verdict, harness, model),
    )


def _copy_pr_evidence(
    conn: sqlite3.Connection, out: sqlite3.Connection, commit_map: dict[int, int]
) -> None:
    """Channel-pr rows ride the projection too (ticket #35): commit-attached
    rows of EVERY copy of a Shared commit re-point onto its projected row —
    recorded state is not per-copy-pure and dropping a copy's PR rows would
    drop real observations — deduped per (commit, pr_number, rule, raw) so
    the same PR's row recorded by several branches' sweeps counts once.
    Suspicious rows carry no commit and collapse per
    (repo_id, pr_number, rule, raw). Channel precedence at consumption is
    the gather/report layer's rule (prsweep.effective_*) — the provenance
    column is what it acts on."""
    seen_null: set[tuple[int, int, str, str]] = set()
    seen_mapped: set[tuple[int, int, str, str]] = set()
    for r in conn.execute(
        "SELECT * FROM pr_evidence ORDER BY repo_id, pr_number, commit_id, rule, raw"
    ):
        cid = r["commit_id"]
        key_prefix: tuple[int, int, str, str]
        if cid is None:
            mapped = None
            key_prefix = (int(r["repo_id"]), int(r["pr_number"]), str(r["rule"]), str(r["raw"]))
            if key_prefix in seen_null:
                continue
            seen_null.add(key_prefix)
        else:
            mapped = commit_map[int(cid)]
            key_prefix = (mapped, int(r["pr_number"]), str(r["rule"]), str(r["raw"]))
            if key_prefix in seen_mapped:
                continue
            seen_mapped.add(key_prefix)
        out.execute(
            "INSERT INTO pr_evidence (repo_id, pr_number, commit_id, rule, raw, verdict,"
            " harness, model, channel, observed_at, suspicious)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["repo_id"],
                r["pr_number"],
                mapped,
                r["rule"],
                r["raw"],
                r["verdict"],
                r["harness"],
                r["model"],
                r["channel"],
                r["observed_at"],
                r["suspicious"],
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
            "INSERT OR IGNORE INTO commits_features (commit_id, feature_id, role) VALUES (?, ?, ?)",
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
    # Sample rows have no repo dimension either: a seeded id's (id, sha) row
    # is ONE union row. Keys may disagree on the value; rows read in
    # (feature_id, at_commit_sha, live_lines) order make the lowest value
    # the deterministic winner of the IGNORE.
    for r in conn.execute(
        """SELECT feature_id, at_commit_sha, live_lines FROM feature_line_samples
           ORDER BY feature_id, at_commit_sha, live_lines"""
    ):
        out.execute(
            "INSERT OR IGNORE INTO feature_line_samples (feature_id, at_commit_sha, live_lines)"
            " VALUES (?, ?, ?)",
            (str(r["feature_id"]), str(r["at_commit_sha"]), int(r["live_lines"])),
        )


def _repoint_dropped_features(
    conn: sqlite3.Connection,
    out: sqlite3.Connection,
    commit_map: dict[int, int],
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
        "DELETE FROM feature_line_samples WHERE feature_id NOT IN (SELECT id FROM features)"
    )
    out.execute("DELETE FROM fixes_features WHERE feature_id NOT IN (SELECT id FROM features)")
    out.execute("DELETE FROM commits_features WHERE feature_id NOT IN (SELECT id FROM features)")
    out.execute(
        """DELETE FROM features WHERE NOT EXISTS
           (SELECT 1 FROM commits_features cf
            WHERE cf.feature_id = features.id AND cf.role = 'defines')"""
    )
    out.execute("DELETE FROM fixes_features WHERE feature_id NOT IN (SELECT id FROM features)")
    out.execute("DELETE FROM commits_features WHERE feature_id NOT IN (SELECT id FROM features)")
