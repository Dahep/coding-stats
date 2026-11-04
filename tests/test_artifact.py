"""Artifact serializer tests: determinism, curation, digest verify, materialization."""

import contextlib
import hashlib
import json
import re
import sqlite3

import pytest

from coding_stats import artifact
from coding_stats.derive import annotate_all, close_lineage, run_derive
from coding_stats.gitwalk import walk
from conftest import add_feature, commit_files, make_db

CLASSIFIED_AT = "2026-01-01T00:00:00Z"


def _verdict(conn, sha, verdict):
    conn.execute(
        "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
        " classified_at) VALUES ((SELECT id FROM commits WHERE sha = ?), ?, 'why', 'raw', 'm',"
        " ?)",
        (sha, verdict, CLASSIFIED_AT),
    )


def _history(repo):
    h1 = commit_files(repo, "feature a", {"f.txt": "a1\na2\n"})
    h2 = commit_files(repo, "fix a1", {"f.txt": "A1\na2\n"})
    return h1, h2


def _built_db(tmp_path, repo, name="a.db", hist=None):
    """A working DB with verdicts, features, fix lineage and live-lines samples."""
    h1, h2 = hist or _history(repo)
    conn = make_db(tmp_path / name, repo)
    walk(repo, conn, "main")
    fa = add_feature(conn, "feature a")
    conn.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role)"
        " VALUES ((SELECT id FROM commits WHERE sha = ?), ?, 'defines')",
        (h1, fa),
    )
    for sha, v in ((h1, "feature"), (h2, "fix")):
        _verdict(conn, sha, v)
    conn.commit()
    annotate_all(repo, conn)
    close_lineage(conn)
    run_derive(conn, repo)  # with the tables just rebuilt, the live-lines pass
    conn.commit()
    return conn, fa, h1, h2


def _header(content: str, key: str) -> str:
    m = re.search(rf"^-- {key}: ?(.*)$", content, re.M)
    assert m is not None
    return m.group(1)


def test_serialize_is_byte_stable_and_digest_verifies(tmp_path, repo):
    conn, *_ = _built_db(tmp_path, repo)
    first = artifact.serialize(conn)
    second = artifact.serialize(conn)
    assert first == second  # byte-equal across calls
    path = artifact.write(conn, repo)
    assert path == repo / ".coding-stats" / "coding-stats.sql"
    # the store holds only the SQL artifact (ADR-0006 clause 5)
    assert artifact.verify(path) is None
    content = path.read_text(encoding="utf-8")
    assert artifact.digest_of(path) == _header(content, "digest")
    # tampering with any byte breaks verification
    tampered = tmp_path / "tampered.sql"
    body = bytearray(first)
    body[len(body) // 2] ^= 0x01
    tampered.write_bytes(bytes(body))
    err = artifact.verify(tampered)
    assert err is not None and "mismatch" in err
    # a missing digest line is an error, not an ok
    bare = tmp_path / "bare.sql"
    bare.write_text("-- coding-stats artifact\n", encoding="utf-8")
    assert artifact.verify(bare) is not None
    conn.close()


def test_serialize_stable_across_identically_built_dbs(tmp_path, repo):
    # two DBs over the same history: wall-clock (repositories.added_at etc.)
    # must not leak into the artifact
    hist = _history(repo)
    conn_a, *_ = _built_db(tmp_path, repo, "a.db", hist)
    conn_b, *_ = _built_db(tmp_path, repo, "b.db", hist)
    assert artifact.serialize(conn_a) == artifact.serialize(conn_b)
    conn_a.close()
    conn_b.close()


def test_artifact_curation_excludes_machine_state(tmp_path, repo):
    conn, fa, h1, h2 = _built_db(tmp_path, repo)
    conn.execute("INSERT INTO runs (config_json, started_at, updated_at) VALUES ('{}', 'x', 'y')")
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('repo', ?)",
        (json.dumps(str(repo)),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('classify_session_id', '\"ses_x\"')"
    )
    conn.commit()
    content = artifact.serialize(conn).decode("utf-8")

    # runs table and machine-local settings are out entirely
    assert "INSERT INTO runs" not in content and "CREATE TABLE runs" not in content
    assert "classify_session_id" not in content
    assert str(repo) not in content
    # wall-clock columns frozen to sentinels, raw diff payload emptied
    assert "datetime('now')" not in content
    assert "NULL, '');" in content  # repositories.added_at sentinel
    assert "a1\\na2" not in content and "A1\\na2" not in content  # no diff payload
    # classified_at is kept: classified-once audit data, not churn
    assert CLASSIFIED_AT in content
    conn.close()


def test_artifact_materializes_standalone_with_committed_shapes(tmp_path, repo):
    conn, fa, h1, h2 = _built_db(tmp_path, repo)
    path = artifact.write(conn, repo)
    mat = sqlite3.connect(tmp_path / "materialized.db")
    mat.row_factory = sqlite3.Row
    mat.executescript(path.read_text(encoding="utf-8"))

    tables = {r[0] for r in mat.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "runs" not in tables  # curated projection
    assert {
        "repositories",
        "commits",
        "commit_verdicts",
        "features",
        "commits_features",
        "fix_touches",
        "fix_targets",
        "fixes_features",
        "feature_line_samples",
        "settings",
    } <= tables

    # drift guard: the artifact's shape matches the working tables exactly
    for table in sorted(tables):
        work_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        mat_cols = [r["name"] for r in mat.execute(f"PRAGMA table_info({table})")]
        assert mat_cols == work_cols, table

    assert mat.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"] == 2
    assert mat.execute("SELECT COUNT(*) c FROM features").fetchone()["c"] == 1
    row = mat.execute("SELECT * FROM repositories").fetchone()
    assert row["added_at"] == ""  # sentinel, not wall-clock
    diff = mat.execute(
        "SELECT diff, added_lines, deleted_lines, churn_lines FROM commits ORDER BY id"
    ).fetchall()
    assert all(r["diff"] == "" for r in diff)
    assert (diff[0]["added_lines"], diff[0]["deleted_lines"]) == (2, 0)
    assert mat.execute("SELECT COUNT(*) c FROM feature_line_samples").fetchone()["c"] > 0
    keys = {r["key"] for r in mat.execute("SELECT key FROM settings")}
    assert keys <= {"backfill_state", "branch", "model", "min_target_lines", "unattributed_lines"}
    assert (
        mat.execute("SELECT COUNT(*) c FROM commit_verdicts WHERE run_id IS NOT NULL").fetchone()[
            "c"
        ]
        == 0
    )
    conn.close()
    mat.close()


def test_seed_working_db_stamps_the_artifacts_schema_step(tmp_path, repo):
    """The one-seed API keeps replay and stamp inseparable: the seeded DB
    carries the artifact's own stamp (its schema step, not a fabricated 0),
    so a later serialize of the seeded DB re-emits the same header stamp —
    losing #31 again is not reachable through this path."""
    conn, *_ = _built_db(tmp_path, repo)
    data = artifact.serialize(conn)
    step = artifact.parse_header(data).schema_step
    assert step > 0  # the source artifact is stamped; the compare is not vacuous

    seeded = artifact.seed_working_db(tmp_path / "seeded.db", data, register=False)
    markers = {
        r[0]
        for r in seeded.execute("SELECT name FROM sqlite_master WHERE name LIKE '_schema_step_%'")
    }
    assert markers == {f"_schema_step_{i:04d}" for i in range(step + 1)}
    assert artifact.parse_header(artifact.serialize(seeded)).schema_step == step
    seeded.close()
    conn.close()


def test_seed_working_db_refuses_data_that_does_not_verify(tmp_path, repo):
    """Digest verification gates the ONE seed path itself: stale-digest
    bytes are refused before anything is built (entry points keep their own
    earlier checks for their own messages — this one cannot be bypassed)."""
    conn, *_ = _built_db(tmp_path, repo)
    data = artifact.serialize(conn)
    tampered = data.replace(b"INSERT INTO features", b"INSERT INTO featurez", 1)
    assert artifact.verify_bytes(tampered) is not None  # the digest is stale
    with pytest.raises(artifact.MaterializeError, match="refusing artifact"):
        artifact.seed_working_db(tmp_path / "seeded.db", tampered, register=False)
    assert not (tmp_path / "seeded.db").exists()  # refused before the file existed
    conn.close()


def _reforged_low_stamp(data: bytes, step: int) -> bytes:
    """Set the header's schema-step line to `step` and RE-compute the digest:
    the unkeyed digest cannot authenticate the stamp — anyone who can commit
    a Data branch can mint a header whose stamp the body did not earn."""
    text = re.sub(
        rb"^-- schema-step: \d+$",
        f"-- schema-step: {step}".encode("ascii"),
        data,
        count=1,
        flags=re.M,
    )
    m = artifact._DIGEST_LINE_RE.search(text)
    blanked = text[: m.start()] + artifact.DIGEST_BLANK.encode("ascii") + text[m.end() :]
    digest = hashlib.sha256(blanked).hexdigest()
    return text[: m.start()] + f"-- digest: {digest}".encode("ascii") + text[m.end() :]


def test_seed_working_db_refuses_a_forged_low_stamp(tmp_path, repo):
    """A current-shape body carrying a forged (re-digested) low header stamp
    may not crash: whatever db.apply_steps does with migration steps re-run
    on newer content, every outcome is either a clean MaterializeError
    refusal (gather/seeding skip the sibling, recovery refuses, no pinned
    message touched) or a quiet success on steps that tolerate their own
    re-run — a raw sqlite3.Error/RuntimeError escaping the refusal channel
    is the crash this forbids. The strictest stamp (0) refuses concretely:
    its step 1 needs machine-local settings the artifact does not carry."""
    from coding_stats.gather import GatherError, _load

    conn, *_ = _built_db(tmp_path, repo)
    data = artifact.serialize(conn)
    current = artifact.parse_header(data).schema_step
    assert current > 1  # steps 1..current exist that a 0-stamp would re-run

    for step in range(current):
        forged = _reforged_low_stamp(data, step)
        assert artifact.verify_bytes(forged) is None  # the digest freshly minted
        target = tmp_path / f"seeded{step}.db"
        if step == 0:
            # the object body needs machine-local settings step 1 cannot recover
            with pytest.raises(artifact.MaterializeError, match="artifact refused"):
                artifact.seed_working_db(target, forged, register=False)
            with pytest.raises(GatherError, match="artifact refused"):
                _load(repo, "forged0", forged)
            continue
        try:
            seeded = artifact.seed_working_db(target, forged, register=False)
        except artifact.MaterializeError:
            continue  # clean refusal — the only rejection channel
        seeded.close()  # otherwise: steps tolerate their own re-run, fine
        # gather's channel lets the refusal through, never a raw error
        with contextlib.suppress(GatherError):
            _load(repo, f"forged{step}", forged)
    conn.close()


def test_header_fields(tmp_path, repo):
    conn, fa, h1, h2 = _built_db(tmp_path, repo)
    content = artifact.serialize(conn).decode("utf-8")
    assert content.startswith("-- coding-stats artifact\n-- format: 1\n")
    assert _header(content, "schema-step") == "8"
    assert _header(content, "repository-key") == "local:repo@main"
    assert _header(content, "covered-through") == h2  # latest walked commit
    digest = _header(content, "digest")
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    # digest covers the content with its own value blanked
    blanked = re.sub(r"^-- digest: .*$", "-- digest:", content, count=1, flags=re.M)
    assert hashlib.sha256(blanked.encode("utf-8")).hexdigest() == digest
    conn.close()


# ------------------------------------------- header injection + byte digest --


def test_header_values_are_injection_safe(tmp_path, repo):
    conn, *_ = _built_db(tmp_path, repo)
    evil = "x\nCREATE TABLE pwned(x);\n\x07\"quoted\"'\\'"
    conn.execute("UPDATE repositories SET repo_key = ?", (evil,))
    conn.commit()
    path = artifact.write(conn, repo)
    content = path.read_bytes()

    # header values stay inside their single comment line: nothing executable
    key_lines = [ln for ln in content.splitlines() if ln.startswith(b"-- repository-key:")]
    assert len(key_lines) == 1
    assert b"\\x0A" in key_lines[0] and b"\\x07" in key_lines[0]  # newline/bell escaped
    assert b"\\x5C" in key_lines[0]  # the escape char itself is escaped (injective)
    # the dump replays standalone and no injected table appears
    mat = sqlite3.connect(tmp_path / "mat.db")
    mat.executescript(content.decode("utf-8"))
    names = {r[0] for r in mat.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "pwned" not in names
    mat.close()
    assert artifact.verify(path) is None


def test_header_dot_command_probe_is_byte_safe(tmp_path, repo):
    conn, *_ = _built_db(tmp_path, repo)
    conn.execute("UPDATE repositories SET repo_key = ?", ("-- .shell echo pwned",))
    conn.commit()
    path = artifact.write(conn, repo)
    content = path.read_bytes()
    # encodes nothing -> stays literal for readability, but no line ever
    # starts with '.' (sqlite3 dot-command) or leaves the comment
    assert b"-- repository-key: -- .shell echo pwned" in content
    assert all(not ln.startswith(b".") for ln in content.splitlines())
    mat = sqlite3.connect(tmp_path / "mat.db")
    mat.executescript(content.decode("utf-8"))
    mat.close()
    assert artifact.verify(path) is None


def test_verify_is_byte_exact(tmp_path, repo):
    conn, *_ = _built_db(tmp_path, repo)
    path = artifact.write(conn, repo)
    data = path.read_bytes()

    crlf = tmp_path / "crlf.sql"
    crlf.write_bytes(data.replace(b"\n", b"\r\n"))
    assert artifact.verify(crlf) is not None  # CRLF must not launder through text mode

    bad = tmp_path / "bad.sql"
    bad.write_bytes(data + b"\xff\xfe")
    err = artifact.verify(bad)
    assert err is not None and "mismatch" in err  # failure verdict, no decode crash

    # digest_of hashes exact bytes and is stable over odd content
    first = artifact.digest_of(bad)
    assert first == artifact.digest_of(bad)
    assert b"\r" not in data  # serialize writes LF-only bytes, stable to hash
