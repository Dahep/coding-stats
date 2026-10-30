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
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = 1

DIGEST_BLANK = "-- digest:"
_DIGEST_LINE_RE = re.compile(rb"^-- digest:(?: ([0-9a-f]+))?$", re.M)

_SETTINGS_WHITELIST = (
    "backfill_state",
    "branch",
    "min_target_lines",
    "model",
    "unattributed_lines",
)


@dataclass(frozen=True)
class _Table:
    name: str
    ddl: str
    columns: tuple[str, ...]
    source: str  # SELECT <columns> FROM ... ORDER BY <stable keys>
    sentinels: dict[str, object] = field(default_factory=dict)


_TABLES: tuple[_Table, ...] = (
    _Table(
        "repositories",
        """CREATE TABLE repositories (
               id INTEGER PRIMARY KEY,
               repo_key TEXT NOT NULL UNIQUE,
               slug TEXT NOT NULL,
               branch TEXT,
               clone_url TEXT,
               added_at TEXT
           );""",
        ("id", "repo_key", "slug", "branch", "clone_url", "added_at"),
        "SELECT id, repo_key, slug, branch, clone_url, added_at FROM repositories ORDER BY id",
        {"added_at": ""},  # wall-clock -> sentinel (impl-w1-hazard obligation)
    ),
    _Table(
        "commits",
        """CREATE TABLE commits (
               id INTEGER PRIMARY KEY,
               repo_id INTEGER NOT NULL,
               sha TEXT NOT NULL,
               parent_sha TEXT,
               tree_sha TEXT NOT NULL,
               committed_at TEXT NOT NULL,
               author_name TEXT NOT NULL,
               title TEXT NOT NULL,
               message TEXT NOT NULL,
               subject_patch_id TEXT,
               walk_index INTEGER NOT NULL,
               kind TEXT NOT NULL CHECK (kind IN ('normal', 'merge')),
               diff TEXT NOT NULL,
               added_lines INTEGER NOT NULL DEFAULT 0,
               deleted_lines INTEGER NOT NULL DEFAULT 0,
               churn_lines INTEGER NOT NULL DEFAULT 0,
               UNIQUE (repo_id, sha),
               UNIQUE (repo_id, walk_index)
           );""",
        (
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
        ),
        """SELECT id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title,
                  message, subject_patch_id, walk_index, kind, diff, added_lines,
                  deleted_lines, churn_lines
           FROM commits ORDER BY id""",
        {"diff": ""},  # raw diff text is machine-derivable, never committed
    ),
    _Table(
        "commit_verdicts",
        """CREATE TABLE commit_verdicts (
               commit_id INTEGER PRIMARY KEY,
               verdict TEXT NOT NULL CHECK (verdict IN ('feature', 'fix', 'refactor',
                                                        'revert', 'cleanup', 'merge',
                                                        'unknown')),
               rationale TEXT NOT NULL,
               raw_llm_output TEXT NOT NULL,
               model TEXT NOT NULL,
               run_id INTEGER,
               classified_at TEXT NOT NULL
           );""",
        ("commit_id", "verdict", "rationale", "raw_llm_output", "model", "run_id", "classified_at"),
        """SELECT commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at
           FROM commit_verdicts ORDER BY commit_id""",
        {"run_id": None},  # run provenance is machine-local
    ),
    _Table(
        "ai_attribution",
        """CREATE TABLE ai_attribution (
               commit_id INTEGER PRIMARY KEY,
               verdict TEXT NOT NULL CHECK (verdict IN ('yes', 'possible', 'no-evidence')),
               harness TEXT,
               model TEXT
           );""",
        ("commit_id", "verdict", "harness", "model"),
        "SELECT commit_id, verdict, harness, model FROM ai_attribution ORDER BY commit_id",
    ),
    _Table(
        "ai_evidence",
        """CREATE TABLE ai_evidence (
               commit_id INTEGER NOT NULL,
               rule TEXT NOT NULL,
               raw TEXT NOT NULL,
               verdict TEXT NOT NULL CHECK (verdict IN ('yes', 'possible')),
               PRIMARY KEY (commit_id, rule, raw)
           );""",
        ("commit_id", "rule", "raw", "verdict"),
        "SELECT commit_id, rule, raw, verdict FROM ai_evidence"
        " ORDER BY commit_id, rule, raw",
    ),
    _Table(
        "pr_evidence",
        """CREATE TABLE pr_evidence (
               repo_id INTEGER NOT NULL,
               pr_number INTEGER NOT NULL,
               commit_id INTEGER,
               rule TEXT NOT NULL,
               raw TEXT NOT NULL,
               verdict TEXT NOT NULL CHECK (verdict IN ('yes', 'possible')),
               harness TEXT,
               model TEXT,
               channel TEXT NOT NULL CHECK (channel = 'pr'),
               observed_at TEXT NOT NULL,
               suspicious INTEGER NOT NULL CHECK (suspicious IN (0, 1)),
               UNIQUE (repo_id, pr_number, commit_id, rule, raw)
           );""",
        (
            "repo_id",
            "pr_number",
            "commit_id",
            "rule",
            "raw",
            "verdict",
            "harness",
            "model",
            "channel",
            "observed_at",
            "suspicious",
        ),
        """SELECT repo_id, pr_number, commit_id, rule, raw, verdict, harness, model, channel,
                  observed_at, suspicious
           FROM pr_evidence ORDER BY repo_id, pr_number, commit_id, rule, raw""",
    ),
    _Table(
        "pr_watermark",
        """CREATE TABLE pr_watermark (
               repo_id INTEGER NOT NULL,
               last_pr_number INTEGER NOT NULL,
               PRIMARY KEY (repo_id)
           );""",
        ("repo_id", "last_pr_number"),
        "SELECT repo_id, last_pr_number FROM pr_watermark ORDER BY repo_id",
    ),
    _Table(
        "features",
        """CREATE TABLE features (
               id TEXT NOT NULL,
               title TEXT NOT NULL,
               about TEXT NOT NULL DEFAULT '',
               title_history_json TEXT NOT NULL DEFAULT '[]',
               created_at TEXT NOT NULL,
               created_run_id INTEGER,
               repo_id INTEGER NOT NULL,
               live_lines INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (repo_id, id)
           );""",
        (
            "id",
            "title",
            "about",
            "title_history_json",
            "created_at",
            "created_run_id",
            "repo_id",
            "live_lines",
        ),
        """SELECT id, title, about, title_history_json, created_at, created_run_id,
                  repo_id, live_lines
           FROM features ORDER BY repo_id, id""",
        {"created_run_id": None},
    ),
    _Table(
        "commits_features",
        """CREATE TABLE commits_features (
               commit_id INTEGER NOT NULL,
               feature_id TEXT NOT NULL,
               role TEXT NOT NULL CHECK (role IN ('defines', 'touches')),
               PRIMARY KEY (commit_id, feature_id),
               UNIQUE (feature_id, commit_id)
           );""",
        ("commit_id", "feature_id", "role"),
        "SELECT commit_id, feature_id, role FROM commits_features ORDER BY commit_id, feature_id",
    ),
    _Table(
        "fix_touches",
        """CREATE TABLE fix_touches (
               fix_commit_id INTEGER NOT NULL,
               source_commit_id INTEGER NOT NULL,
               hit_lines INTEGER NOT NULL,
               PRIMARY KEY (fix_commit_id, source_commit_id)
           );""",
        ("fix_commit_id", "source_commit_id", "hit_lines"),
        "SELECT fix_commit_id, source_commit_id, hit_lines FROM fix_touches"
        " ORDER BY fix_commit_id, source_commit_id",
    ),
    _Table(
        "fix_targets",
        """CREATE TABLE fix_targets (
               fix_commit_id INTEGER NOT NULL,
               target_commit_id INTEGER NOT NULL,
               PRIMARY KEY (fix_commit_id, target_commit_id)
           );""",
        ("fix_commit_id", "target_commit_id"),
        "SELECT fix_commit_id, target_commit_id FROM fix_targets"
        " ORDER BY fix_commit_id, target_commit_id",
    ),
    _Table(
        "fixes_features",
        """CREATE TABLE fixes_features (
               fix_commit_id INTEGER NOT NULL,
               feature_id TEXT NOT NULL,
               via_fix_commit_id INTEGER,
               PRIMARY KEY (fix_commit_id, feature_id),
               UNIQUE (fix_commit_id, feature_id, via_fix_commit_id)
           );""",
        ("fix_commit_id", "feature_id", "via_fix_commit_id"),
        "SELECT fix_commit_id, feature_id, via_fix_commit_id FROM fixes_features"
        " ORDER BY fix_commit_id, feature_id, via_fix_commit_id",
    ),
    _Table(
        "feature_line_samples",
        """CREATE TABLE feature_line_samples (
               feature_id TEXT NOT NULL,
               at_commit_sha TEXT NOT NULL,
               live_lines INTEGER NOT NULL,
               PRIMARY KEY (feature_id, at_commit_sha)
           );""",
        ("feature_id", "at_commit_sha", "live_lines"),
        "SELECT feature_id, at_commit_sha, live_lines FROM feature_line_samples"
        " ORDER BY feature_id, at_commit_sha",
    ),
    _Table(
        "settings",
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
        ("key", "value"),
        "SELECT key, value FROM settings WHERE key IN"
        f" ({', '.join(f'{k!r}' for k in _SETTINGS_WHITELIST)}) ORDER BY key",
    ),
)


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
    for table in _TABLES:
        parts.append(table.ddl.strip() + "\n")
        cols = ", ".join(table.columns)
        for row in conn.execute(table.source):
            values = ", ".join(
                _lit(table.sentinels[c]) if c in table.sentinels else _lit(row[c])
                for c in table.columns
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


def materialize(conn: sqlite3.Connection, data: bytes) -> None:
    """Replay artifact bytes into conn under an SQLite authorizer: ONLY the
    curated shape is executable — CREATE TABLE of the known tables and INSERTs
    into them (plus plain reads/transactions). ATTACH/DETACH, PRAGMAs, DML
    beyond INSERT, and any other DDL are DENIED.

    This is load-bearing: the artifact digest is an unkeyed checksum, so any
    committer can recompute it — checksummed artifact bytes are DATA, never
    code. Both ingest (gather) and recovery (anchor bytes from the artifact's
    git history) replay through here. Schema bookkeeping is deliberately not
    part of the replay: see stamp_schema_steps. Raises MaterializeError on
    refusal or on a shape that does not match the curated columns."""
    conn.set_authorizer(_artifact_authorizer())
    try:
        conn.executescript(data.decode("utf-8"))
    except (sqlite3.Error, UnicodeDecodeError) as exc:
        raise MaterializeError(f"artifact refused: {exc}") from exc
    finally:
        conn.set_authorizer(None)
    _complete_shape(conn)
    _check_shape(conn)
    _check_ownership(conn)


def _complete_shape(conn: sqlite3.Connection) -> None:
    """Bring a pre-feature artifact body up to the curated shape: every
    curated table the replay did not create is created EMPTY by us, outside
    the guarded replay. Artifacts minted before a table existed (older
    schema steps) stay replayable — their rows for the new table are simply
    absent, and pure state (AI attribution) re-derives on the next walk.
    Skipping is safe here: an empty table can only mean 'no rows', and the
    shape/ownership guards below still vet every table that IS in the body."""
    for table in _TABLES:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table.name,)
        ).fetchone()
        if not exists:
            conn.execute(table.ddl)


def stamp_schema_steps(conn: sqlite3.Connection, data: bytes) -> None:
    """Mark the artifact's schema step as applied (db.connect bookkeeping).
    Runs AFTER the guarded replay: bookkeeping tables are ours, not artifact
    data, and the authorizer denies creating them."""
    m = re.search(rb"^-- schema-step: (\d+)$", data, re.M)
    step = int(m.group(1)) if m else 0
    for i in range(step + 1):
        conn.execute(f"CREATE TABLE IF NOT EXISTS _schema_step_{i:04d} (ok)")
    conn.commit()


def restore_into(conn: sqlite3.Connection, data: bytes) -> None:
    """Seed a DB from artifact bytes in ONE call: the authorizer-guarded
    replay, then the schema-step stamp. The pairing is load-bearing — losing
    half of it is losing #31 again: materialize alone leaves the
    _schema_step_* bookkeeping unstamped, and a later serialize of the seeded
    DB then derives schema-step 0 while the content is the migrated layout
    (every later restore re-runs the migrations and crashes). Raises
    MaterializeError on a refused artifact."""
    materialize(conn, data)
    stamp_schema_steps(conn, data)


KNOWN_TABLES: dict[str, tuple[str, ...]] = {t.name: t.columns for t in _TABLES}


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
    checked by the authorizer; its column list, here)."""
    created = {
        str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for name, cols in KNOWN_TABLES.items():
        if name not in created:
            raise MaterializeError(f"artifact refused: missing table {name}")
        actual = [str(r["name"]) for r in conn.execute(f"PRAGMA table_info({name})")]
        if actual != list(cols):
            raise MaterializeError(
                f"artifact refused: table {name} has columns {actual}, expected {list(cols)}"
            )


def _check_ownership(conn: sqlite3.Connection) -> None:
    """Defense in depth: an artifact is ONE repository's rows. Exactly one
    repositories row, and every row in every table must belong to it — a
    crafted artifact must not smuggle rows that relabel onto another repo
    during ingest (the remap would silently adopt them)."""
    repos = conn.execute("SELECT id FROM repositories ORDER BY id").fetchall()
    if len(repos) != 1:
        raise MaterializeError(
            f"artifact refused: {len(repos)} repositories rows (expected exactly 1)"
        )
    rid = int(repos[0]["id"])
    in_commits = "(SELECT id FROM commits)"
    in_features = "(SELECT id FROM features)"
    foreign = [
        ("commits", f"SELECT COUNT(*) c FROM commits WHERE repo_id != {rid}"),
        ("features", f"SELECT COUNT(*) c FROM features WHERE repo_id != {rid}"),
        (
            "commit_verdicts",
            f"SELECT COUNT(*) c FROM commit_verdicts WHERE commit_id NOT IN {in_commits}",
        ),
        (
            "ai_attribution",
            f"SELECT COUNT(*) c FROM ai_attribution WHERE commit_id NOT IN {in_commits}",
        ),
        (
            "ai_evidence",
            f"SELECT COUNT(*) c FROM ai_evidence WHERE commit_id NOT IN {in_commits}",
        ),
        (
            "pr_evidence",
            f"SELECT COUNT(*) c FROM pr_evidence WHERE repo_id != {rid}"
            f" OR (commit_id IS NOT NULL AND commit_id NOT IN {in_commits})",
        ),
        (
            "pr_watermark",
            f"SELECT COUNT(*) c FROM pr_watermark WHERE repo_id != {rid}",
        ),
        (
            "commits_features",
            f"SELECT COUNT(*) c FROM commits_features WHERE commit_id NOT IN {in_commits}"
            f" OR feature_id NOT IN {in_features}",
        ),
        (
            "fix_touches",
            f"SELECT COUNT(*) c FROM fix_touches WHERE fix_commit_id NOT IN {in_commits}"
            f" OR source_commit_id NOT IN {in_commits}",
        ),
        (
            "fix_targets",
            f"SELECT COUNT(*) c FROM fix_targets WHERE fix_commit_id NOT IN {in_commits}"
            f" OR target_commit_id NOT IN {in_commits}",
        ),
        (
            "fixes_features",
            f"SELECT COUNT(*) c FROM fixes_features WHERE fix_commit_id NOT IN {in_commits}"
            f" OR feature_id NOT IN {in_features}"
            f" OR (via_fix_commit_id IS NOT NULL AND via_fix_commit_id NOT IN {in_commits})",
        ),
        (
            "feature_line_samples",
            f"SELECT COUNT(*) c FROM feature_line_samples WHERE feature_id NOT IN {in_features}",
        ),
    ]
    for table, sql in foreign:
        n = int(conn.execute(sql).fetchone()["c"])
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
