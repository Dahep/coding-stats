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

from coding_stats import artifact, gather, gitwalk, orgview, prsweep, recovery
from coding_stats.db import connect
from conftest import commit_files, make_db

OBSERVED_AT = "2026-10-30T00:00:00+00:00"
TRAILER_VALUE = "Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
WM_DATE = "2026-10-28T12:00:00+00:00"  # the marker's merge date in _built_db


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
    # the recorded watermark beside the rows: everything through PR 8
    conn.execute(
        "INSERT INTO pr_watermark (repo_id, last_pr_number, last_merged_at)"
        " VALUES ((SELECT id FROM repositories ORDER BY id LIMIT 1), 8, ?)",
        (WM_DATE,),
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


def _strip_tables(conn, *tables, step: int | None = None) -> bytes:
    """Artifact bytes as a pre-feature mint would have written them: without
    the named tables' bodies, and with the header schema-step stamped to that
    mint's own step (shape completion only applies to older steps), digest
    re-stamped (the test_ai_attribution_artifact crafting pattern). Crafted
    content VERIFIES."""
    content = artifact.serialize(conn).decode("utf-8")
    out: list[str] = []
    skipping_ddl = False
    for line in content.splitlines(keepends=True):
        if line.startswith("-- schema-step:") and step is not None:
            out.append(f"-- schema-step: {step}\n")
            continue
        if any(line.startswith(f"CREATE TABLE {t}") for t in tables):
            skipping_ddl = True
            continue
        if skipping_ddl:
            if line.strip() == ");":
                skipping_ddl = False
            continue
        if any(line.startswith(f"INSERT INTO {t} ") for t in tables):
            continue
        out.append(line)
    blank = "".join(out)
    digest = re.search(r"^-- digest: ([0-9a-f]+)$", blank, re.M).group(1)
    blanked = blank.replace(f"-- digest: {digest}", "-- digest:", 1)
    restamped = hashlib.sha256(blanked.encode()).hexdigest()
    return blanked.replace("-- digest:", f"-- digest: {restamped}", 1).encode()


def pre_pr_bytes(conn) -> bytes:
    """A pre-#35 artifact body: no PR-channel state at all (a #34-era mint:
    schema step 4)."""
    return _strip_tables(conn, "pr_evidence", "pr_watermark", step=4)


def pre_watermark_bytes(conn) -> bytes:
    """A pre-#36 artifact body: PR rows but no watermark table (a #35-era
    mint: schema step 5)."""
    return _strip_tables(conn, "pr_watermark", step=5)


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
        conn.execute(
            "INSERT INTO pr_watermark (repo_id, last_pr_number, last_merged_at)"
            " VALUES ((SELECT id FROM repositories ORDER BY id LIMIT 1), ?, ?)",
            (10 + len(sources), WM_DATE),
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
    # the recorded watermarks ride the projection per repository too — the
    # resume marker's merge date with the PR number (the loop stamps 10 for
    # alpha — len(sources)==0 — then 11 for beta)
    assert [(r["last_pr_number"], r["last_merged_at"]) for r in proj.execute(
        "SELECT last_pr_number, last_merged_at FROM pr_watermark"
        " ORDER BY last_pr_number")] == [(10, WM_DATE), (11, WM_DATE)]
    if proj is not uconn:
        proj.close()
    uconn.close()


# ---------------------------------------------------------------------------
# the watermark rides the Artifact beside the rows (ticket #36)
# ---------------------------------------------------------------------------


WM_DATE = "2026-10-28T12:00:00+00:00"


def _watermark_rows(conn):
    return [
        (int(r["repo_id"]), int(r["last_pr_number"]), r["last_merged_at"])
        for r in conn.execute(
            "SELECT repo_id, last_pr_number, last_merged_at FROM pr_watermark ORDER BY repo_id"
        )
    ]


def test_artifact_round_trips_the_pr_watermark(tmp_path, repo):
    conn, h1, h2 = _built_db(tmp_path, repo)
    before = _watermark_rows(conn)
    assert len(before) == 1 and before[0][1] == 8 and before[0][2] == WM_DATE
    data = artifact.serialize(conn)
    text = data.decode("utf-8")
    assert "CREATE TABLE pr_watermark" in text
    assert "INSERT INTO pr_watermark" in text
    assert "last_merged_at" in text  # the resume marker's merge date rides too
    assert artifact.verify_bytes(data) is None

    mat = sqlite3.connect(tmp_path / "mat.db")
    mat.row_factory = sqlite3.Row
    artifact.restore_into(mat, data)
    assert _watermark_rows(mat) == before
    mat.close()
    conn.close()


def test_pre_watermark_artifact_materializes_and_reads_zero(tmp_path, repo):
    conn, h1, h2 = _built_db(tmp_path, repo)
    old = pre_watermark_bytes(conn)  # a #35-era mint: rows but no watermark
    assert artifact.verify_bytes(old) is None  # crafted bytes are valid artifacts
    conn.close()

    mat = sqlite3.connect(tmp_path / "mat.db")
    mat.row_factory = sqlite3.Row
    artifact.restore_into(mat, old)
    assert _watermark_rows(mat) == []  # nothing recorded: sweeps start over
    assert len(_pr_rows(mat)) == 3  # the rows still ride
    mat.close()


def test_partial_recompute_restores_the_watermark_and_pr_rows_unchanged(tmp_path, repo):
    conn, h1, h2 = _built_db(tmp_path, repo)  # rows for PRs 7/8, watermark 8
    anchor = artifact.serialize(conn)  # the older verifiable Artifact version
    rows_at_anchor = _pr_rows(conn)

    # recorded state advances past the anchor: PR 9 sweeps in
    c2 = commit_files(repo, "feat: more", {"c.txt": "3\n"})
    gitwalk.walk(repo, conn, "main")
    cid = int(conn.execute("SELECT id FROM commits WHERE sha = ?", (c2,)).fetchone()["id"])
    prsweep.replace_pr_rows(
        conn, 9, [prsweep.PrRow("tool-trailer", "newer:row", "yes", cid, False, "claude", None)],
        OBSERVED_AT,
    )
    conn.execute("UPDATE pr_watermark SET last_pr_number = 9")
    conn.commit()

    restored = recovery.rebuild(tmp_path / "w.db", anchor, conn, repo, "main")
    # the watermark and PR rows survive unchanged: restored from the anchor
    # exactly as that version recorded them (never re-derived, and the
    # pre-recompute DB's carried-over state never clobbers them)
    assert _pr_rows(restored) == rows_at_anchor
    assert _watermark_rows(restored) == [(1, 8, WM_DATE)]
    # and the partial recompute's re-walk never touches them either
    assert gitwalk.walk(repo, restored, "main") == 1  # only the newer commit
    assert _pr_rows(restored) == rows_at_anchor
    assert _watermark_rows(restored) == [(1, 8, WM_DATE)]
    restored.close()


def test_gather_carries_the_watermark_and_replaces_it_wholesale(tmp_path, repo):
    conn, h1, h2 = _built_db(tmp_path, repo)  # watermark (8, WM_DATE)
    path = artifact.write(conn, tmp_path / "store")
    conn.close()
    union = tmp_path / "union.db"
    assert gather.gather(union, [path]).ingested == 1
    uconn = sqlite3.connect(union)
    uconn.row_factory = sqlite3.Row
    # the resume marker's merge date rides the gather union with the rest
    assert _watermark_rows(uconn) == [(1, 8, WM_DATE)]

    # a newer artifact of the same key replaces the watermark wholesale
    newer = "2026-10-29T12:00:00+00:00"
    conn = connect(tmp_path / "w.db")
    conn.execute("UPDATE pr_watermark SET last_pr_number = 9, last_merged_at = ?", (newer,))
    conn.commit()
    path2 = artifact.write(conn, tmp_path / "store2")
    conn.close()
    assert gather.gather(union, [path2]).ingested == 1
    assert _watermark_rows(uconn) == [(1, 9, newer)]  # replaced, never extended
    uconn.close()


def test_step_6_minted_artifact_completes_the_shape_and_keeps_the_watermark(tmp_path, repo):
    # a #36-era mint (pr_watermark at its step-6 shape: no merge date) stays
    # replayable: the shape completion adds the column, the recorded
    # watermark survives with a NULL date (a marker that claims no merge
    # date), and the DB's own step-7 migration on open is then a no-op
    conn, h1, h2 = _built_db(tmp_path, repo)
    old = _step6_bytes(conn)
    assert artifact.verify_bytes(old) is None  # crafted bytes are valid artifacts
    conn.close()

    mat = sqlite3.connect(tmp_path / "mat.db")
    mat.row_factory = sqlite3.Row
    artifact.restore_into(mat, old)
    cols = [str(r["name"]) for r in mat.execute("PRAGMA table_info(pr_watermark)")]
    assert cols == ["repo_id", "last_pr_number", "last_merged_at"]
    assert _watermark_rows(mat) == [(1, 8, None)]  # watermark preserved, date unknown
    mat.close()

    opened = connect(tmp_path / "mat.db")  # step 7 applies cleanly on top
    assert _watermark_rows(opened) == [(1, 8, None)]
    opened.close()


def _step6_bytes(conn) -> bytes:
    """Artifact bytes as a #36-era mint wrote them: pr_watermark at its
    step-6 shape (repo_id, last_pr_number — no merge date), header stamped
    schema step 6, digest re-stamped (the crafting pattern above)."""
    content = artifact.serialize(conn).decode("utf-8")
    out: list[str] = []
    in_wm_ddl = False
    for line in content.splitlines(keepends=True):
        if line.startswith("-- schema-step:"):
            out.append("-- schema-step: 6\n")
            continue
        if line.startswith("CREATE TABLE pr_watermark"):
            in_wm_ddl = True
            out.append(line)
            continue
        if in_wm_ddl:
            if "last_merged_at" in line:
                continue  # the column did not exist at step 6
            if line.strip() == ");":
                in_wm_ddl = False
            out.append(line)
            continue
        if line.startswith("INSERT INTO pr_watermark "):
            line = re.sub(
                r"\(repo_id, last_pr_number, last_merged_at\) VALUES"
                r" \((\d+), (\d+), '[^']*'\)",
                r"(repo_id, last_pr_number) VALUES (\1, \2)",
                line,
            )
        out.append(line)
    blank = "".join(out)
    digest = re.search(r"^-- digest: ([0-9a-f]+)$", blank, re.M).group(1)
    blanked = blank.replace(f"-- digest: {digest}", "-- digest:", 1)
    restamped = hashlib.sha256(blanked.encode()).hexdigest()
    return blanked.replace("-- digest:", f"-- digest: {restamped}", 1).encode()

