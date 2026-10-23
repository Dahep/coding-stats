"""Seeding (ADR-0006 clause 3): sibling Data branches share verdicts.

During an update for Target branch B, commits gathered in B's walk that a
sibling Data branch of the SAME repo already classified are seeded from that
artifact instead of sent to the LLM: the verdict row (with its original
``classified_at`` and model), the feature rows it defines, its
feature-touch links, and its direct fix attribution — feature ids never
collide across branches because copied ids keep their original
``{repo}@{branch}`` prefix (the branch that FIRST classified the feature).

Rules per ADR-0006 / ticket #22:

- only sibling artifacts whose digest verifies are sources; a damaged one is
  noted and skipped (seeding is an optimization, never a hard dependency —
  the branch classifies on its own when no source is available);
- per shared sha the winner is the verdict with the earliest
  ``classified_at``, ties broken by the lexicographic Repository key order —
  checking sources in (earliest-classified-at, key) order and taking the
  first cover of each sha is exactly that selection;
- detect, lineage and live-lines still run on B afterwards: B's fix targets
  and line counts come from its own blame, and B's walk order stays its own
  (seeding never re-derives, it only copies what classification decided).
"""

import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import artifact, datastore

_STORE_REL_IN_TREE = ".coding-stats/coding-stats.sql"


@dataclass(frozen=True)
class SeedResult:
    seeded: int
    sources: tuple[str, ...]  # sibling Data branches that contributed


@dataclass(frozen=True)
class _Source:
    """One verifying sibling artifact, materialized in memory."""

    data_branch: str
    repo_key: str
    order: tuple[str, str, str]  # (earliest classified_at, repository key, branch)
    rows: sqlite3.Connection
    by_sha: dict[str, int]  # sha -> the sibling's local commit id


def seed_from_siblings(
    conn: sqlite3.Connection, repo_dir: Path, our_data_branch: str, quiet: bool = False
) -> SeedResult:
    """Seed every unclassified walk commit whose sha a verifying sibling
    Data branch already classified. Returns the seeded count and sources."""
    our_base, our_rid = _our_identity(conn)
    refs = datastore.remote_data_refs(repo_dir)
    sources = _winning_sources(repo_dir, our_base, refs, our_data_branch, quiet)
    if not sources:
        return SeedResult(0, ())
    seeded = 0
    used: list[str] = []
    with conn:
        pending = conn.execute(
            """SELECT c.id, c.sha FROM commits c
               LEFT JOIN commit_verdicts v ON v.commit_id = c.id
               WHERE v.commit_id IS NULL ORDER BY c.walk_index"""
        ).fetchall()
        for c in pending:  # each commit is seeded once: earliest source wins
            sha = str(c["sha"])
            cover = next((s for s in sources if sha in s.by_sha), None)
            if cover is None:
                continue
            _copy_commit(conn, our_rid, cover, int(c["id"]), sha)
            if cover.data_branch not in used:
                used.append(cover.data_branch)
            seeded += 1
    return SeedResult(seeded, tuple(used))


def _our_identity(conn: sqlite3.Connection) -> tuple[str, int]:
    """(Repository key base — without the branch part — our repository id)."""
    row = conn.execute("SELECT id, repo_key FROM repositories ORDER BY id").fetchone()
    if row is None:
        return "", 0
    key = str(row["repo_key"])
    return key.rsplit("@", 1)[0], int(row["id"])


def _sibling_artifact(repo_dir: Path, tip_sha: str) -> bytes | None:
    """The sibling Data branch's artifact bytes read straight out of its
    committed tree (no checkout needed)."""
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "show", f"{tip_sha}:{_STORE_REL_IN_TREE}"],
        capture_output=True,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


sibling_artifact = _sibling_artifact


def _winning_sources(
    repo_dir: Path,
    our_base: str,
    refs: dict[str, str],
    our_data_branch: str,
    quiet: bool,
) -> list[_Source]:
    """Every verifying sibling artifact of the same repo, in win order."""
    sources: list[_Source] = []
    for name in sorted(refs):
        if name == our_data_branch:
            continue  # our own branch is not its own seeding source
        data = _sibling_artifact(repo_dir, refs[name])
        if data is None:
            continue  # no artifact (or unreachable tree): not a source
        header = artifact.parse_header(data)
        if header is None:
            continue  # not a coding-stats artifact at all
        if header.repository_key.rsplit("@", 1)[0] != our_base:
            continue  # another repo's Data branch: never a seeding source
        if artifact.verify_bytes(data) is not None:
            if not quiet:
                print(f"seeding source {name}: digest does not verify; skipped")
            continue  # a damaged sibling can't spread its data
        rows = sqlite3.connect(":memory:")
        rows.row_factory = sqlite3.Row
        try:
            artifact.materialize(rows, data)
        except artifact.MaterializeError as exc:
            rows.close()
            if not quiet:
                print(f"seeding source {name}: refused ({exc})")
            continue
        earliest = rows.execute("SELECT MIN(classified_at) t FROM commit_verdicts").fetchone()
        sources.append(
            _Source(
                data_branch=name,
                repo_key=header.repository_key,
                order=(
                    str(earliest["t"] or ""),
                    header.repository_key,
                    name,
                ),
                rows=rows,
                by_sha={
                    str(r["sha"]): int(r["id"]) for r in rows.execute("SELECT sha, id FROM commits")
                },
            )
        )
    sources.sort(key=lambda s: s.order)
    return sources


def _copy_commit(
    conn: sqlite3.Connection, our_rid: int, src: _Source, our_commit_id: int, sha: str
) -> None:
    """Copy one seeded commit's verdict, defined/linked feature rows, and
    direct fix attribution. via_fix ids are re-anchored to our own commit
    ids when the referenced sha exists here (else NULL: no dangling ints —
    the closure's deterministic re-derivation below extends what reproves)."""
    src_id = src.by_sha[sha]
    v = src.rows.execute("SELECT * FROM commit_verdicts WHERE commit_id = ?", (src_id,)).fetchone()
    if v is not None:
        conn.execute(
            "INSERT OR IGNORE INTO commit_verdicts"
            " (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (
                our_commit_id,
                str(v["verdict"]),
                str(v["rationale"]),
                str(v["raw_llm_output"]),
                str(v["model"]),
                str(v["classified_at"]),
            ),
        )
    for link in src.rows.execute(
        "SELECT feature_id, role FROM commits_features WHERE commit_id = ?", (src_id,)
    ):
        fid = str(link["feature_id"])
        _copy_feature(conn, src, fid, our_rid)
        conn.execute(
            "INSERT OR IGNORE INTO commits_features (commit_id, feature_id, role) VALUES (?, ?, ?)",
            (our_commit_id, fid, str(link["role"])),
        )
    for att in src.rows.execute(
        "SELECT feature_id, via_fix_commit_id FROM fixes_features WHERE fix_commit_id = ?",
        (src_id,),
    ):
        fid = str(att["feature_id"])
        _copy_feature(conn, src, fid, our_rid)
        our_via = None
        if att["via_fix_commit_id"] is not None:
            via = src.rows.execute(
                "SELECT sha FROM commits WHERE id = ?", (att["via_fix_commit_id"],)
            ).fetchone()
            if via is not None:
                o = conn.execute("SELECT id FROM commits WHERE sha = ?", (via["sha"],)).fetchone()
                our_via = int(o["id"]) if o else None
        conn.execute(
            "INSERT OR IGNORE INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id)"
            " VALUES (?, ?, ?)",
            (our_commit_id, fid, our_via),
        )


def _copy_feature(conn: sqlite3.Connection, src: _Source, fid: str, our_rid: int) -> None:
    """One feature row, with its original (branch-carrying) id and repo_id
    redirected to OUR repository row. Already-present ids (walk-defined or
    copied through another sha) are no-ops."""
    if conn.execute("SELECT 1 FROM features WHERE id = ?", (fid,)).fetchone():
        return
    f = src.rows.execute("SELECT * FROM features WHERE id = ?", (fid,)).fetchone()
    if f is None:
        return  # unknown-id attribution stays out rather than dangling
    conn.execute(
        "INSERT OR IGNORE INTO features (id, title, about, title_history_json,"
        " created_at, created_run_id, repo_id, live_lines)"
        " VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
        (
            fid,
            str(f["title"]),
            str(f["about"]),
            str(f["title_history_json"]),
            str(f["created_at"]),
            our_rid,
            int(f["live_lines"]),
        ),
    )
