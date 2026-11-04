"""Derive (ADR-0008): THE single deterministic stage rebuilding the
blame-derived state from scratch every run — fix attribution, Fix lineage,
and the tip's live-lines accounting.

One stage, one entry point: `run_derive` is what the update orchestrator
calls once per run (after classify, before anything consumes it). What used
to be three sequential stages with three separate transactions
(targets.py's annotate, lineage.py's closure, livelines.py's attribution)
is folded here; blame.py stays the internal line-evidence leaf (its
porcelain parsing is implemented exactly once and imported).

The derived-only tables (Q11a) are REBUILT FROM SCRATCH each production
run: `fix_touches`, `fix_targets` and `fixes_features` are fully cleared and
recomputed inside this stage. Nothing carries over across runs, so the
stage is idempotent: re-running it at a RAISED `min_target_lines` drops the
targets that now fall below the threshold (the append-only
prev-targets guard that silently retained stale targets is gone). Artifact
carry-over at rest is unchanged — the Artifact still serializes whatever
content these tables hold after the stage.

Debug trims (`--no-detect` / `--no-lineage`) skip the attribution sections
while the live-lines pass keeps running — the old pipeline's behaviour
(live-lines was never skippable), kept honest by staying debug-only:
production update always rebuilds from scratch.

Per-section writing keeps the old transaction sizes (empty-then-rebuild
per table; the backfill sweep writes its state when its history is
trustworthy). A stage that fails midway leaves partially rebuilt derived
tables behind, serializes nothing, and the next run rebuilds from scratch.
"""

import json
import sqlite3
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import verdicts
from .blame import _porcelain_shas, blame_deleted_lines
from .gitwalk import _unquote_git_path, is_store_path

# Backfill plan caps (grill-r3-Q9 leaves the cap to implementation):
# a full sweep up to this many walked commits, evenly sampled above it.
BACKFILL_FULL_MAX_COMMITS = 400
BACKFILL_SAMPLE_POINTS = 160


@dataclass(frozen=True)
class DeriveResult:
    """What one derive stage computed and recorded."""

    fixes_annotated: int  # fix-verdict commits attributed this run (0 on a debug trim)
    closure_rows: int  # fixes_features rows counted in the rebuilt closure
    at_sha: str  # the measured tip (latest walked commit)
    unattributed_lines: int  # tip lines whose commit is not in the DB
    samples_written: int  # feature_line_samples rows written (incl. tip)
    backfilled_points: int  # sample points processed by the backfill sweep


def run_derive(
    conn: sqlite3.Connection,
    repo_dir: Path,
    min_target_lines: int = 1,
    *,
    attribution: bool = True,
) -> DeriveResult:
    """The derive stage: fix attribution -> lineage closure -> live lines,
    in that order (live lines accrue through the closure). One orchestrator
    call, one deterministic result. `attribution=False` is the debug trim:
    the live-lines pass runs as usual, the attribution tables are left
    untouched."""
    fixes_annotated = 0
    closure_rows = 0
    if attribution:
        fixes_annotated = annotate_all(repo_dir, conn, min_target_lines)
        closure_rows = close_lineage(conn)
    live = update_live_lines(conn, repo_dir)
    return DeriveResult(
        fixes_annotated=fixes_annotated,
        closure_rows=closure_rows,
        at_sha=live.at_sha,
        unattributed_lines=live.unattributed_lines,
        samples_written=live.samples_written,
        backfilled_points=live.backfilled_points,
    )


# ------------------------------------------------- fix attribution (targets)


def annotate_fix(repo: Path, conn: sqlite3.Connection, sha: str, min_target_lines: int = 1) -> None:
    """Record fix_touches and fix_targets for one fix verdict commit.
    Called from the from-scratch rebuild (the tables are already clear);
    its own DELETE keeps a direct call idempotent too."""
    hits_per_source: dict[str, int] = {}
    excluded = {sha}
    row = conn.execute("SELECT id, parent_sha FROM commits WHERE sha = ?", (sha,)).fetchone()
    if not row or not row["parent_sha"]:
        return
    per_file = blame_deleted_lines(repo, sha)
    for _file, shas in per_file.items():
        for source in shas:
            if source in excluded or source.startswith("0000"):
                continue
            hits_per_source[source] = hits_per_source.get(source, 0) + 1

    commit_id = row["id"]
    touched: list[tuple[str, int]] = sorted(hits_per_source.items(), key=lambda kv: -kv[1])
    with conn:
        conn.execute("DELETE FROM fix_touches WHERE fix_commit_id = ?", (commit_id,))
        conn.execute("DELETE FROM fix_targets WHERE fix_commit_id = ?", (commit_id,))
        for source_sha, lines in touched:
            src = conn.execute("SELECT id FROM commits WHERE sha = ?", (source_sha,)).fetchone()
            if not src:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO fix_touches (fix_commit_id, source_commit_id, hit_lines)"
                " VALUES (?, ?, ?)",
                (commit_id, src["id"], lines),
            )
            if lines >= min_target_lines:
                conn.execute(
                    "INSERT OR IGNORE INTO fix_targets (fix_commit_id, target_commit_id)"
                    " VALUES (?, ?)",
                    (commit_id, src["id"]),
                )


def annotate_all(repo: Path, conn: sqlite3.Connection, min_target_lines: int = 1) -> int:
    """Rebuild fix_touches/fix_targets from scratch for every fix-verdict
    commit. Derived-only: both tables are cleared first, so a re-run at a
    raised min_target_lines drops targets that fall below it."""
    shas = [
        r["sha"]
        for r in conn.execute(
            """SELECT c.sha FROM commits c JOIN commit_verdicts v ON v.commit_id = c.id
               WHERE v.verdict = ? ORDER BY c.walk_index""",
            (verdicts.FIX,),
        )
    ]
    with conn:
        conn.execute("DELETE FROM fix_touches")
        conn.execute("DELETE FROM fix_targets")
    for sha in shas:
        annotate_fix(repo, conn, sha, min_target_lines)
    return len(shas)


# ------------------------------------------------------- fix lineage closure


def close_lineage(conn: sqlite3.Connection) -> int:
    """Rebuild the transitive closure for all fix commits — from scratch:
    `fixes_features` is cleared first (derived-only). Chronological order
    guarantees a fix's targets have already been closed when it is
    processed; every remaining insert is fresh."""
    rows = conn.execute(
        """SELECT c.id FROM commits c
           JOIN fix_targets ft ON ft.fix_commit_id = c.id
           ORDER BY c.walk_index"""
    ).fetchall()
    added = 0
    with conn:  # clear + rebuild in one transaction: a partial closure never lands
        conn.execute("DELETE FROM fixes_features")
        for (fix_id,) in rows:
            added += close_one(conn, fix_id)
    return added


def close_one(conn: sqlite3.Connection, fix_id: int) -> int:
    added = 0
    seen: set[int] = set()
    queue: list[tuple[int, int | None]] = [
        (r["target_commit_id"], None)
        for r in conn.execute(
            "SELECT target_commit_id FROM fix_targets WHERE fix_commit_id = ?", (fix_id,)
        )
    ]
    while queue:
        unit, first_fix = queue.pop(0)
        if unit in seen or unit == fix_id:
            continue
        seen.add(unit)
        for r in conn.execute(
            "SELECT feature_id FROM commits_features WHERE commit_id = ? AND role = 'defines'",
            (unit,),
        ):
            cur = conn.execute(
                "INSERT OR IGNORE INTO fixes_features (fix_commit_id, feature_id,"
                " via_fix_commit_id) VALUES (?, ?, ?)",
                (fix_id, r["feature_id"], first_fix),
            )
            added += cur.rowcount
        verdict = conn.execute(
            "SELECT verdict FROM commit_verdicts WHERE commit_id = ?", (unit,)
        ).fetchone()
        if verdict and verdicts.is_fix(verdict["verdict"]):
            next_fix = first_fix if first_fix is not None else unit
            for r in conn.execute(
                "SELECT target_commit_id FROM fix_targets WHERE fix_commit_id = ?", (unit,)
            ):
                queue.append((r["target_commit_id"], next_fix))
    return added


# ------------------------------------------------------------- live lines
# (folds the former livelines stage: per-feature counts at the tip, the
# backfill sweep, the Sample writer. Sample rows keep their schema and
# append-only history — only the derivation process consolidated.)


@dataclass(frozen=True)
class LiveLineResult:
    """What the live-lines pass measured and recorded."""

    at_sha: str  # the measured tip (latest walked commit)
    unattributed_lines: int  # tip lines whose commit is not in the DB
    samples_written: int  # feature_line_samples rows written (incl. tip)
    backfilled_points: int  # sample points processed by the backfill sweep


def sample_points(walk_indexes: list[int]) -> list[int]:
    """Backfill plan over walked commits: the full sweep under the size cap,
    otherwise ~BACKFILL_SAMPLE_POINTS evenly spaced points including the first
    and last commit. Deterministic for a given walk."""
    idx = sorted(walk_indexes)
    if len(idx) <= BACKFILL_FULL_MAX_COMMITS:
        return idx
    n = BACKFILL_SAMPLE_POINTS
    step = (len(idx) - 1) / (n - 1)
    return sorted({idx[round(i * step)] for i in range(n)})


def update_live_lines(conn: sqlite3.Connection, repo_dir: Path) -> LiveLineResult:
    """Measure per-feature live lines at the tip, refresh features.live_lines,
    and write Sample rows. The backfill sweep runs until its history is
    trustworthy (backfill_state): first run, after an interrupted sweep, or
    after new classification landed (late-created features gain their
    historical samples)."""
    row = conn.execute("SELECT sha FROM commits ORDER BY walk_index DESC LIMIT 1").fetchone()
    if not row:
        return LiveLineResult("", 0, 0, 0)
    tip = str(row["sha"])
    plan = conn.execute("SELECT walk_index, sha FROM commits ORDER BY walk_index").fetchall()
    walk_of = {str(r["sha"]): int(r["walk_index"]) for r in plan}
    alive = _defining_walks(conn)

    samples_written = 0
    backfilled_points = 0
    have_samples = bool(conn.execute("SELECT COUNT(*) c FROM feature_line_samples").fetchone()["c"])
    if alive and _needs_backfill(conn, have_samples):
        by_index = {int(r["walk_index"]): str(r["sha"]) for r in plan}
        for idx in sample_points(sorted(by_index)):
            sha = by_index[idx]
            per_feature, _ = _attribute(conn, *_line_counts_at(repo_dir, sha))
            samples_written += _write_sample(conn, sha, alive, walk_of[sha], per_feature)
            backfilled_points += 1
        _record_backfill_state(conn)  # completeness tracked, never assumed

    per_feature, unattributed = _attribute(conn, *_line_counts_at(repo_dir, tip))
    with conn:
        conn.execute("UPDATE features SET live_lines = 0")
        for fid, n in per_feature.items():
            conn.execute("UPDATE features SET live_lines = ? WHERE id = ?", (n, fid))
        # surfaced by the report (and the artifact) — never silently dropped
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('unattributed_lines', ?)",
            (json.dumps(unattributed),),
        )
    samples_written += _write_sample(conn, tip, alive, walk_of[tip], per_feature)
    conn.commit()
    return LiveLineResult(tip, unattributed, samples_written, backfilled_points)


def _backfill_state(conn: sqlite3.Connection) -> dict[str, object] | None:
    row = conn.execute("SELECT value FROM settings WHERE key = 'backfill_state'").fetchone()
    if not row:
        return None
    try:
        state = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return None
    return state if isinstance(state, dict) else None


def _max_classified_walk(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """SELECT MAX(c.walk_index) w FROM commits c
           JOIN commit_verdicts v ON v.commit_id = c.id"""
    ).fetchone()
    return int(row["w"] or 0)


def _needs_backfill(conn: sqlite3.Connection, have_samples: bool) -> bool:
    """The sweep re-runs until its history is trustworthy: no samples yet, no
    recorded completed sweep, or new classification landed after the last one
    (features created late must gain their historical samples)."""
    if not have_samples:
        return True
    state = _backfill_state(conn)
    if state is None or state.get("status") != "done":
        return True
    recorded = state.get("max_classified_walk_index", 0)
    if not isinstance(recorded, int):
        return True  # unknown/partial state: re-sweep is the safe side
    return _max_classified_walk(conn) > recorded


def _record_backfill_state(conn: sqlite3.Connection) -> None:
    """End of a sweep: record completeness honestly. With a classification
    backlog outstanding the status stays pending (later runs re-sweep once the
    backlog clears); a fully classified walk marks the sweep done."""
    pending = int(
        conn.execute(
            "SELECT COUNT(*) c FROM commits c LEFT JOIN commit_verdicts v"
            " ON v.commit_id = c.id WHERE v.commit_id IS NULL"
        ).fetchone()["c"]
    )
    state = {
        "status": "done" if pending == 0 else "pending",
        "max_classified_walk_index": _max_classified_walk(conn),
    }
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('backfill_state', ?)",
            (json.dumps(state),),
        )


def _defining_walks(conn: sqlite3.Connection) -> dict[str, int]:
    """feature_id -> walk index at which the feature comes into existence
    (its earliest defining commit; falls back to its earliest link)."""
    out: dict[str, int] = {}
    for r in conn.execute(
        """SELECT cf.feature_id fid, MIN(c.walk_index) w
           FROM commits_features cf JOIN commits c ON c.id = cf.commit_id
           WHERE cf.role = 'defines' GROUP BY cf.feature_id"""
    ):
        out[str(r["fid"])] = int(r["w"])
    for r in conn.execute(
        """SELECT cf.feature_id fid, MIN(c.walk_index) w
           FROM commits_features cf JOIN commits c ON c.id = cf.commit_id
           GROUP BY cf.feature_id"""
    ):
        out.setdefault(str(r["fid"]), int(r["w"]))
    return out


def _write_sample(
    conn: sqlite3.Connection,
    at_sha: str,
    alive: dict[str, int],
    walk_index: int,
    per_feature: dict[str, int],
) -> int:
    """One Sample row per feature alive at this commit (idempotent per sha)."""
    written = 0
    with conn:
        for fid, defined_walk in alive.items():
            if defined_walk > walk_index:
                continue  # the feature does not exist yet at this sample
            conn.execute(
                "INSERT OR REPLACE INTO feature_line_samples"
                " (feature_id, at_commit_sha, live_lines) VALUES (?, ?, ?)",
                (fid, at_sha, per_feature.get(fid, 0)),
            )
            written += 1
    return written


def _line_counts_at(repo_dir: Path, sha: str) -> tuple[Counter[str], int]:
    """(introducing commit -> line count, unblamable lines) at a commit.
    --first-parent matches the walk's first-parent semantics: lines brought in
    by side-branch commits (absent from the DB) attribute to the merge commit
    the walk includes, instead of going unattributed.

    Binary accounting matches the walk's numstat semantics exactly:
    NUL-containing blobs are binary to git (numstat reports '-', churn is
    never counted) and are skipped here entirely — their bytes are content,
    not lines, and must not surface as fake unattributed lines. Weird-text
    blobs (high bytes, no NUL) are text to git: they count in walked churn,
    so blame attributes them normally (porcelain decoded with 'replace' —
    record metadata is ASCII; content bytes don't affect sha records)."""
    counts: Counter[str] = Counter()
    unattributed = 0
    entries = _files_at(repo_dir, sha)
    binaries = _binary_blobs(repo_dir, {blob for _, blob in entries})
    for path, blob in entries:
        if blob in binaries:
            continue  # binary to git: neither walked churn nor live lines
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "blame",
                    "-w",
                    "-C",
                    "--first-parent",
                    "--porcelain",
                    sha,
                    "--",
                    path,
                ],
                check=True,
                capture_output=True,
            )
            text = proc.stdout.decode("utf-8", "replace")
        except subprocess.CalledProcessError:
            unattributed += _blob_line_count(repo_dir, sha, path)  # blame failure
            continue
        for blamed in _porcelain_shas(text):
            counts[blamed] += 1
    return counts, unattributed


def _binary_blobs(repo_dir: Path, blobs: set[bytes]) -> set[bytes]:
    """Blob shas whose head 8000 bytes contain a NUL — git's own binary
    verdict (the same heuristic `numstat` uses to report '-'). One streaming
    cat-file batch pass; per-blob cost is bounded by the head-cap read even
    for multi-MB blobs."""
    if not blobs:
        return set()
    NUL_HEAD = 8000  # bytes; git's binary heuristic window
    found: set[bytes] = set()
    proc = subprocess.Popen(
        ["git", "-C", str(repo_dir), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert proc.stdin and proc.stdout
        for blob in blobs:
            proc.stdin.write(blob + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline()  # "<oid> (blob|missing) <size>"
            parts = header.split()
            if len(parts) < 3 or parts[1] == b"missing":
                continue
            size = int(parts[2])
            head = proc.stdout.read(min(size, NUL_HEAD))
            proc.stdout.read(size - len(head) + 1)  # trailing NL after content
            if b"\0" in head:
                found.add(blob)
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait()
    return found


def _files_at(repo_dir: Path, sha: str) -> list[tuple[str, bytes]]:
    """Tracked (path, blob-sha) pairs at a commit, skipping store paths and
    submodule gitlinks. Uses raw NUL-delimited ``ls-tree -z`` names: text
    output C-quotes special spellings, which would both break blame
    lookups and let quoted store paths slip past the exclusion check."""
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-tree", "-r", "-z", sha],
        capture_output=True,
        check=True,
    )
    pairs = []
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.partition(b"\t")
        mode_type_sha = meta.split()
        if mode_type_sha[0] == b"160000":
            continue  # gitlinks: blame never descends into submodule content
        path = _decode_name(name)
        if is_store_path(path):
            continue
        pairs.append((path, mode_type_sha[2]))
    return pairs


def _decode_name(raw: bytes) -> str:
    """Raw ls-tree name to str. ``-z`` output is never quoted, but a
    defensively quoted-looking spelling (with escapes) is decoded so exclusion
    and blame always see the real path."""
    if raw[:1] == b'"' and raw[-1:] == b'"' and b"\\" in raw[1:-1]:
        return _unquote_git_path(raw.decode("ascii", "surrogateescape"))
    return raw.decode("utf-8", "surrogateescape")


def _blob_line_count(repo_dir: Path, sha: str, path: str) -> int:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "show", f"{sha}:{path}"],
        capture_output=True,
        check=False,
    )
    return len(proc.stdout.splitlines()) if proc.returncode == 0 else 0


def _attribute(
    conn: sqlite3.Connection, counts: Counter[str], unblamable: int
) -> tuple[dict[str, int], int]:
    """Commit line counts -> per-feature live lines + unattributed total.
    Unattributed = lines whose introducing commit is not in the DB (plus lines
    blame could not attribute at all)."""
    features_by_commit: dict[str, set[str]] = {}
    for r in conn.execute(
        """SELECT c.sha, x.feature_id FROM commits c JOIN
               (SELECT commit_id, feature_id FROM commits_features
                UNION SELECT fix_commit_id, feature_id FROM fixes_features) x
               ON x.commit_id = c.id"""
    ):
        features_by_commit.setdefault(str(r["sha"]), set()).add(str(r["feature_id"]))
    known = {str(r["sha"]) for r in conn.execute("SELECT sha FROM commits")}
    per_feature: dict[str, int] = {}
    unattributed = unblamable
    for sha, n in counts.items():
        if sha not in known:
            unattributed += n
            continue
        for fid in features_by_commit.get(sha, ()):
            per_feature[fid] = per_feature.get(fid, 0) + n
    return per_feature, unattributed
