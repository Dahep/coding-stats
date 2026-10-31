"""Shared fixture: a tiny repo with known feature/fix/refactor/revert shape.

History (chronological, all on main):
  c1 "feature one"   a.txt: alpha/beta/charlie          -> feature 1
  c2 "feature two"   b.txt: one/two                     -> feature 2
  c3 "fix alpha"     a.txt rewrites alpha->ALPHA        -> fix (targets c1)
  c4 "refactor"      a.txt reorders lines               -> refactor (no fix rows)
  c5 "fix of fix"    a.txt rewrites ALPHA->ALPHAV2      -> fix (targets c3; lineage -> c1)
  c6 "dup add"       adds dup.txt with same patch       -> cleanup-ish (patch-id dupe of c7)
  c7 "add dup.txt"   dup.txt introduced                 -> patch-equivalent with c6
  c8 "whitespace"    a.txt adds trailing newline only   -> cleanup (no deleted lines)
"""

import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from coding_stats import cli, gitwalk
from coding_stats.classify import create_feature
from coding_stats.db import connect
from coding_stats.identity import register_repository


def run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def commit_files(repo: Path, msg: str, files: dict[str, str]) -> str:
    for name, content in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    run(repo, "add", "-A")
    run(repo, "commit", "-m", msg)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_db(path: Path, repo: Path, branch: str = "main"):
    """A fresh DB with THE repository row registered (wave 1: one repo per DB)."""
    conn = connect(path)
    register_repository(conn, repo, branch)
    return conn


def add_feature(conn, title: str, about: str = "", created_at: str = "2026-01-01T00:00:00Z") -> str:
    """Insert a feature with the repo-prefixed id for the DB's single repo."""
    repo_id = conn.execute("SELECT id FROM repositories ORDER BY id").fetchone()[0]
    return create_feature(conn, repo_id, title, about, created_at)


def store_worktree(repo: Path, branch: str = "main") -> Path:
    """The Data-branch worktree the engine defaults to (datastore.
    default_worktree's .git-dir case): ci land commits/pushes from here."""
    return repo / ".git" / "coding-stats-store" / branch


def store_dir(repo: Path, branch: str = "main") -> Path:
    """The artifact store inside the Data-branch worktree."""
    return store_worktree(repo, branch) / ".coding-stats"


def git_out(repo: Path, *args: str) -> str:
    """Read-only git output for assertions."""
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def bare_remote(tmp_path) -> str:
    import subprocess

    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(path)], check=True)
    return str(path)


def with_remote(repo: Path, bare) -> None:
    import subprocess

    run(repo, "remote", "add", "origin", bare)
    run(repo, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "-C", bare, "symbolic-ref", "HEAD", "refs/heads/main"], check=True)


# ---------------------------------------------------------------------------
# PR-tier shared helpers (the zero-network API/git fixture layer of the
# prsweep suites) and the squash-PR sweep builders shared by the watermark
# suites — named helpers here so every suite builds the same fixtures
# ---------------------------------------------------------------------------

TRAILER_VALUE = "Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
TRAILER_LINE = f"Co-authored-by: {TRAILER_VALUE}"
OBSERVED_AT = "2026-10-30T00:00:00+00:00"


def pr(number, **kw):
    """One PR as the LIST endpoint returns it (the Pull Request Simple
    schema's fields the sweep reads). The wire shape is the real one: mergedness
    is the ``merged_at`` timestamp-or-null — there is NO ``merged`` boolean on
    the wire (``merged: true`` in GitHub's docs and ticket bodies is the intent,
    not the wire format). ``merge_commit_sha`` is the landing sha the
    walk-coverage check reads; ``merged_at`` doubles as the resume marker's
    merge date (``merged_at=`` overrides it per row)."""
    merged = kw.get("merged", kw.get("state", "closed") != "open")
    return {
        "number": number,
        "title": kw.get("title", f"PR {number}"),
        "body": kw.get("body", ""),
        "state": kw.get("state", "closed"),
        "merged_at": kw.get("merged_at", "2026-10-29T12:00:00+00:00") if merged else None,
        "merge_commit_sha": kw.get("merge_commit_sha"),
        "user": {"login": kw.get("login", "daniel"), "type": kw.get("utype", "User")},
        "head": {"ref": kw.get("head", "feature/x")},
        "base": {"ref": kw.get("base", "main")},
    }


class FakeTransport:
    """Injected API transport: serves list pages, counts every request."""

    def __init__(self, pulls):
        self.pulls = pulls  # in API order (descending number)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        m = re.search(r"[?&]page=(\d+)", url)
        page = int(m.group(1)) if m else 1
        start = (page - 1) * 100
        return self.pulls[start : start + 100]


def githubize(conn):
    """Register a GitHub identity for the DB's repo (the API side derives
    owner/name from the repositories row's clone URL)."""
    conn.execute(
        "UPDATE repositories SET clone_url = 'https://github.com/dahep/coding-stats',"
        " repo_key = 'https://github.com/dahep/coding-stats@main'"
    )
    conn.commit()


def push_head_ref(repo, sha, n):
    """The GitHub-side PR head ref, on the bare 'origin' remote."""
    run(repo, "push", "-q", "origin", f"{sha}:refs/pull/{n}/head")


def start_remote(tmp_path, repo):
    """Initial commit plus a bare 'origin' remote (PR head refs live there)."""
    commit_files(repo, "init", {"a.txt": "a\n"})
    with_remote(repo, bare_remote(tmp_path))


def pr_rows(conn, pr_number=None):
    out = []
    sql = (
        "SELECT c.sha, e.pr_number, e.rule, e.raw, e.verdict, e.channel, e.observed_at,"
        " e.suspicious, e.harness, e.model FROM pr_evidence e"
        " LEFT JOIN commits c ON c.id = e.commit_id"
    )
    args: tuple = ()
    if pr_number is not None:
        sql += " WHERE e.pr_number = ?"
        args = (pr_number,)
    sql += " ORDER BY e.pr_number, e.rule, e.raw"
    for r in conn.execute(sql, args):
        out.append(tuple(r))
    return out


def rows_at(conn, sha):
    return {
        (r["rule"], r["raw"], r["verdict"], r["channel"], r["suspicious"])
        for r in conn.execute(
            "SELECT e.rule, e.raw, e.verdict, e.channel, e.suspicious FROM pr_evidence e"
            " JOIN commits c ON c.id = e.commit_id WHERE c.sha = ?",
            (sha,),
        )
    }


def commit_id(conn, sha):
    return int(conn.execute("SELECT id FROM commits WHERE sha = ?", (sha,)).fetchone()["id"])


def land_squash_prs(repo, numbers):
    """One squash-merged PR per number, oldest first: a trailer-carrying
    head pushed to refs/pull/N/head (then the branch deleted), then the
    ``(#n)`` landing on main (the same patch — the head-parentage check
    passes)."""
    shas = {}
    for n in numbers:
        run(repo, "checkout", "-b", f"claude/f{n}")
        h = commit_files(repo, f"feat: core {n}\n\n{TRAILER_LINE}\n", {f"f{n}.txt": f"{n}\n"})
        push_head_ref(repo, h, n)
        run(repo, "checkout", "main")
        run(repo, "branch", "-D", f"claude/f{n}")
        shas[n] = commit_files(repo, f"feat: work {n} (#{n})", {f"f{n}.txt": f"{n}\n"})
    return shas


def pr_list_rows(*numbers):
    """The list endpoint's rows for the given PR numbers (API order:
    descending number), every PR carrying a body signal and an agent-prefixed
    head ref so each one records at least one evidence row."""
    return [
        pr(n, body="Generated with Codex.\n", head=f"claude/f{n}")
        for n in sorted(numbers, reverse=True)
    ]


def primed_sweep_db(tmp_path, repo, numbers):
    """Landed squash PRs + a walked, GitHub-identified DB (the sweep's
    pre-state): 1 remote fetch per PR, list pages served by the injected
    transport."""
    start_remote(tmp_path, repo)
    shas = land_squash_prs(repo, numbers)
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    return conn, shas


def cli_sweep_db(tmp_path, repo, numbers):
    """A CLI working DB over landed squash PRs (init + walk, identified as
    a GitHub repo) — the pr-sweep CLI faces' pre-state."""
    start_remote(tmp_path, repo)
    land_squash_prs(repo, numbers)
    db = tmp_path / "cli.db"
    cli.main(["init", str(repo), "--db", str(db)])
    cli.main(["walk", "--db", str(db)])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    githubize(conn)
    conn.close()
    return db


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    run(r, "init", "-b", "main")
    run(r, "config", "user.name", "Tester")
    run(r, "config", "user.email", "t@t.io")
    run(r, "config", "commit.gpgsign", "false")
    return r


@pytest.fixture()
def shas(repo: Path) -> dict[str, str]:
    history: dict[str, str] = {}
    history["c1"] = commit_files(
        repo,
        "feature one: alpha module",
        {
            "a.txt": "alpha\nbeta\ncharlie\n",
        },
    )
    history["c2"] = commit_files(
        repo,
        "feature two: beta index",
        {
            "b.txt": "one\ntwo\n",
        },
    )
    history["c3"] = commit_files(
        repo,
        "fix alpha handling",
        {
            "a.txt": "ALPHA\nbeta\ncharlie\n",
        },
    )
    history["c4"] = commit_files(
        repo,
        "refactor: reorder alpha module",
        {
            "a.txt": "charlie\nALPHA\nbeta\n",
        },
    )
    history["c5"] = commit_files(
        repo,
        "fix: actually repair alpha v2",
        {
            "a.txt": "charlie\nALPHAV2\nbeta\n",
        },
    )
    # patch-id duplicate setup: introduce dup.txt, remove it, re-add the same content
    history["c6"] = commit_files(
        repo,
        "add dup suport",
        {
            "dup.txt": "dup\nextra\n",
        },
    )
    (repo / "dup.txt").unlink()
    run(repo, "add", "-A")
    run(repo, "commit", "-m", "remove dup file")
    history["cr"] = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    history["c7"] = commit_files(
        repo,
        "add dup support",
        {
            "dup.txt": "dup\nextra\n",
        },
    )
    # whitespace-only change (adds a blank line): no deletions, no blame targets
    history["c8"] = commit_files(
        repo,
        "tidy: blank line",
        {
            "a.txt": "charlie\n\nALPHAV2\nbeta\n",
        },
    )
    return history


@pytest.fixture()
def dbconn(tmp_path: Path):
    return connect(tmp_path / "test.db")
