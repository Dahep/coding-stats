"""Database handle: open/create and apply schema migrations."""

import sqlite3
from collections.abc import Callable
from pathlib import Path

from .schema import RUNS_DDL, SCHEMA_STEPS

# The files that belong to one SQLite database: '' is the DB file itself,
# the rest are the sidecars a connection can leave behind. ONE list — the
# union of the near-duplicate lists recovery and migration used to keep
# separately (they had diverged on ''); removing an absent name is a no-op,
# so the superset never hurts. The '' entry matters exactly where a WHOLE
# file must go (a tmp name before its first use — a stale -wal would replay
# old frames onto the fresh DB — and a failed swap's leftover); atomic file
# replacement (replace_file) deliberately never applies '' to the
# destination: there it names the fresh DB itself.
DB_SUFFIXES: tuple[str, ...] = ("", "-wal", "-shm", "-journal")


def apply_steps(conn: sqlite3.Connection) -> None:
    """Bring an open DB to the current working shape: every SCHEMA_STEP whose
    ``_schema_step_NNNN`` bookkeeping table is missing (the bookkeeping
    decides which steps already ran), then the machine-local tables a
    restored shape needs. ``connect`` runs this on a fresh open;
    ``artifact.seed_working_db`` runs it on the replayed connection, where
    the seed's stamp gates which steps still run. Idempotent."""
    for i, step in enumerate(SCHEMA_STEPS):
        tablename = f"_schema_step_{i:04d}"
        applied = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (tablename,)
        ).fetchone()
        if applied:
            continue
        if callable(step):
            _apply_migration(conn, step, tablename)
        else:
            conn.executescript(step)
            conn.execute(f"CREATE TABLE {tablename} (ok)")
            conn.commit()
    _ensure_machine_local(conn)


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_steps(conn)
    return conn


def swap_tmp(db_path: Path) -> Path:
    """The tmp sibling an atomic replacement stages through: the ONE
    tmp-name convention for every ``replace_file`` caller (the old bespoke
    ``.rebuild-tmp``/``.migrate-tmp`` spellings are gone). Distinct
    destinations never collide — the name derives from each destination."""
    return db_path.parent / (db_path.name + ".swap-tmp")


def remove_db_files(path: Path) -> None:
    """Remove a DB file and its sidecars (``DB_SUFFIXES``, '' included):
    a tmp name before its first use, or a failed swap's leftover."""
    for suffix in DB_SUFFIXES:
        Path(str(path) + suffix).unlink(missing_ok=True)


def replace_file(tmp_path: Path, db_path: Path) -> None:
    """Atomically replace ``db_path`` with ``tmp_path`` — the original stays
    in place until the swap — then drop stale sidecars. Sidecar cleanup
    comes ONLY after the main file is in place: a stale ``-wal`` replayed
    onto the replaced file would resurrect the OLD database's frames (the
    bug recovery.rebuild and the migration swap each hit and were each
    fixed for). A failed swap removes the tmp files and leaves the original
    DB untouched. Sidecars of both names are cleaned (a tmp file is gone
    after the swap, but its sidecars may linger for the next tmp user)."""
    try:
        tmp_path.replace(db_path)
    except OSError:
        remove_db_files(tmp_path)
        raise
    for target in (db_path, tmp_path):
        for suffix in DB_SUFFIXES[1:]:
            Path(str(target) + suffix).unlink(missing_ok=True)


def _ensure_machine_local(conn: sqlite3.Connection) -> None:
    """Re-create the machine-local tables a restored DB shape needs. The
    artifact excludes ``runs`` entirely (ADR-0006), yet the working shape
    references it (``features.created_run_id REFERENCES runs(id)``): a seeded
    or migrated DB that carries the step markers but skipped step 0's DDL
    dies on its next ``features`` DML under FK enforcement — without this
    check even a NULL ``created_run_id`` fails to find the parent table. Idempotent."""
    has = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
    ).fetchone()
    if not has:
        conn.execute(RUNS_DDL)
        conn.commit()


def _apply_migration(
    conn: sqlite3.Connection, migrate: Callable[[sqlite3.Connection], None], tablename: str
) -> None:
    """Apply one migration step atomically.

    The explicit transaction covers ALL of the step's DDL and DML plus this
    bookkeeping marker (python's sqlite3 autocommits DDL outside a real
    transaction, so `with conn:` alone is not enough), so a mid-migration
    failure rolls back to the previous shape and leaves the DB reopenable —
    the step then re-runs cleanly. FK enforcement is toggled outside the
    transaction (the pragma is a no-op inside one) because steps may rebuild
    parent tables."""
    conn.commit()  # end any implicit transaction so BEGIN/pragma take effect
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        try:
            migrate(conn)
            conn.execute(f"CREATE TABLE {tablename} (ok)")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
