"""Walk-integration of the git-only AI attribution detector (ticket #34).

Synthetic git fixtures: every verdict/evidence row the walk stores is a pure
function of the commit objects it touches — re-derivable on partial
recompute, deterministic across fresh DBs, and matched on the RAW identity
strings even when a .mailmap resolves them.
"""

import os
import subprocess

from coding_stats import gitwalk
from conftest import make_db, run

TRAILER_VALUE = "Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
TRAILER_LINE = f"Co-authored-by: {TRAILER_VALUE}"
SQUASH_MSG = (
    "feat: add the thing (#123)\n"
    "\n"
    "* feat: add core\n"
    "\n"
    "  Assisted-by: claude-code:Opus 4.8 (1M context)\n"
    "\n"
    "* fix typo\n"
    "\n"
    f"{TRAILER_LINE}\n"
)


def commit_as(repo, msg, files, name="Tester", email="t@t.io", cname=None, cemail=None):
    """One commit with explicit author/committer identity (raw strings)."""
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    run(repo, "add", "-A")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": cname or name,
        "GIT_COMMITTER_EMAIL": cemail or email,
    }
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", msg],
        check=True,
        capture_output=True,
        env=env,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def verdicts(conn):
    return {
        r["sha"]: (r["verdict"], r["harness"], r["model"])
        for r in conn.execute(
            "SELECT c.sha, a.verdict, a.harness, a.model FROM ai_attribution a"
            " JOIN commits c ON c.id = a.commit_id"
        )
    }


def evidence(conn):
    out = {}
    for r in conn.execute(
        "SELECT c.sha, e.rule, e.raw, e.verdict FROM ai_evidence e"
        " JOIN commits c ON c.id = e.commit_id"
        " ORDER BY c.walk_index, e.rule, e.raw"
    ):
        out.setdefault(r["sha"], []).append((r["rule"], r["raw"], r["verdict"]))
    rank = {"co-authored-by": 1, "agent-email": 2, "name-suffix": 3, "body-signature": 4,
            "tool-trailer": 5, "branch-prefix": 6}
    return {sha: sorted(rows, key=lambda t: (rank[t[0]], t[1])) for sha, rows in out.items()}


def test_walk_records_verdict_and_evidence_per_commit(tmp_path, repo):
    shas = {}
    shas["trailer"] = commit_as(
        repo,
        f"feat: trailered work\n\n{TRAILER_LINE}\n",
        {"a.txt": "1\n"},
        name="Daniel Herrmann",
        email="daniel@example.org",
    )
    shas["signature"] = commit_as(
        repo,
        "feat: signed work\n\nGenerated with Codex.\n",
        {"b.txt": "2\n"},
    )
    shas["bot"] = commit_as(
        repo,
        "chore: deps",
        {"c.txt": "3\n"},
        name="dependabot[bot]",
        email="dependabot[bot]@users.noreply.github.com",
    )
    shas["plain"] = commit_as(repo, "fix: rounding", {"d.txt": "4\n"})

    conn = make_db(tmp_path / "w.db", repo)
    assert gitwalk.walk(repo, conn, "main") == 4

    v = verdicts(conn)
    assert v[shas["trailer"]] == ("yes", "claude", "Opus 4.8 (1M context)")
    assert v[shas["signature"]] == ("yes", "codex", None)
    assert v[shas["bot"]] == ("yes", "dependabot", None)
    # nothing matched is 'no-evidence' — stored, and never a "human" verdict
    assert v[shas["plain"]] == ("no-evidence", None, None)

    ev = evidence(conn)
    assert ev[shas["trailer"]] == [("co-authored-by", TRAILER_VALUE, "yes")]
    assert ev[shas["signature"]] == [("body-signature", "Generated with Codex.", "yes")]
    assert ev[shas["bot"]] == [
        ("agent-email", "dependabot[bot]@users.noreply.github.com", "yes"),
        ("name-suffix", "dependabot[bot]", "yes"),
    ]
    assert shas["plain"] not in ev
    conn.close()


def test_walk_records_committer_identity_too(tmp_path, repo):
    sha = commit_as(
        repo,
        "feat: x",
        {"a.txt": "1\n"},
        name="Tester",
        email="t@t.io",
        cname="Daniel (aider)",
        cemail="daniel@example.org",
    )
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    assert verdicts(conn)[sha] == ("yes", "aider", None)
    assert evidence(conn)[sha] == [("name-suffix", "Daniel (aider)", "yes")]
    conn.close()


def test_squash_framing_trailers_are_recorded_like_raw_trailers(tmp_path, repo):
    sha = commit_as(repo, SQUASH_MSG, {"a.txt": "1\n"})
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    assert verdicts(conn)[sha] == ("yes", "claude", "Opus 4.8 (1M context)")
    assert evidence(conn)[sha] == [
        ("co-authored-by", TRAILER_VALUE, "yes"),
        ("tool-trailer", "claude-code:Opus 4.8 (1M context)", "yes"),
    ]
    conn.close()


def test_merge_subject_branch_prefix_is_possible_tier(tmp_path, repo):
    commit_as(repo, "chore: base", {"a.txt": "1\n"})
    run(repo, "checkout", "-b", "claude/feature-x")
    commit_as(repo, "feat: on the branch", {"b.txt": "2\n"})
    run(repo, "checkout", "main")
    run(
        repo,
        "merge",
        "--no-ff",
        "claude/feature-x",
        "-m",
        "Merge pull request #5 from daniel/claude/feature-x",
    )
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    merge_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    v = verdicts(conn)
    assert v[merge_sha] == ("possible", "claude", None)
    assert evidence(conn)[merge_sha] == [("branch-prefix", "claude/feature-x", "possible")]
    conn.close()


def test_match_uses_raw_identity_before_mailmap(tmp_path, repo):
    # a .mailmap resolves the agent identity to a human one: detection keys
    # on the RAW strings and still attributes the commit
    mailmap = "Daniel Herrmann <daniel@example.org> Claude Opus <noreply@anthropic.com>\n"
    sha = commit_as(
        repo,
        f"feat: x\n\n{TRAILER_LINE}\n",
        {"a.txt": "1\n", ".mailmap": mailmap},
        name="Claude Opus",
        email="noreply@anthropic.com",
    )
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    assert verdicts(conn)[sha] == ("yes", "claude", "Opus 4.8 (1M context)")
    assert evidence(conn)[sha] == [
        ("co-authored-by", TRAILER_VALUE, "yes"),
        ("agent-email", "noreply@anthropic.com", "yes"),
    ]
    conn.close()


def test_walk_is_idempotent(tmp_path, repo):
    commit_as(repo, f"feat: x\n\n{TRAILER_LINE}\n", {"a.txt": "1\n"})
    commit_as(repo, "fix: y", {"b.txt": "2\n"})
    conn = make_db(tmp_path / "w.db", repo)
    assert gitwalk.walk(repo, conn, "main") == 2
    before = (dict(verdicts(conn)), dict(evidence(conn)))
    assert gitwalk.walk(repo, conn, "main") == 0  # nothing new
    assert (dict(verdicts(conn)), dict(evidence(conn))) == before
    conn.close()


def test_backfill_re_derives_missing_rows_purely(tmp_path, repo):
    # an artifact minted before this feature carries no attribution rows:
    # the next walk re-derives them from the same commit objects
    commit_as(repo, f"feat: x\n\n{TRAILER_LINE}\n", {"a.txt": "1\n"})
    commit_as(repo, "fix: y", {"b.txt": "2\n"})
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    before = (dict(verdicts(conn)), dict(evidence(conn)))
    conn.execute("DELETE FROM ai_evidence")
    conn.execute("DELETE FROM ai_attribution")
    conn.commit()
    assert gitwalk.walk(repo, conn, "main") == 0  # no new commits
    assert (dict(verdicts(conn)), dict(evidence(conn))) == before
    conn.close()


def test_detection_is_deterministic_across_fresh_dbs(tmp_path, repo):
    commit_as(repo, SQUASH_MSG, {"a.txt": "1\n"})
    commit_as(
        repo,
        "chore: deps",
        {"b.txt": "2\n"},
        name="Copilot",
        email="1+Copilot@users.noreply.github.com",
    )
    outs = []
    for name in ("one.db", "two.db"):
        conn = make_db(tmp_path / name, repo)
        gitwalk.walk(repo, conn, "main")
        outs.append((dict(verdicts(conn)), dict(evidence(conn))))
        conn.close()
    assert outs[0] == outs[1]
