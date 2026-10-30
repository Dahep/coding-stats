"""PR-channel evidence through the Artifact chain (ticket #35).

The rows ride the Artifact like every recorded state — serializer, digest
verifier, materialize shape/ownership guards, gather union, org projection —
and are RECORDED, never re-derived: a walk over a restored pre-feature
artifact re-derives #34's git rows from the commit objects but never PR
rows. A re-run replaces one PR's rows wholesale rather than duplicating.
"""

import hashlib
import re
import sqlite3

from coding_stats import artifact, gather, gitwalk, orgview, prsweep
from coding_stats.db import connect
from conftest import commit_files, make_db

OBSERVED_AT = "2026-10-30T00:00:00+00:00"
TRAILER_VALUE = "Claude Opus 4.8 (1M context) <noreply@anthropic.com>"


def _built_db(tmp_path, repo, name="w.db"):
    """Walked DB with one trailer-carrying and one plain commit; PR rows
    recorded at both shapes (attached + suspicious)."""
    h1 = commit_files(
        repo, f"feat: trailered\n\nCo-authored-by: {TRAILER_VALUE}\n", {"a.txt": "1\n"}
    )
    h2 = commit_files(repo, "fix: rounding", {"b.txt": "2\n"})
    conn = make_db(tmp_path / name, repo)
    gitwalk.walk(repo, conn, "main")
    ids = {
        r["sha"]: int(r["id"]) for r in conn.execute("SELECT id, sha FROM commits")
    }
    prsweep.replace_pr_rows(
        conn,
        7,
        [
            prsweep.PrRow("tool-trailer", "codex:gpt-5", "yes", ids[h1], False, "codex", "gpt-5"),
            prsweep.PrRow("branch-prefix", "claude/x", "possible", ids[h2], False, "claude", None),
        ],
        OBSERVED_AT,
    )
    prsweep.replace_pr_rows(
        conn,
        8,
        [prsweep.PrRow("co-authored-by", "Ghost <g@example.org>", "yes", None, True, None, None)],
        OBSERVED_AT,
    )
    conn.commit()
    return conn, h1, h2


def _pr_rows(conn):
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT repo_id, pr_number, commit_id, rule, raw, verdict, harness, model,"
            " channel, observed_at, suspicious FROM pr_evidence"
            " ORDER BY repo_id, pr_number, commit_id, rule, raw"
        )
    ]


def pre_pr_bytes(conn) -> bytes:
    """Artifact bytes as a pre-#35 mint would have written them: no
    pr_evidence table in the body, digest re-stamped (the
    test_ai_attribution_artifact crafting pattern). Crafted content VERIFIES."""
    content = artifact.serialize(conn).decode("utf-8")
    out: list[str] = []
    skipping_ddl = False
    for line in content.splitlines(keepends=True):
        if line.startswith("CREATE TABLE pr_evidence"):
            skipping_ddl = True
            continue
        if skipping_ddl:
            if line.strip() == ");":
                skipping_ddl = False
            continue
        if line.startswith("INSERT INTO pr_evidence"):
            continue
        out.append(line)
    blank = "".join(out)
    digest = re.search(r"^-- digest: ([0-9a-f]+)$", blank, re.M).group(1)
    blanked = blank.replace(f"-- digest: {digest}", "-- digest:", 1)
    restamped = hashlib.sha256(blanked.encode()).hexdigest()
    return blanked.replace("-- digest:", f"-- digest: {restamped}", 1).encode()


def _reseat(content: str, needle: str) -> bytes:
    """Verified artifact text + one injected statement, digest re-stamped."""
    digest = re.search(r"^-- digest: ([0-9a-f]+)$", content, re.M).group(1)
    blanked = content.replace(f"-- digest: {digest}", "-- digest:", 1)
    tampered = blanked + needle
    restamped = hashlib.sha256(tampered.encode()).hexdigest()
    return tampered.replace("-- digest:", f"-- digest: {restamped}", 1).encode()


def test_artifact_round_trips_pr_evidence_rows(tmp_path, repo):
    conn, h1, h2 = _built_db(tmp_path, repo)
    before = _pr_rows(conn)
    assert len(before) == 3  # two attached + one suspicious
    data = artifact.serialize(conn)
    text = data.decode("utf-8")
    assert "CREATE TABLE pr_evidence" in text
    assert "INSERT INTO pr_evidence" in text
    assert artifact.verify_bytes(data) is None

    mat = sqlite3.connect(tmp_path / "mat.db")
    mat.row_factory = sqlite3.Row
    artifact.restore_into(mat, data)
    assert _pr_rows(mat) == before
    # the suspicious row keeps no commit and its audit fields
    susp = [r for r in _pr_rows(mat) if r[2] is None]
    assert susp and all(r[10] == 1 and r[8] == "pr" for r in susp)
    mat.close()
    conn.close()


def test_pre_pr_artifact_materializes_and_walk_never_rederives_pr_rows(tmp_path, repo):
    conn, h1, h2 = _built_db(tmp_path, repo)
    old = pre_pr_bytes(conn)
    assert artifact.verify_bytes(old) is None  # crafted bytes are valid artifacts
    conn.close()

    raw = sqlite3.connect(tmp_path / "restored.db")
    raw.row_factory = sqlite3.Row
    artifact.restore_into(raw, old)
    raw.close()
    restored = connect(tmp_path / "restored.db")
    assert restored.execute("SELECT COUNT(*) c FROM pr_evidence").fetchone()["c"] == 0
    gitwalk.backfill_attribution(restored, repo)  # #34 re-derives from git
    assert gitwalk.walk(repo, restored, "main") == 0  # nothing new to walk
    # RECORDED, never re-derived: the walk produced no PR rows
    assert restored.execute("SELECT COUNT(*) c FROM pr_evidence").fetchone()["c"] == 0
    assert restored.execute("SELECT COUNT(*) c FROM ai_evidence").fetchone()["c"] == 1
    restored.close()


def test_walk_never_touches_recorded_pr_rows(tmp_path, repo):
    conn, h1, h2 = _built_db(tmp_path, repo)
    before = _pr_rows(conn)
    assert gitwalk.walk(repo, conn, "main") == 0
    assert _pr_rows(conn) == before
    conn.close()


def test_materialize_refuses_smuggled_pr_rows(tmp_path, repo):
    conn, h1, h2 = _built_db(tmp_path, repo)
    data = artifact.serialize(conn).decode("utf-8")
    conn.close()
    hostile = _reseat(
        data,
        "INSERT INTO pr_evidence (repo_id, pr_number, commit_id, rule, raw, verdict,"
        " harness, model, channel, observed_at, suspicious)"
        " VALUES (1, 99, 999999, 'tool-trailer', 'x', 'yes', NULL, NULL, 'pr',"
        " '2026-10-30T00:00:00+00:00', 0);\n",
    )
    assert artifact.verify_bytes(hostile) is None  # digest re-stamped, shape still guarded
    mat = sqlite3.connect(":memory:")
    mat.row_factory = sqlite3.Row
    try:
        artifact.materialize(mat, hostile)
    except artifact.MaterializeError as exc:
        assert "pr_evidence" in str(exc)
    else:
        raise AssertionError("smuggled pr_evidence row was accepted")
    finally:
        mat.close()
    # a wrong repository id is refused the same way
    hostile2 = _reseat(
        data,
        "INSERT INTO pr_evidence (repo_id, pr_number, commit_id, rule, raw, verdict,"
        " harness, model, channel, observed_at, suspicious)"
        " VALUES (42, 99, NULL, 'tool-trailer', 'x', 'yes', NULL, NULL, 'pr',"
        " '2026-10-30T00:00:00+00:00', 1);\n",
    )
    mat = sqlite3.connect(":memory:")
    mat.row_factory = sqlite3.Row
    try:
        artifact.materialize(mat, hostile2)
    except artifact.MaterializeError as exc:
        assert "pr_evidence" in str(exc)
    else:
        raise AssertionError("foreign-repo pr_evidence row was accepted")
    finally:
        mat.close()


def test_gather_carries_pr_rows_and_replaces_repo_rows(tmp_path, repo):
    conn, h1, h2 = _built_db(tmp_path, repo)
    path = artifact.write(conn, tmp_path / "store")
    conn.close()
    union = tmp_path / "union.db"
    assert gather.gather(union, [path]).ingested == 1
    uconn = sqlite3.connect(union)
    uconn.row_factory = sqlite3.Row
    rows = _pr_rows(uconn)
    assert len(rows) == 3
    # commit ids are remapped into union space; the suspicious row stays NULL
    assert {r[2] is None for r in rows} == {False, True}

    # a different artifact of the same key replaces the repo's rows wholesale
    conn = connect(tmp_path / "w.db")
    c2 = commit_files(repo, "feat: more", {"c.txt": "3\n"})
    gitwalk.walk(repo, conn, "main")
    cid = int(conn.execute("SELECT id FROM commits WHERE sha = ?", (c2,)).fetchone()["id"])
    prsweep.replace_pr_rows(
        conn, 7, [prsweep.PrRow("tool-trailer", "new:hotness", "yes", cid, False, "claude", None)],
        OBSERVED_AT,
    )
    prsweep.replace_pr_rows(
        conn, 9, [prsweep.PrRow("tool-trailer", "aider:y", "yes", cid, False, "aider", "y")],
        OBSERVED_AT,
    )
    conn.commit()
    path2 = artifact.write(conn, tmp_path / "store2")
    conn.close()
    assert gather.gather(union, [path2]).ingested == 1
    rows2 = _pr_rows(uconn)
    assert len(rows2) == 3  # PR 7 (1) + PR 8 (1) + PR 9 (1): replaced, never extended
    assert (7, "new:hotness") in {(r[1], r[4]) for r in rows2}
    assert not any(r[4] == "codex:gpt-5" for r in rows2)  # PR 7's stale row is gone
    uconn.close()


def test_org_projection_carries_pr_rows(tmp_path):
    # two repos gathered into one union: the projection carries the PR rows
    # with their provenance (channel) intact
    import subprocess

    outs = {}
    sources = []
    for name in ("alpha", "beta"):
        r = tmp_path / name
        r.mkdir()
        subprocess.run(["git", "-C", str(r), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(r), "config", "user.name", "Tester"], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(r), "config", "user.email", "t@t.io"], check=True, capture_output=True
        )
        outs[name] = commit_files(r, f"feat: {name}", {f"{name}.txt": "1\n"})
        conn = make_db(tmp_path / f"{name}.db", r)
        gitwalk.walk(r, conn, "main")
        cid = int(
            conn.execute("SELECT id FROM commits WHERE sha = ?", (outs[name],)).fetchone()["id"]
        )
        prsweep.replace_pr_rows(
            conn, 3, [prsweep.PrRow("tool-trailer", f"{name}:m", "yes", cid, False, name, "m")],
            OBSERVED_AT,
        )
        conn.commit()
        sources.append(artifact.write(conn, tmp_path / f"{name}-store"))
        conn.close()
    union = tmp_path / "union.db"
    assert gather.gather(union, sources).ingested == 2
    uconn = sqlite3.connect(union)
    uconn.row_factory = sqlite3.Row
    proj = orgview.org(uconn)
    rows = _pr_rows(proj)
    assert {(r[8], r[4]) for r in rows} == {("pr", "alpha:m"), ("pr", "beta:m")}
    if proj is not uconn:
        proj.close()
    uconn.close()
