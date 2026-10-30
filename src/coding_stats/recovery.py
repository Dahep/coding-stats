"""Partial recompute on Artifact digest failure (ADR-0003 clause 3).

On a digest MISMATCH the tampered artifact content is never trusted. The
DATA branch's git history (ADR-0006 clause 1: the artifact lives on
``coding-stats/<target>``) is searched for the newest committed version
whose embedded digest self-verifies; that version ANCHORS the rebuild: its
rows are materialized into a fresh working DB (discarding whatever the
tampered state carried beyond it — correct, it cannot be trusted), and the
update flow re-walks and re-classifies only commits newer than its coverage
horizon. When no committed version verifies (history rewritten, or no prior
version exists at all) the fallback is a FULL recompute from scratch —
distinct from restore-recover and logged as such.

A tampered current file (uncommitted hand-edit) and a tampered latest commit
resolve through the same path: both search for the newest self-verifying
PAST version.

The same rebuild primitive serves a third case: a fresh/throwaway working DB
(CI runs are stateless) seeding itself from a VERIFIED artifact, because the
committed artifact is the store (see cli._run_update).
"""

import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import artifact, datastore, db, gitwalk, identity

STORE_PATH = datastore.STORE_TREE

# machine-local + user config survive a rebuild; derived measurement state
# never does (it is re-derived, or travels with the artifact's own rows)
_DERIVED_SETTINGS = frozenset({"unattributed_lines", "backfill_state"})


@dataclass(frozen=True)
class Recovery:
    """What kind of rebuild ran and what it is trusted through."""

    mode: str  # "restored" | "full-recompute"
    anchor_sha: str | None  # committed artifact version the rebuild anchors at


def find_anchor(repo_dir: Path, ref: str | None) -> tuple[str, bytes] | None:
    """(commit sha, bytes) of the newest committed artifact version on `ref`
    whose embedded digest self-verifies; None when no committed version does.
    The caller pins the ref (datastore.anchor_ref picks the Target's own Data
    branch): versions reachable only from another branch's refs must never
    anchor a restore — their rows would make the walk look rewritten."""
    if not ref:
        return None
    try:
        log = subprocess.run(
            ["git", "-C", str(repo_dir), "log", ref, "--format=%H", "--", STORE_PATH],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except subprocess.CalledProcessError:
        return None  # unknown ref: no anchor -> full recompute, the safe side
    for sha in log:  # newest first: the tampered latest version is skipped by verify
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "show", f"{sha}:{STORE_PATH}"],
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0 and artifact.verify_bytes(proc.stdout) is None:
            return sha, proc.stdout
    return None


def recover(
    conn: sqlite3.Connection,
    db_path: Path,
    repo_dir: Path,
    branch: str | None,
    anchor_ref: str | None = None,
) -> tuple[Recovery, sqlite3.Connection]:
    """Rebuild the working DB after a digest MISMATCH. Returns the recovery
    verdict and a fresh connection to the rebuilt DB (the old one is closed).

    The anchor search walks the ref the artifact lives in: the DATA branch's
    history by default (ADR-0006 clause 1), scoped per Target branch so
    restoring never reaches across branches; `branch` itself stays the
    identity branch of the re-registered repository. The legacy-layout
    migration passes the Target branch's ref instead (its store is committed
    there)."""
    if anchor_ref is None and branch:
        anchor_ref = datastore.anchor_ref(repo_dir, datastore.data_branch(branch))
    anchor_found = find_anchor(repo_dir, anchor_ref)
    if anchor_found is not None:
        sha, data = anchor_found
        return Recovery("restored", sha), rebuild(db_path, data, conn, repo_dir, branch)
    return Recovery("full-recompute", None), rebuild(db_path, None, conn, repo_dir, branch)


def rebuild(
    db_path: Path,
    data: bytes | None,
    old: sqlite3.Connection,
    repo_dir: Path,
    branch: str | None,
) -> sqlite3.Connection:
    """Replace the working DB atomically with a fresh one: materialize `data`
    (artifact bytes) when given — restore/seed — or leave it empty for a full
    recompute. Pending commits regain their real diffs from git (the artifact
    stores ''), non-derived settings are carried over, and the repository is
    (re-)registered so identity rules stay enforced."""
    tmp_path = db_path.parent / (db_path.name + ".rebuild-tmp")
    _cleanup(tmp_path)
    raw = sqlite3.connect(tmp_path)
    raw.row_factory = sqlite3.Row
    try:
        if data is not None:
            artifact.restore_into(raw, data)  # replay + stamp, the ONE seed entry
    finally:
        raw.close()
    fresh = db.connect(tmp_path)  # no-op for materialized steps; migrates old artifacts
    try:
        _carry_settings(old, fresh)
        identity.register_repository(fresh, repo_dir, branch)
        if data is not None:
            rehydrate_diffs(fresh, repo_dir)
            # safe-side upgrade: artifacts minted before the scheme carries
            # branch ids ({repo-slug}-{n}) re-prefix in place, suffixes kept
            row = fresh.execute("SELECT slug FROM repositories ORDER BY id LIMIT 1").fetchone()
            identity.reprefix_features(fresh, str(row["slug"]))
        fresh.commit()
    finally:
        fresh.close()
    old.close()
    try:
        os.replace(tmp_path, db_path)  # atomic: the original stays until the swap
    except OSError:
        _cleanup(tmp_path)  # failed swap: the original DB is untouched
        raise
    _cleanup_sidecars(db_path)  # stale sidecars of the replaced DB, only now
    return db.connect(db_path)


def rehydrate_diffs(conn: sqlite3.Connection, repo_dir: Path) -> int:
    """Refill commits.diff for PENDING commits (no verdict row) from git,
    through the walk's own diff+strip path (store paths out, merge diffs vs
    first parent). The artifact format stores '' for the machine-derivable
    payload; without this, restored backlog would be classified against noise
    (merges would even pass the empty-merge shortcut). Classified commits
    keep '' — their diffs are already consumed. Public: the migration seed
    path (ADR-0005) re-uses it after materializing a legacy artifact."""
    rows = conn.execute(
        """SELECT c.id, c.sha FROM commits c LEFT JOIN commit_verdicts v
           ON v.commit_id = c.id WHERE v.commit_id IS NULL"""
    ).fetchall()
    refilled = 0
    for r in rows:
        try:
            stat = gitwalk.prepare_diff(gitwalk.diff_for(repo_dir, str(r["sha"])))
        except subprocess.CalledProcessError:
            continue  # sha not in git: the walk's resume check reports it loudly
        conn.execute("UPDATE commits SET diff = ? WHERE id = ?", (stat.diff, r["id"]))
        refilled += 1
    return refilled


def _carry_settings(old: sqlite3.Connection, new: sqlite3.Connection) -> None:
    for r in old.execute("SELECT key, value FROM settings ORDER BY key"):
        if r["key"] in _DERIVED_SETTINGS:
            continue
        new.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (r["key"], r["value"])
        )


def _cleanup(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


def _cleanup_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
