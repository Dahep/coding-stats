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

The copies' INSERT statements (column lists, conflict knobs, per-direction
sentinels) come from the tablecatalog's orgview copy plans; the org RULES —
winner selection, re-pointing, orphan purging — stay hand-written here.
"""

import sqlite3

from . import identity, tablecatalog
from .aiattr import strongest

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
    out = sqlite3.connect(":memory:")
    out.row_factory = sqlite3.Row
    for table in tablecatalog.artifact_tables():
        out.execute(tablecatalog.artifact_ddl(table))

    _copy_repositories(conn, out)
    commit_map = _copy_commits(conn, out)  # dedup shas per repo base
    _copy_verdicts(conn, out, commit_map)
    _copy_attribution(conn, out, commit_map)
    _copy_watermarks(conn, out)
    _copy_disclosure(conn, out)
    _copy_features(conn, out, commit_map)
    _copy_links(conn, out, commit_map)
    _repoint_dropped_features(conn, out, commit_map)
    _purge_orphaned_features(out)
    _copy_settings(conn, out)
    # gather's provenance/completeness table travels too: the least-complete
    # sampling rule reads it on the projection exactly as on the union
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'union_artifacts'"
    ).fetchone():
        out.execute(tablecatalog.fk_ddl(tablecatalog.table("union_artifacts")))
        plan = tablecatalog.copy_plan("union_artifacts", "orgview")
        for r in conn.execute(tablecatalog.plan_select_sql(plan)):
            out.execute(tablecatalog.plan_insert_sql(plan), tablecatalog.plan_values(plan, r))
    out.execute(f"CREATE TABLE {_PROJECTED_MARK} (ok)")
    out.commit()
    return out


def _copy_repositories(conn: sqlite3.Connection, out: sqlite3.Connection) -> None:
    """added_at writes the '' wall-clock sentinel (the orgview knob): a
    projection never carries the wall-clock value."""
    plan = tablecatalog.copy_plan("repositories", "orgview")
    for r in conn.execute(tablecatalog.plan_select_sql(plan)):
        out.execute(tablecatalog.plan_insert_sql(plan), tablecatalog.plan_values(plan, r))


def _copy_commits(conn: sqlite3.Connection, out: sqlite3.Connection) -> dict[int, int]:
    """Dedup Shared commits per (repo base, sha); returns loser-id -> winner-id
    re-pointing map (self-mapped when the winner)."""
    plan = tablecatalog.copy_plan("commits", "orgview")
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
        out.execute(tablecatalog.plan_insert_sql(plan), tablecatalog.plan_values(plan, winner))
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
    plan = tablecatalog.copy_plan("commit_verdicts", "orgview")
    for r in conn.execute(tablecatalog.plan_select_sql(plan)):
        cid = int(r["commit_id"])
        if commit_map[cid] != cid:  # loser row: its verdict was superseded
            continue
        out.execute(tablecatalog.plan_insert_sql(plan), tablecatalog.plan_values(plan, r))


def _copy_watermarks(conn: sqlite3.Connection, out: sqlite3.Connection) -> None:
    """The recorded watermarks ride the projection per repository too
    (ticket #36): run state recorded like the evidence rows — the resume
    marker's merge date and PR number together — copied verbatim, never
    derived here."""
    plan = tablecatalog.copy_plan("pr_watermark", "orgview")
    for r in conn.execute(tablecatalog.plan_select_sql(plan)):
        out.execute(tablecatalog.plan_insert_sql(plan), tablecatalog.plan_values(plan, r))


def _copy_disclosure(conn: sqlite3.Connection, out: sqlite3.Connection) -> None:
    """The PR-tier disclosure marker rides the projection per repository too
    (ADR-0007 clause 5): recorded state like the watermark — copied
    verbatim, never derived here — one row per repo whose PR tier has run."""
    plan = tablecatalog.copy_plan("pr_disclosure", "orgview")
    for r in conn.execute(tablecatalog.plan_select_sql(plan)):
        out.execute(tablecatalog.plan_insert_sql(plan), tablecatalog.plan_values(plan, r))


def _copy_attribution(
    conn: sqlite3.Connection, out: sqlite3.Connection, commit_map: dict[int, int]
) -> None:
    """The attribution rows of every copy of a Shared commit are UNIONED onto
    its projected row (ticket #38) — one commit keeps ONE attribution,
    resolved later by channel precedence (aiattr's effective_* semantics):

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
    evidence = tablecatalog.copy_plan("ai_evidence", "orgview")
    summary = tablecatalog.copy_plan("ai_attribution", "orgview")
    winner = cids[0]
    for cid in cids:
        for r in conn.execute(
            "SELECT rule, raw, verdict FROM ai_evidence WHERE commit_id = ? ORDER BY rule, raw",
            (cid,),
        ):
            out.execute(
                tablecatalog.plan_insert_sql(evidence),
                tablecatalog.plan_values(evidence, r, overrides={"commit_id": winner}),
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
    verdict = strongest(str(r["verdict"]) for r in summaries)  # aiattr's ladder, one source
    harness = next((r["harness"] for r in summaries if r["harness"] is not None), None)
    model = next((r["model"] for r in summaries if r["model"] is not None), None)
    out.execute(
        tablecatalog.plan_insert_sql(summary),
        tablecatalog.plan_values(
            summary,
            summaries[0],
            overrides={"commit_id": winner, "verdict": verdict, "harness": harness, "model": model},
        ),
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
    the gather/report layer's rule (aiattr.effective_*) — the provenance
    column is what it acts on."""
    plan = tablecatalog.copy_plan("pr_evidence", "orgview")
    seen_null: set[tuple[int, int, str, str]] = set()
    seen_mapped: set[tuple[int, int, str, str]] = set()
    for r in conn.execute(tablecatalog.plan_select_sql(plan)):
        cid = r["commit_id"]
        key_prefix: tuple[int, int, str, str]
        if cid is None:
            key_prefix = (int(r["repo_id"]), int(r["pr_number"]), str(r["rule"]), str(r["raw"]))
            if key_prefix in seen_null:
                continue
            seen_null.add(key_prefix)
        else:
            key_prefix = (
                commit_map[int(cid)],
                int(r["pr_number"]),
                str(r["rule"]),
                str(r["raw"]),
            )
            if key_prefix in seen_mapped:
                continue
            seen_mapped.add(key_prefix)
        out.execute(
            tablecatalog.plan_insert_sql(plan),
            tablecatalog.plan_values(plan, r, commit_ids=commit_map),
        )


def _copy_features(
    conn: sqlite3.Connection, out: sqlite3.Connection, commit_map: dict[int, int]
) -> dict[str, int]:
    """One feature row per feature id (duplicated, seeded ids collapse over
    a repo's keys); returns fid -> the owner's projected repo_id (the key
    that won its defining sha's dedup; least Repository key fallback).
    created_run_id writes NULL (the orgview knob: a projected feature keeps
    no run provenance)."""
    plan = tablecatalog.copy_plan("features", "orgview")
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute(tablecatalog.plan_select_sql(plan)):
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
            tablecatalog.plan_insert_sql(plan),
            tablecatalog.plan_values(plan, picked, overrides={"repo_id": fid_owner}),
        )
    return owner


def _copy_links(
    conn: sqlite3.Connection, out: sqlite3.Connection, commit_map: dict[int, int]
) -> None:
    """commits_features: winner rows verbatim (loser rows dropped — the
    winner's links represent the Shared commit). fix-side rows re-point at
    every commit reference onto its projected row."""
    plan = tablecatalog.copy_plan("commits_features", "orgview")
    for r in conn.execute(tablecatalog.plan_select_sql(plan)):
        cid = int(r["commit_id"])
        if commit_map[cid] != cid:
            continue  # loser row: the winner's links apply
        out.execute(tablecatalog.plan_insert_sql(plan), tablecatalog.plan_values(plan, r))
    for name in ("fix_touches", "fix_targets"):
        plan = tablecatalog.copy_plan(name, "orgview")
        for r in conn.execute(tablecatalog.plan_select_sql(plan)):
            out.execute(
                tablecatalog.plan_insert_sql(plan),
                tablecatalog.plan_values(plan, r, commit_ids=commit_map),
            )
    plan = tablecatalog.copy_plan("fixes_features", "orgview")
    for r in conn.execute(tablecatalog.plan_select_sql(plan)):
        out.execute(
            tablecatalog.plan_insert_sql(plan),
            tablecatalog.plan_values(plan, r, commit_ids=commit_map),
        )
    # Sample rows have no repo dimension either: a seeded id's (id, sha) row
    # is ONE union row. Keys may disagree on the value; the plan's read order
    # (lowest live_lines first) makes the OR IGNORE's winner deterministic.
    plan = tablecatalog.copy_plan("feature_line_samples", "orgview")
    for r in conn.execute(tablecatalog.plan_select_sql(plan)):
        out.execute(tablecatalog.plan_insert_sql(plan), tablecatalog.plan_values(plan, r))


def _copy_settings(conn: sqlite3.Connection, out: sqlite3.Connection) -> None:
    plan = tablecatalog.copy_plan("settings", "orgview")
    for r in conn.execute(tablecatalog.plan_select_sql(plan)):
        out.execute(tablecatalog.plan_insert_sql(plan), tablecatalog.plan_values(plan, r))


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
