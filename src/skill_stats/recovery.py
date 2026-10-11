"""Partial recompute on Artifact digest failure (ADR-0003 clause 3).

On a digest MISMATCH the tampered artifact content is never trusted. The
artifact file's own git history is searched for the newest committed version
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

from . import artifact, db, identity

STORE_PATH = ".skill-stats/skill-stats.sql"

# machine-local + user config survive a rebuild; derived measurement state
# never does (it is re-derived, or travels with the artifact's own rows)
_DERIVED_SETTINGS = frozenset({"unattributed_lines", "backfill_state"})


@dataclass(frozen=True)
class Recovery:
    """What kind of rebuild ran and what it is trusted through."""

    mode: str  # "restored" | "full-recompute"
    anchor_sha: str | None  # committed artifact version the rebuild anchors at


def find_anchor(repo_dir: Path) -> tuple[str, bytes] | None:
    """(commit sha, bytes) of the newest committed artifact version whose
    embedded digest self-verifies; None when no committed version does."""
    log = subprocess.run(
        ["git", "-C", str(repo_dir), "log", "--format=%H", "--", STORE_PATH],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
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
    conn: sqlite3.Connection, db_path: Path, repo_dir: Path, branch: str | None
) -> tuple[Recovery, sqlite3.Connection]:
    """Rebuild the working DB after a digest MISMATCH. Returns the recovery
    verdict and a fresh connection to the rebuilt DB (the old one is closed)."""
    anchor = find_anchor(repo_dir)
    if anchor is not None:
        sha, data = anchor
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
    recompute. Non-derived settings are carried over, and the repository is
    (re-)registered so identity rules stay enforced."""
    tmp_path = db_path.parent / (db_path.name + ".rebuild-tmp")
    _cleanup(tmp_path)
    raw = sqlite3.connect(tmp_path)
    raw.row_factory = sqlite3.Row
    try:
        if data is not None:
            artifact.materialize(raw, data)
    finally:
        raw.close()
    fresh = db.connect(tmp_path)  # no-op for materialized steps; migrates old artifacts
    try:
        _carry_settings(old, fresh)
        identity.register_repository(fresh, repo_dir, branch)
        fresh.commit()
    finally:
        fresh.close()
    old.close()
    _cleanup(db_path)
    os.replace(tmp_path, db_path)
    return db.connect(db_path)


def _carry_settings(old: sqlite3.Connection, new: sqlite3.Connection) -> None:
    for r in old.execute("SELECT key, value FROM settings ORDER BY key"):
        if r["key"] in _DERIVED_SETTINGS:
            continue
        new.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (r["key"], r["value"])
        )


def _cleanup(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
