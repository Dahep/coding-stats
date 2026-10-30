"""AI attribution through the Artifact chain and the reports (ticket #34).

Evidence rows ride the Artifact like every other recorded state: serializer,
digest verifier, materialize shape/ownership guards, gather union, and the
report renderers. A pre-feature artifact (no attribution tables in its body)
still materializes — completed to the current shape with empty tables — and
the walk re-derives the rows from the commit objects, deterministically.
``no-evidence`` is rendered as exactly that and never as "human".
"""

import hashlib
import re
import sqlite3

from coding_stats import artifact, gather, gitwalk, htmlreport, recovery
from coding_stats.db import connect
from coding_stats.report import text_report
from conftest import commit_files, make_db, run

TRAILER_VALUE = "Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
TRAILER_LINE = f"Co-authored-by: {TRAILER_VALUE}"


def _built_db(tmp_path, repo, name="w.db"):
    """Walked DB with one trailer-carrying and one plain commit."""
    h1 = commit_files(repo, f"feat: trailered\n\n{TRAILER_LINE}\n", {"a.txt": "1\n"})
    h2 = commit_files(repo, "fix: rounding", {"b.txt": "2\n"})
    conn = make_db(tmp_path / name, repo)
    gitwalk.walk(repo, conn, "main")
    return conn, h1, h2


def _rows(conn):
    return {
        r["sha"]: (r["verdict"], r["harness"], r["model"])
        for r in conn.execute(
            "SELECT c.sha, a.verdict, a.harness, a.model FROM ai_attribution a"
            " JOIN commits c ON c.id = a.commit_id"
        )
    }


def pre_attribution_bytes(conn) -> bytes:
    """Artifact bytes as a pre-feature mint would have written them: no
    ai_attribution/ai_evidence in the body, digest re-stamped. Mirrors the
    test_migration_legacy old-format trick: crafted content must VERIFY."""
    content = artifact.serialize(conn).decode("utf-8")
    out: list[str] = []
    skipping_ddl = False
    for line in content.splitlines(keepends=True):
        if line.startswith("CREATE TABLE ai_attribution") or line.startswith(
            "CREATE TABLE ai_evidence"
        ):
            skipping_ddl = True
            continue
        if skipping_ddl:
            if line.strip() == ");":
                skipping_ddl = False
            continue
        if line.startswith("INSERT INTO ai_attribution") or line.startswith(
            "INSERT INTO ai_evidence"
        ):
            continue
        out.append(line)
    blank = "".join(out)
    digest = re.search(r"^-- digest: ([0-9a-f]+)$", blank, re.M).group(1)
    blanked = blank.replace(f"-- digest: {digest}", "-- digest:", 1)
    restamped = hashlib.sha256(blanked.encode()).hexdigest()
    return blanked.replace("-- digest:", f"-- digest: {restamped}", 1).encode()


def _reseat(content: str, needle: str) -> bytes:
    """Take verified artifact text, inject `needle` as a trailing statement,
    and re-stamp the digest (a crafted-but-verifying artifact)."""
    digest = re.search(r"^-- digest: ([0-9a-f]+)$", content, re.M).group(1)
    blanked = content.replace(f"-- digest: {digest}", "-- digest:", 1)
    tampered = blanked + needle
    restamped = hashlib.sha256(tampered.encode()).hexdigest()
    return tampered.replace("-- digest:", f"-- digest: {restamped}", 1).encode()


def test_artifact_round_trips_attribution_rows(tmp_path, repo):
    conn, h1, h2 = _built_db(tmp_path, repo)
    data = artifact.serialize(conn)
    text = data.decode("utf-8")
    assert "CREATE TABLE ai_attribution" in text
    assert "CREATE TABLE ai_evidence" in text
    assert "INSERT INTO ai_attribution" in text
    assert "INSERT INTO ai_evidence" in text
    assert artifact.verify_bytes(data) is None

    mat = sqlite3.connect(tmp_path / "mat.db")
    mat.row_factory = sqlite3.Row
    artifact.restore_into(mat, data)
    got = {
        r["sha"]: (r["verdict"], r["harness"], r["model"])
        for r in mat.execute(
            "SELECT c.sha, a.verdict, a.harness, a.model FROM ai_attribution a"
            " JOIN commits c ON c.id = a.commit_id"
        )
    }
    assert got == _rows(conn)
    rules = [
        (r["rule"], r["raw"])
        for r in mat.execute("SELECT rule, raw FROM ai_evidence ORDER BY rule, raw")
    ]
    assert rules == [("co-authored-by", TRAILER_VALUE)]
    mat.close()
    conn.close()


def test_pre_attribution_artifact_materializes_and_walk_rederives(tmp_path, repo):
    conn, h1, h2 = _built_db(tmp_path, repo)
    before = _rows(conn)
    old = pre_attribution_bytes(conn)
    assert artifact.verify_bytes(old) is None  # crafted bytes are valid artifacts
    conn.close()

    # the recovery seed path: materialize + stamp + migrate, then the walk
    raw = sqlite3.connect(tmp_path / "restored.db")
    raw.row_factory = sqlite3.Row
    artifact.restore_into(raw, old)
    raw.close()
    restored = connect(tmp_path / "restored.db")
    # the completed tables exist and are empty (pre-feature artifact)
    assert restored.execute("SELECT COUNT(*) c FROM ai_attribution").fetchone()["c"] == 0
    assert restored.execute("SELECT COUNT(*) c FROM ai_evidence").fetchone()["c"] == 0
    recovery.rehydrate_diffs(restored, repo)
    assert gitwalk.walk(repo, restored, "main") == 0  # nothing new to walk
    assert _rows(restored) == before  # re-derived purely from the commit objects
    # and the re-derived state serializes with the current shape
    assert "INSERT INTO ai_attribution" in artifact.serialize(restored).decode("utf-8")
    restored.close()


def test_materialize_refuses_smuggled_evidence_rows(tmp_path, repo):
    conn, *_ = _built_db(tmp_path, repo)
    data = artifact.serialize(conn).decode("utf-8")
    conn.close()
    hostile = _reseat(
        data,
        "INSERT INTO ai_evidence (commit_id, rule, raw, verdict)"
        " VALUES (999999, 'co-authored-by', 'x', 'yes');\n",
    )
    assert artifact.verify_bytes(hostile) is None  # digest re-stamped, shape still guarded
    mat = sqlite3.connect(":memory:")
    mat.row_factory = sqlite3.Row
    try:
        artifact.materialize(mat, hostile)
    except artifact.MaterializeError as exc:
        assert "ai_evidence" in str(exc)
    else:
        raise AssertionError("smuggled ai_evidence row was accepted")
    finally:
        mat.close()


def test_gather_union_carries_attribution_rows(tmp_path, repo):
    conn, h1, h2 = _built_db(tmp_path, repo)
    path = artifact.write(conn, tmp_path / "store")
    conn.close()
    union = tmp_path / "union.db"
    result = gather.gather(union, [path])
    assert result.ingested == 1
    uconn = sqlite3.connect(union)
    uconn.row_factory = sqlite3.Row
    got = {
        r["sha"]: (r["verdict"], r["harness"], r["model"])
        for r in uconn.execute(
            "SELECT c.sha, a.verdict, a.harness, a.model FROM ai_attribution a"
            " JOIN commits c ON c.id = a.commit_id"
        )
    }
    assert got[h1] == ("yes", "claude", "Opus 4.8 (1M context)")
    assert got[h2] == ("no-evidence", None, None)
    rules = [
        (r["rule"], r["raw"]) for r in uconn.execute("SELECT rule, raw FROM ai_evidence")
    ]
    assert rules == [("co-authored-by", TRAILER_VALUE)]
    uconn.close()


def test_union_report_renders_attribution_across_repos(tmp_path):
    # two repos gathered into one union: the org projection keeps the
    # winner's (identical) attribution rows per Shared commit and the
    # report renders attribution for both repositories
    outs = {}
    sources = []
    for name, msg in (("alpha", f"feat: a\n\n{TRAILER_LINE}\n"), ("beta", "fix: b")):
        r = tmp_path / name
        r.mkdir()
        run(r, "init", "-b", "main")
        run(r, "config", "user.name", "Tester")
        run(r, "config", "user.email", "t@t.io")
        run(r, "config", "commit.gpgsign", "false")
        outs[name] = commit_files(r, msg, {f"{name}.txt": "1\n"})
        conn = make_db(tmp_path / f"{name}.db", r)
        gitwalk.walk(r, conn, "main")
        sources.append(artifact.write(conn, tmp_path / f"{name}-store"))
        conn.close()
    union = tmp_path / "union.db"
    assert gather.gather(union, sources).ingested == 2
    uconn = sqlite3.connect(union)
    uconn.row_factory = sqlite3.Row
    assert (
        uconn.execute("SELECT COUNT(*) c FROM ai_attribution").fetchone()["c"] == 2
    )
    html = htmlreport.build(uconn)
    assert "AI attribution" in html
    assert TRAILER_VALUE.replace("<", "&lt;").replace(">", "&gt;") in html
    assert "human" not in html.lower()
    uconn.close()


def test_reports_render_attribution_state_and_never_human(tmp_path, repo):
    conn, h1, h2 = _built_db(tmp_path, repo)
    html = htmlreport.build(conn)
    assert "AI attribution" in html
    # evidence rows are auditable in the report (HTML-escaped for safety)
    assert TRAILER_VALUE.replace("<", "&lt;").replace(">", "&gt;") in html
    assert "noreply@anthropic.com" in html
    assert "Opus 4.8 (1M context)" in html  # the model the trailer names
    assert "no-evidence" in html
    assert "human" not in html.lower()
    text = text_report(conn)
    assert "AI attribution" in text
    assert "human" not in text.lower()
    conn.close()
