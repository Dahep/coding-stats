"""The committed Artifact: a deterministic whole-snapshot SQL text (ADR-0003).

Format (version 1) — self-executing: ``sqlite3 db < coding-stats.sql`` rebuilds
a standalone queryable DB from any clone.

* Header comment lines, in this order: ``-- coding-stats artifact``,
  ``-- format: 1``, ``-- schema-step: <n>``, ``-- repository-key: <key>``,
  ``-- covered-through: <sha>`` (latest walked Target-branch commit included)
  and finally ``-- digest: <sha256>`` — the Artifact digest of CONTEXT.md:
  sha256 over the whole content with the digest line's own value blanked
  (the line becomes exactly ``-- digest:``). Unkeyed checksum, honest limits:
  detects accidental or manual edits, not adversarial forgery (ADR-0003).
* Then per table: its CREATE TABLE (curated shape — column types and
  constraints kept, cross-table REFERENCES dropped so the dump materializes
  standalone) followed by one canonical INSERT per row in stable order.
  The shapes, column tuples, canonical SELECTs and sentinels live in
  tablecatalog — the one declaration every consumer derives from.

Curated projection (ADR-0003 clause 2), the artifact is not a byte copy of
the working DB: table ``runs`` is excluded entirely; settings rows other than
``branch``/``model``/``min_target_lines``/``unattributed_lines`` are excluded
(esp. ``repo`` — a machine path — and ``classify_session_id``); wall-clock
columns are frozen to sentinels (``repositories.added_at`` -> ''); the
``commits.diff`` payload is machine-derivable and inserted as '' (kept in the
DDL so shapes match); run-provenance columns (``commit_verdicts.run_id``,
``features.created_run_id``) insert NULL. ``classified_at`` IS kept: verdicts
are classified once, so the value is stable audit data — a re-classification
changing it is an honest data change, not churn.

Determinism is the point (the digest must be stable across machines): every
table is ordered by its stored keys (autoincrement ids / UNIQUE keys), quoting
is fixed, and nothing wall-clock-dependent reaches the output.
"""

import hashlib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import db, identity, tablecatalog

FORMAT = 1

DIGEST_BLANK = "-- digest:"
_DIGEST_LINE_RE = re.compile(rb"^-- digest:(?: ([0-9a-f]+))?$", re.M)


def _lit(value: object) -> str:
    """Fixed SQL literal quoting (strings single-quoted, ints bare, NULL as NULL)."""
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


# printable ASCII minus the escape char: header values made only of these stay
# literal; every other byte is escaped as \xNN (the escape char itself too, so
# the representation is injective)
_HDR_SAFE = set(range(0x20, 0x7F)) - {ord("\\")}


def _hdr(value: object) -> str:
    """Guaranteed single-line ASCII-safe header representation. A value can
    never break out of its ``-- ...`` comment line: newlines, control bytes and
    non-ASCII become \\xNN escapes (SQL/dot-command injection via a crafted
    repository key is structurally impossible); values needing no escaping
    stay literal for readability."""
    out = []
    for b in str(value).encode("utf-8"):
        out.append(chr(b) if b in _HDR_SAFE else f"\\x{b:02X}")
    return "".join(out)


def _head(conn: sqlite3.Connection) -> str:
    key = conn.execute("SELECT repo_key FROM repositories ORDER BY id LIMIT 1").fetchone()
    step = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '_schema_step_%'"
        " ORDER BY name DESC LIMIT 1"
    ).fetchone()
    step_n = int(step["name"].rsplit("_", 1)[-1]) if step else 0
    return (
        "-- coding-stats artifact\n"
        f"-- format: {FORMAT}\n"
        f"-- schema-step: {_hdr(step_n)}\n"
        f"-- repository-key: {_hdr(key['repo_key'] if key else '')}\n"
        f"-- covered-through: {_hdr(covered_through(conn))}\n"
        f"{DIGEST_BLANK}\n"
    )


def serialize(conn: sqlite3.Connection) -> bytes:
    """The complete canonical artifact content, digest line filled in."""
    parts = [_head(conn)]
    for table in tablecatalog.artifact_tables():
        parts.append(tablecatalog.artifact_ddl(table) + "\n")
        cols = ", ".join(table.names)
        sentinels = {c.name: c.artifact_sentinel for c in table.columns if c.has_sentinel}
        for row in conn.execute(tablecatalog.canonical_select(table)):
            values = ", ".join(
                _lit(sentinels[c]) if c in sentinels else _lit(row[c]) for c in table.names
            )
            parts.append(f"INSERT INTO {table.name} ({cols}) VALUES ({values});\n")
    blank = "".join(parts)
    digest = hashlib.sha256(blank.encode("utf-8")).hexdigest()
    content = blank.replace(f"{DIGEST_BLANK}\n", f"{DIGEST_BLANK} {digest}\n", 1)
    return content.encode("utf-8")


def write(conn: sqlite3.Connection, store_root: Path) -> Path:
    """Land the artifact: ``<store_root>/.coding-stats/coding-stats.sql``
    (reads the WORKING db — settings.repo is a machine path and absent from
    the artifact). The store holds ONLY the SQL artifact (ADR-0006 clause
    5): reports render through the report verb/action, never here."""
    store = Path(store_root) / ".coding-stats"
    store.mkdir(parents=True, exist_ok=True)
    path = store / "coding-stats.sql"
    path.write_bytes(serialize(conn))
    return path


def covered_through(conn: sqlite3.Connection) -> str:
    """The Coverage horizon: sha of the latest walked Target-branch commit
    included in the snapshot ('' when the DB holds none)."""
    row = conn.execute("SELECT sha FROM commits ORDER BY walk_index DESC LIMIT 1").fetchone()
    return str(row["sha"]) if row else ""


@dataclass(frozen=True)
class ArtifactHeader:
    """The comment header of an artifact, values decoded (\\xNN inverse)."""

    repository_key: str
    covered_through: str
    digest: str
    schema_step: int


_HEADER_FIELD_RE = re.compile(rb"^-- (repository-key|covered-through|schema-step): ?(.*)$", re.M)


def unhdr(text: str) -> str:
    """Decode _hdr's \\xNN escaping back to the original value (gather's
    window into header values — repository keys can carry non-ASCII)."""
    out = bytearray()
    i = 0
    while i < len(text):
        if text.startswith("\\x", i) and i + 4 <= len(text):
            hexpart = text[i + 2 : i + 4]
            if len(hexpart) == 2 and all(c in "0123456789abcdefABCDEF" for c in hexpart):
                out.append(int(hexpart, 16))
                i += 4
                continue
        out.extend(text[i].encode("utf-8", "surrogateescape"))
        i += 1
    return out.decode("utf-8", "surrogateescape")


def parse_header(data: bytes, require_tag: bool = True) -> ArtifactHeader | None:
    """Parse (and decode) the artifact header; None when it is not a
    coding-stats artifact. With the default require_tag, ingestion demands
    the current marker line — legacy skill-stats artifacts (old marker,
    same body) are refused here. Migration reads them with
    require_tag=False (ADR-0005)."""
    if require_tag and not data.startswith(b"-- coding-stats artifact\n"):
        return None
    fields = {m.group(1).decode("ascii"): m.group(2) for m in _HEADER_FIELD_RE.finditer(data)}
    digest_m = _DIGEST_LINE_RE.search(data)
    if (
        not {"repository-key", "covered-through", "schema-step"} <= set(fields)
        or digest_m is None
        or not digest_m.group(1)
    ):
        return None
    return ArtifactHeader(
        repository_key=unhdr(fields["repository-key"].decode("utf-8", "surrogateescape")),
        covered_through=unhdr(fields["covered-through"].decode("utf-8", "surrogateescape")),
        digest=digest_m.group(1).decode("ascii"),
        schema_step=int(fields["schema-step"]),
    )


def _replay(conn: sqlite3.Connection, data: bytes) -> None:
    """Replay artifact bytes into conn under an SQLite authorizer: ONLY the
    curated shape is executable — CREATE TABLE of the known tables and INSERTs
    into them (plus plain reads/transactions). ATTACH/DETACH, PRAGMAs, DML
    beyond INSERT, and any other DDL are DENIED.

    This is load-bearing: the artifact digest is an unkeyed checksum, so any
    committer can recompute it — checksummed artifact bytes are DATA, never
    code. Schema bookkeeping is deliberately not part of the replay: see
    stamp_schema_steps. Raises MaterializeError on refusal or on a shape
    that does not match the curated columns.

    Private on purpose: a replay is never callable without its schema-step
    stamp (restore_into) or without the working-shape finish and digest
    verification (seed_working_db) — the stamp-less replay that lost #31 is
    unreachable."""
    conn.set_authorizer(_artifact_authorizer())
    try:
        conn.executescript(data.decode("utf-8"))
    except (sqlite3.Error, UnicodeDecodeError) as exc:
        raise MaterializeError(f"artifact refused: {exc}") from exc
    finally:
        conn.set_authorizer(None)
    _complete_shape(conn, _schema_step_of(data))
    _check_shape(conn)
    _check_ownership(conn)


# The tables minted AFTER the core settled (schema steps 4-8): shape
# completion only ever fills THESE, and only for artifacts whose header
# schema-step predates the table's own step. Core tables (repositories,
# commits, commit_verdicts, features, ...) are NEVER resurrected — an
# artifact missing one is refused by _check_shape. Declared in the
# catalogue (minted_at per table); core tables never appear there.
_COMPLETE_STEPS: dict[str, int] = tablecatalog.complete_steps()

# Columns minted after their table (schema step 7): a replay whose body
# created the table at its OLDER shape gets the column added (outside the
# guarded replay), so bodies minted before the column existed stay
# replayable — pr_watermark's resume-marker merge date is NULL there, a
# marker that claims no merge date. Completed only for artifacts whose
# header stamp predates the column's step: a newer stamp carrying the old
# shape is drift, and _check_shape refuses it. Declared in the catalogue
# (minted_at per column).
_COMPLETE_COLUMNS: dict[tuple[str, str], int] = tablecatalog.complete_columns()


def _schema_step_of(data: bytes) -> int:
    """The artifact header's schema-step stamp (0 when the body carries
    none)."""
    m = re.search(rb"^-- schema-step: (\d+)$", data, re.M)
    return int(m.group(1)) if m else 0


def _complete_shape(conn: sqlite3.Connection, step: int) -> None:
    """Bring a pre-feature artifact body up to the curated shape: of the
    post-step-3 tables (ai_attribution, ai_evidence, pr_evidence,
    pr_watermark, pr_disclosure), every one the replay did not create AND
    whose schema step is NEWER than the artifact's header stamp is created
    EMPTY by us, outside the guarded replay; likewise every column minted
    after its table is ADDED to an old-shape table of such a body (see
    _COMPLETE_COLUMNS).
    Artifacts minted before a table (or column) existed stay replayable —
    their rows for the new table are simply absent, and pure state (AI
    attribution) re-derives on the next walk. A mint that already claimed
    the table's step must carry it (no completion): missing core tables are
    never resurrected, and _check_shape refuses the artifact."""
    for table in tablecatalog.artifact_tables():
        at_step = _COMPLETE_STEPS.get(table.name)
        if at_step is None or step >= at_step:
            continue
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table.name,)
        ).fetchone()
        if not exists:
            conn.execute(tablecatalog.artifact_ddl(table))
    for (tname, column), at_step in _COMPLETE_COLUMNS.items():
        if step >= at_step:
            continue
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({tname})")}
        if cols and column not in cols:
            # a missing table is _check_shape's refusal, not ours to fill
            col = tablecatalog.table(tname).column(column)
            conn.execute(f"ALTER TABLE {tname} ADD COLUMN {column} {col.type}")


def stamp_schema_steps(conn: sqlite3.Connection, data: bytes) -> None:
    """Mark the artifact's schema step as applied (db.connect bookkeeping).
    Runs AFTER the guarded replay: bookkeeping tables are ours, not artifact
    data, and the authorizer denies creating them."""
    step = _schema_step_of(data)
    for i in range(step + 1):
        conn.execute(f"CREATE TABLE IF NOT EXISTS _schema_step_{i:04d} (ok)")
    conn.commit()


def restore_into(conn: sqlite3.Connection, data: bytes) -> None:
    """Seed an ALREADY-OPEN connection from artifact bytes in ONE call: the
    authorizer-guarded replay, then the schema-step stamp. The pairing is
    the point — a stamp-less replay leaves the _schema_step_* bookkeeping
    unwritten, and a later serialize of the seeded DB then derives
    schema-step 0 while the content is a newer layout, so every later
    restore re-runs the migrations and crashes (#31). Every caller outside
    this module goes through restore_into or seed_working_db; the replay
    alone is not reachable. Raises MaterializeError on a refused artifact."""
    _replay(conn, data)
    stamp_schema_steps(conn, data)


def seed_working_db(
    db_path: Path,
    data: bytes,
    *,
    register: bool,
    repo_dir: Path | None = None,
    branch: str | None = None,
) -> sqlite3.Connection:
    """The ONE path from artifact bytes to a working DB: verify the digest,
    replay the curated rows, stamp the schema steps, and apply the working
    shape — one call on one connection. The parameters are inseparable here
    on purpose: an unstamped replay leaves the bookkeeping unwritten and a
    later serialize derives schema-step 0 while the content is a newer
    layout, so every later restore re-runs the migrations and crashes (#31).

    The result is a fresh DB at `db_path` (which must not already exist —
    callers stage a db.swap_tmp name, or pass ':memory:' for a throwaway
    connection, in which case Path(':memory:') fspaths back to the literal
    sqlite name). Returns the open connection with db.connect's semantics
    (Row rows, FK enforcement, current shape). Digest verification runs
    here AND at the entry points that own the bytes (the anchor search,
    gather, seeding, migration all verify before seeding and keep their own
    refusal messages) — an unverifiable artifact never reaches a replay.

    `register` is keyword-required and decides who writes the repositories
    row: True runs identity.register_repository inside the seed (the
    recovery rebuild registers the repository against the seeded rows); False
    leaves the row entirely to the caller — the migration path holds the
    relabel authority (a rename IS the identity change the register rules
    must refuse) and writes the row itself. Never a hidden default."""
    err = verify_bytes(data)
    if err is not None:
        raise MaterializeError(f"refusing artifact: {err}")
    conn = sqlite3.connect(db_path)
    try:
        _replay(conn, data)
        stamp_schema_steps(conn, data)
        # the working shape (db.apply_steps) needs FK enforcement and Row
        # rows exactly like connect gives them
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        db.apply_steps(conn)
        if register:
            if repo_dir is None:
                raise ValueError("seed_working_db(register=True) needs the repo to register")
            identity.register_repository(conn, repo_dir, branch)
    except BaseException:
        conn.close()
        raise
    return conn


KNOWN_TABLES: dict[str, tuple[str, ...]] = {
    t.name: t.names for t in tablecatalog.artifact_tables()
}


class MaterializeError(ValueError):
    """Refused artifact replay: hostile statement or non-curated shape."""


def _artifact_authorizer() -> Callable[[int, str | None, str | None, str | None, str | None], int]:
    """The allowlist (small and complete): known-table CREATE/INSERT, plain
    reads of known tables, SELECT and transactions. Everything else —
    ATTACH/DETACH any database, PRAGMA, DELETE/UPDATE/DROP, triggers, views,
    temp objects, functions — denies."""

    def auth(
        action: int,
        arg1: str | None,
        arg2: str | None,
        dbname: str | None,
        source: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_CREATE_TABLE and arg1 in KNOWN_TABLES:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_INSERT and arg1 in KNOWN_TABLES:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and arg1 in KNOWN_TABLES:
            return sqlite3.SQLITE_OK
        if (
            action == sqlite3.SQLITE_CREATE_INDEX
            and arg2 in KNOWN_TABLES
            and (arg1 or "").startswith("sqlite_autoindex_")
        ):
            # implicit indexes of our CREATE TABLE PRIMARY KEY/UNIQUE
            # constraints; explicit CREATE INDEX stays denied
            return sqlite3.SQLITE_OK
        if action in (
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_UPDATE,
        ) and arg1 in ("sqlite_master", "sqlite_temp_master"):
            # CREATE TABLE's own schema bookkeeping; direct sqlite_master
            # writes are refused by SQLite itself (writable_schema is denied
            # here), so this cannot mint new objects behind the allowlist
            return sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return auth


def _check_shape(conn: sqlite3.Connection) -> None:
    """Post-replay drift guard: the replay must have created every curated
    table with exactly its curated columns (an attacker's CREATE TABLE name is
    checked by the authorizer; its column list, here). Positional reads: the
    guards run on whatever connection the caller replays into, with no
    row_factory requirement."""
    created = {
        str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for name, cols in KNOWN_TABLES.items():
        if name not in created:
            raise MaterializeError(f"artifact refused: missing table {name}")
        actual = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({name})")]
        if actual != list(cols):
            raise MaterializeError(
                f"artifact refused: table {name} has columns {actual}, expected {list(cols)}"
            )


def _check_ownership(conn: sqlite3.Connection) -> None:
    """Defense in depth: an artifact is ONE repository's rows. Exactly one
    repositories row, and every row in every table must belong to it — a
    crafted artifact must not smuggle rows that relabel onto another repo
    during ingest (the remap would silently adopt them). The per-table
    predicates derive from the catalogue's FK graph: one predicate per
    reference to an in-artifact table (repositories by value, commits and
    features by membership, nullable columns guarded)."""
    repos = conn.execute("SELECT id FROM repositories ORDER BY id").fetchall()
    if len(repos) != 1:
        raise MaterializeError(
            f"artifact refused: {len(repos)} repositories rows (expected exactly 1)"
        )
    rid = int(repos[0][0])
    for table, sql in tablecatalog.ownership_checks(rid):
        n = int(conn.execute(sql).fetchone()[0])
        if n:
            raise MaterializeError(
                f"artifact refused: {table} has {n} row(s) outside the artifact's repository"
            )


def digest_of(path: Path) -> str:
    """The stored Artifact digest's meaning: sha256 over the exact file bytes
    with the digest line's value blanked. Raises when the file carries no
    digest. Bytes-based: no decoding happens anywhere in the path."""
    parts = _digest_parts(Path(path).read_bytes())
    if parts is None:
        raise ValueError(f"{path}: no digest line")
    return hashlib.sha256(parts[0]).hexdigest()


def verify(path: Path) -> str | None:
    """None when the stored digest covers the file's exact bytes; else the
    error. Works on raw bytes — CRLF or invalid-UTF-8 tampering can never
    launder through text-mode normalization, and malformed content yields a
    failure verdict instead of an exception."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        return str(exc)
    return verify_bytes(data)


def verify_bytes(data: bytes) -> str | None:
    """Digest verdict for raw artifact bytes (no file I/O) — used to vet
    versions restored out of git history before trusting them."""
    parts = _digest_parts(data)
    if parts is None:
        return "no digest line"
    blanked, stored = parts
    recomputed = hashlib.sha256(blanked).hexdigest()
    if recomputed != stored.decode("ascii"):
        return f"digest mismatch: stored {stored.decode('ascii')}, recomputed {recomputed}"
    return None


def _digest_parts(data: bytes) -> tuple[bytes, bytes] | None:
    """(content with the digest value blanked, stored digest bytes) from raw
    bytes; None when the digest line is missing or malformed."""
    m = _DIGEST_LINE_RE.search(data)  # first occurrence: the header, always first
    if m is None or not m.group(1):
        return None
    blanked = data[: m.start()] + DIGEST_BLANK.encode("ascii") + data[m.end() :]
    return blanked, m.group(1)
