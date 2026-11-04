"""The verdicts leaf: ONE owner of the recorded Commit verdict strings.

Two guards:

* COUPLING — the donut/report verdict set EQUALS the set the schema CHECK
  accepts, introspected from the generated DDL and a live DB. A verdict
  added anywhere without the report — or shown without being accepted —
  fails here. (The live bug this pins: classify writes `merge` verdicts for
  empty-diff merges, and the donut dropped them while labeling the SUM OF
  THE LISTED counts as the commit total.)
* LITERAL GUARD — no recorded-verdict string literal is hardcoded in
  src/coding_stats/*.py outside verdicts.py. Every surviving occurrence is
  whitelisted below, each with its reason and a pinned count, so a new
  literal fails the guard in either direction.
"""

import ast
import re
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from coding_stats import db, tablecatalog, verdicts

SRC = Path(__file__).parent.parent / "src" / "coding_stats"


def _check_verdicts(ddl: str) -> set[str]:
    m = re.search(r"CHECK \(verdict IN \(([^)]*)\)\)", ddl)
    assert m, f"no verdict CHECK in DDL:\n{ddl}"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def test_check_constraint_matches_recorded():
    assert _check_verdicts(verdicts.check_constraint()) == set(verdicts.RECORDED)
    assert verdicts.is_fix(verdicts.FIX) and not verdicts.is_fix(verdicts.MERGE)


def test_catalogue_generates_the_check_once():
    """The working schema's and the Artifact's DDL embed the SAME generated
    CHECK — one copy, fed through the tablecatalog from the verdicts leaf."""
    fk = tablecatalog.fk_ddl(tablecatalog.table("commit_verdicts"))
    art = tablecatalog.artifact_ddl(tablecatalog.table("commit_verdicts"))
    check = re.search(r"CHECK \(verdict IN [^)]*\)\)", verdicts.check_constraint()).group(0)
    assert check in fk and check in art
    assert _check_verdicts(fk) == set(verdicts.RECORDED)
    assert _check_verdicts(art) == set(verdicts.RECORDED)


def test_report_order_covers_every_accepted_verdict():
    """Every schema-accepted verdict is a report slice, in a stable order
    with no duplicates — the donut's slices sum to its center total by
    construction."""
    assert set(verdicts.REPORT_ORDER) == set(verdicts.RECORDED)
    assert len(verdicts.REPORT_ORDER) == len(set(verdicts.REPORT_ORDER))


def test_donut_slices_equal_the_schema_check():
    """THE coupling: the report's donut color map is keyed by exactly the
    set the schema CHECK accepts, and the donut is built over REPORT_ORDER."""
    from coding_stats import htmlreport

    assert set(htmlreport._VERDICT_COLORS) == set(verdicts.RECORDED)
    assert list(htmlreport._VERDICT_COLORS) and all(
        v in verdicts.REPORT_ORDER for v in htmlreport._VERDICT_COLORS
    )


def _commit(conn: sqlite3.Connection, sha: str, walk_index: int) -> int:
    cur = conn.execute(
        "INSERT INTO commits (repo_id, sha, tree_sha, committed_at, author_name,"
        " title, message, walk_index, kind, diff)"
        " VALUES ((SELECT id FROM repositories LIMIT 1), ?, 't', '2026-01-01', 'a',"
        " 'title', 'msg', ?, 'normal', 'x')",
        (sha, walk_index),
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def test_live_db_accepts_exactly_the_recorded_verdicts(tmp_path):
    """Every recorded verdict is accepted by the REAL schema CHECK; the
    first not-yet-recorded ADR-0008 verdict is refused. When spec #49 lands
    enhancement/chore, THIS test and verdicts.RECORDED change together —
    and the coupling above forces the reports along."""
    conn = db.connect(tmp_path / "v.db")
    conn.execute("INSERT INTO repositories (repo_key, slug, branch) VALUES ('k', 's', 'main')")
    for i, verdict in enumerate(verdicts.RECORDED, 1):
        cid = _commit(conn, f"sha-{i}", i)
        conn.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
            " model, classified_at) VALUES (?, ?, '', '', 'm', 'x')",
            (cid, verdict),
        )
    cid = _commit(conn, "sha-x", 99)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
            " model, classified_at) VALUES (?, 'enhancement', '', '', 'm', 'x')",
            (cid,),
        )
    conn.close()


def test_merge_verdicts_render_in_the_donut(tmp_path, repo, shas):
    """The live bug this run fixes, end to end: a merge verdict (classify
    writes them for empty-diff merges) is a donut slice, and the center
    total is the sum of the slices."""
    from coding_stats import gitwalk, htmlreport
    from conftest import make_db

    conn = make_db(tmp_path / "m.db", repo)
    gitwalk.walk(repo, conn, "main")
    conn.execute(
        "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
        " model, classified_at) VALUES ((SELECT id FROM commits WHERE sha = ?), ?,"
        " 'empty diff vs first parent', '', 'm', 'x')",
        (shas["c8"], verdicts.MERGE),
    )
    conn.commit()
    page = htmlreport.build(conn)
    assert "merge &nbsp;<b>1</b>" in page  # the merge slice, labeled
    conn.close()


# ---------------------------------------------------------------------------
# The literal guard: every recorded-verdict literal surviving outside
# verdicts.py, pinned per (file, token) with its reason. "Whitelist nothing
# silently" — each entry names WHY the literal is not a verdict use, and the
# count is asserted exactly, so additions AND removals fail here.
# ---------------------------------------------------------------------------

_LITERAL_WHITELIST: dict[tuple[str, str], tuple[int, str]] = {
    # classify.py — the LLM reply protocol's k tokens: the wire vocabulary
    # the model answers in, NOT verdicts. K_TO_VERDICT is the mapping
    # between the two vocabularies, so its keys must spell the tokens.
    ("classify.py", "fix"): (1, "K_TO_VERDICT key: reply-protocol k token"),
    ("classify.py", "refactor"): (1, "K_TO_VERDICT key: reply-protocol k token"),
    ("classify.py", "revert"): (1, "K_TO_VERDICT key: reply-protocol k token"),
    ("classify.py", "cleanup"): (1, "K_TO_VERDICT key: reply-protocol k token"),
    # Commit KIND literals — commits.kind's CHECK accepts ('normal',
    # 'merge'); a different vocabulary that happens to share the word.
    ("classify.py", "merge"): (2, "commit-kind comparisons, not verdicts"),
    ("gitwalk.py", "merge"): (1, "commit-kind literal (commit_kind), not a verdict"),
    # The PR landing-strategy tag ("squash" | "merge", DESIGN.md's merge
    # strategies) — again a different vocabulary sharing the word.
    ("prsweep.py", "merge"): (2, "PR landing-strategy tag, not a verdict"),
}

_RECORDED = set(verdicts.RECORDED)


def _bare_verdict_literals(path: Path) -> list[str]:
    """Bare string constants (and whole f-string fragments) whose entire
    value is a recorded verdict. Long strings (prompts, SQL, docstrings)
    never match — only standalone literals do."""
    tree = ast.parse(path.read_text())
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and type(node.value) is str and node.value in _RECORDED:
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.Constant) and type(v.value) is str and v.value in _RECORDED:
                    out.append(v.value)
    return out


def test_no_verdict_literals_outside_verdicts():
    for path in sorted(SRC.glob("*.py")):
        if path.name == "verdicts.py":
            continue
        found = Counter(_bare_verdict_literals(path))
        for token in _RECORDED:
            n = found.get(token, 0)
            expected, reason = _LITERAL_WHITELIST.get((path.name, token), (0, ""))
            assert n == expected, (
                f"{path.name}: {n} bare {token!r} literal(s), expected {expected}"
                f" ({reason or 'verdict literals live only in verdicts.py'})"
            )
