"""Channel-pr evidence sweep (ticket #35, ADR-0007 clauses 3-5).

Synthetic git fixtures + an injected API transport: zero real network. The
sweep lists closed merged PRs (list pages only), lazily fetches
``refs/pull/N/head`` for squash/rebase PRs over the git protocol, detects on
the head commits with #34's rule table plus PR-level signals, maps onto the
landed sha, and records PR-channel evidence rows once — recorded, never
re-derived, replaced per PR on re-runs. Git-only evidence on the same commit
outranks inherited PR evidence; suspicious rows are never attribution.
"""

import re
import subprocess

import pytest

from coding_stats import aiattr, cli, gitwalk, prsweep
from conftest import commit_files, make_db, run

TRAILER_VALUE = "Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
TRAILER_LINE = f"Co-authored-by: {TRAILER_VALUE}"
OBSERVED_AT = "2026-10-30T00:00:00+00:00"


def pr(number, **kw):
    """One PR as the LIST endpoint returns it (the Pull Request Simple
    schema's fields the sweep reads). The wire shape is the real one: mergedness
    is the ``merged_at`` timestamp-or-null — there is NO ``merged`` boolean on
    the wire (``merged: true`` in GitHub's docs and ticket bodies is the intent,
    not the wire format). ``merge_commit_sha`` is the landing sha the
    walk-coverage check reads; ``state`` separates still-open PRs (the watermark
    clamp) from closed ones."""
    merged = kw.get("merged", kw.get("state", "closed") != "open")
    return {
        "number": number,
        "title": kw.get("title", f"PR {number}"),
        "body": kw.get("body", ""),
        "state": kw.get("state", "closed"),
        "merged_at": "2026-10-29T12:00:00+00:00" if merged else None,
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


@pytest.fixture()
def fetch_log(monkeypatch):
    """Spy on the git-protocol head fetch (never the API)."""
    calls = []
    orig = prsweep.fetch_head_ref

    def spy(repo, n):
        calls.append(n)
        return orig(repo, n)

    monkeypatch.setattr(prsweep, "fetch_head_ref", spy)
    return calls


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
    from conftest import bare_remote, with_remote

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


# ---------------------------------------------------------------------------
# aiattr's PR-level signals (same rule table, never a second engine)
# ---------------------------------------------------------------------------


def test_pr_signals_body_footer_bot_author_and_branch_prefix():
    m = {
        (x.rule, x.raw): (x.harness, x.model)
        for x in aiattr.pr_matches(
            body="Assisted-by: codex:gpt-5\n",
            author_login="dependabot[bot]",
            author_is_bot=True,
            head_branch="claude/feature",
        )
    }
    assert ("tool-trailer", "codex:gpt-5") in m
    assert m[("tool-trailer", "codex:gpt-5")] == ("codex", "gpt-5")
    assert ("name-suffix", "dependabot[bot]") in m
    assert m[("name-suffix", "dependabot[bot]")] == ("dependabot", None)
    assert ("branch-prefix", "claude/feature") in m
    assert m[("branch-prefix", "claude/feature")] == ("claude", None)
    attr = aiattr.pr_attribution(
        body="Assisted-by: codex:gpt-5\n",
        author_login="dependabot[bot]",
        author_is_bot=True,
        head_branch="claude/feature",
    )
    assert attr.verdict == "yes"  # rules 1-5 matched (bot author is a yes-tier hit)


def test_pr_signals_plain_human_and_unprefixed_branch_are_no_evidence():
    attr = aiattr.pr_attribution(
        body="just a description\n",
        author_login="daniel",
        author_is_bot=False,
        head_branch="feature/x",
    )
    assert attr.verdict == "no-evidence"
    assert attr.evidence == ()


def test_pr_signals_bot_author_without_suffix_pattern_still_names_harness():
    rows = aiattr.pr_matches(
        body="", author_login="renovate", author_is_bot=True, head_branch=None
    )
    assert [(x.rule, x.raw, x.harness) for x in rows] == [
        ("name-suffix", "renovate", "renovate"),
    ]


# ---------------------------------------------------------------------------
# real list schema: mergedness comes from merged_at, never a `merged` boolean
# ---------------------------------------------------------------------------


def test_list_row_without_merged_key_is_classified_by_merged_at():
    # the REAL list schema: no `merged` boolean on the wire — mergedness is
    # the merged_at timestamp-or-null (`merged: true` in docs/ticket bodies is
    # the intent, not the wire format)
    merged_row = pr(5)
    assert "merged" not in merged_row
    assert merged_row["merged_at"]
    assert prsweep._parse_pr(merged_row).merged

    unmerged_row = pr(6, merged=False)
    assert "merged" not in unmerged_row
    assert unmerged_row["merged_at"] is None
    assert not prsweep._parse_pr(unmerged_row).merged

    # a bare list row with merged_at and nothing else merges-classifies
    parsed = prsweep._parse_pr({"number": 7, "merged_at": "2026-10-29T12:00:00+00:00"})
    assert parsed.merged and parsed.number == 7
    assert not prsweep._parse_pr({"number": 8, "merged_at": None}).merged


# ---------------------------------------------------------------------------
# listing: pages only, zero per-PR calls
# ---------------------------------------------------------------------------


def test_list_pages_cost_is_one_batch_of_list_requests():
    pulls = [pr(n, merged=(n % 2 == 0)) for n in range(250, 0, -1)]
    t = FakeTransport(pulls)
    listing = prsweep.list_pulls("dahep", "coding-stats", 0, transport=t)
    assert listing.requests == 3 == -(-250 // 100)  # ceil(n/100): list pages only
    assert all(
        "pulls?" in u and "repos/dahep/coding-stats/pulls" in u and "state=all" in u for u in t.urls
    )
    assert [p.number for p in listing.merged] == list(range(2, 251, 2))  # merged, ascending


def test_list_stops_paging_at_the_bound():
    pulls = [pr(n) for n in range(250, 0, -1)]
    t = FakeTransport(pulls)
    listing = prsweep.list_pulls("dahep", "coding-stats", 240, transport=t)
    assert [p.number for p in listing.merged] == [241, 242, 243, 244, 245, 246, 247, 248, 249, 250]
    assert listing.requests == 2  # second page is entirely <= the bound: stop


def test_sweep_costs_list_pages_only_over_many_prs(tmp_path, repo):
    # 250 PRs on the numbering sequence, none of them landable here: the
    # sweep must spend zero per-PR API calls and touch no commit-list endpoint
    pulls = [pr(n, merged=True, base="develop") for n in range(125, 0, -1)]
    pulls += [pr(n, merged=False, base="main") for n in range(250, 125, -1)]
    conn = make_db(tmp_path / "w.db", repo)
    githubize(conn)
    t = FakeTransport(pulls)  # descending, like the API
    res = prsweep.sweep(conn, repo, "main", 0, transport=t, now=OBSERVED_AT)
    assert res.requests == 3 == -(-250 // 100)  # ceil(n/100) list pages
    assert res.processed == 0 and res.rows == 0
    assert len(t.urls) == 3  # zero per-PR calls


# ---------------------------------------------------------------------------
# squash-merged + branch-deleted PRs (acceptance criterion 1)
# ---------------------------------------------------------------------------


def test_squash_pr_recovers_evidence_the_walk_would_miss(tmp_path, repo, fetch_log):
    start_remote(tmp_path, repo)
    run(repo, "checkout", "-b", "claude/feature")
    h1 = commit_files(repo, f"feat: core work\n\n{TRAILER_LINE}\n", {"b.txt": "b\n"})
    push_head_ref(repo, h1, 5)
    run(repo, "checkout", "main")
    run(repo, "branch", "-D", "claude/feature")  # branch deleted after merge
    squash = commit_files(repo, "feat: the work (#5)", {"b.txt": "b\n"})

    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    t = FakeTransport(
        [
            pr(
                5,
                body="Assisted-by: codex:gpt-5\n",
                login="dependabot[bot]",
                utype="Bot",
                head="claude/feature",
            )
        ]
    )
    res = prsweep.sweep(conn, repo, "main", 0, transport=t, now=OBSERVED_AT)
    assert res.processed == 1 and res.rows == 4 and res.suspicious == 0
    assert fetch_log == [5]  # fetched lazily over the git protocol

    assert rows_at(conn, squash) == {
        ("co-authored-by", TRAILER_VALUE, "yes", "pr", 0),
        ("tool-trailer", "codex:gpt-5", "yes", "pr", 0),
        ("name-suffix", "dependabot[bot]", "yes", "pr", 0),
        ("branch-prefix", "claude/feature", "possible", "pr", 0),
    }
    # the walk alone missed every one of them (git-only shadow stays intact)
    assert conn.execute("SELECT COUNT(*) c FROM ai_evidence").fetchone()["c"] == 0
    v = conn.execute(
        "SELECT verdict, harness, model FROM ai_attribution WHERE commit_id = ?",
        (commit_id(conn, squash),),
    ).fetchone()
    assert tuple(v) == ("no-evidence", None, None)

    # the reconstructed harness/model the walk could not know
    eff = prsweep.effective_attribution(conn, commit_id(conn, squash))
    assert eff.verdict == "yes"
    assert eff.harness == "claude"  # rank-first evidence naming one (co-authored-by)
    assert eff.model == "Opus 4.8 (1M context)"


def test_squash_pr_rows_carry_channel_observed_at_and_pr_number(tmp_path, repo, fetch_log):
    start_remote(tmp_path, repo)
    run(repo, "checkout", "-b", "claude/feature")
    h1 = commit_files(repo, f"feat: core work\n\n{TRAILER_LINE}\n", {"b.txt": "b\n"})
    push_head_ref(repo, h1, 5)
    run(repo, "checkout", "main")
    run(repo, "branch", "-D", "claude/feature")
    squash = commit_files(repo, "feat: the work (#5)", {"b.txt": "b\n"})
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    t = FakeTransport([pr(5, body="Generated with Codex.\n", head="claude/feature")])
    prsweep.sweep(conn, repo, "main", 0, transport=t, now=OBSERVED_AT)
    rows = pr_rows(conn, 5)
    assert rows and all(r[1] == 5 for r in rows)  # pr number on every row
    assert all(r[5] == "pr" for r in rows)  # channel pr
    assert all(r[6] == OBSERVED_AT for r in rows)  # observed_at recorded
    assert all(r[0] == squash for r in rows)  # attached at the mapped sha
    assert all(r[7] == 0 for r in rows)


# ---------------------------------------------------------------------------
# rebase-merged PRs: patch-id mapping (acceptance criterion 3)
# ---------------------------------------------------------------------------


def test_rebase_pr_maps_head_commits_by_patch_id_without_duplicates(tmp_path, repo, fetch_log):
    start_remote(tmp_path, repo)
    run(repo, "checkout", "-b", "codex/reb")
    commit_files(repo, f"feat: one\n\n{TRAILER_LINE}\n", {"b.txt": "b\n"})
    h2 = commit_files(repo, "fix: two\n\nGenerated with Codex.\n", {"c.txt": "c\n"})
    push_head_ref(repo, h2, 6)  # PR head: the pre-rebase chain
    run(repo, "checkout", "main")
    commit_files(repo, "unrelated", {"z.txt": "z\n"})  # force the rebase to rewrite
    run(repo, "rebase", "main", "codex/reb")
    run(repo, "checkout", "main")
    run(repo, "merge", "--ff-only", "codex/reb")
    run(repo, "branch", "-D", "codex/reb")
    twins = gitwalk.list_commits(repo, "main")[-2:]  # the rewritten copies
    r1, r2 = twins

    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    t = FakeTransport([pr(6, head="codex/reb")])
    res = prsweep.sweep(conn, repo, "main", 0, transport=t, now=OBSERVED_AT)
    assert res.processed == 1 and res.suspicious == 0
    assert fetch_log == [6]

    # the head commits' own signals were ALREADY recorded by the walk at the
    # identical landed commits: recorded once (git rows), never twice
    git_rows = {
        r["sha"]: {(r["rule"], r["raw"])}
        for r in conn.execute(
            "SELECT c.sha, e.rule, e.raw FROM ai_evidence e JOIN commits c ON c.id = e.commit_id"
        )
    }
    assert ("co-authored-by", TRAILER_VALUE) in git_rows[r1]
    assert ("body-signature", "Generated with Codex.") in git_rows[r2]
    assert rows_at(conn, r1) == set()
    # only the PR-level signal is new, attached at the head tip's twin
    assert rows_at(conn, r2) == {("branch-prefix", "codex/reb", "possible", "pr", 0)}


def test_rebase_pr_maps_each_head_commit_to_its_own_twin(tmp_path, repo, fetch_log):
    start_remote(tmp_path, repo)
    run(repo, "checkout", "-b", "codex/reb")
    commit_files(repo, "feat: one", {"b.txt": "b\n"})
    h2 = commit_files(repo, "fix: two", {"c.txt": "c\n"})
    push_head_ref(repo, h2, 6)
    run(repo, "checkout", "main")
    commit_files(repo, "unrelated", {"z.txt": "z\n"})
    run(repo, "rebase", "main", "codex/reb")
    run(repo, "checkout", "main")
    run(repo, "merge", "--ff-only", "codex/reb")
    run(repo, "branch", "-D", "codex/reb")
    r1, r2 = gitwalk.list_commits(repo, "main")[-2:]

    # the PR body is the only carrier here (head commits were plain)
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    t = FakeTransport([pr(6, body="Assisted-by: aider:x\n", head="codex/reb")])
    prsweep.sweep(conn, repo, "main", 0, transport=t, now=OBSERVED_AT)
    # PR-level rows anchor at the tip twin; head rows mapped by patch-id
    assert rows_at(conn, r2) == {
        ("tool-trailer", "aider:x", "yes", "pr", 0),
        ("branch-prefix", "codex/reb", "possible", "pr", 0),
    }
    assert rows_at(conn, r1) == set()


# ---------------------------------------------------------------------------
# plain-merge PRs: never fetched, recorded once from their commits
# ---------------------------------------------------------------------------


def test_merge_pr_records_from_commits_once_and_never_fetches(tmp_path, repo, fetch_log):
    start_remote(tmp_path, repo)
    run(repo, "checkout", "-b", "claude/merged")
    h1 = commit_files(repo, f"feat: m1\n\n{TRAILER_LINE}\n", {"b.txt": "b\n"})
    push_head_ref(repo, h1, 7)  # exists on the remote, must not be fetched
    run(repo, "checkout", "main")
    run(
        repo,
        "merge",
        "--no-ff",
        "-m",
        "Merge pull request #7 from daniel/claude/merged\n\nPR title",
        "claude/merged",
    )
    merge = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    run(repo, "branch", "-D", "claude/merged")

    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    t = FakeTransport([pr(7, body="Generated with Codex.\n", head="claude/merged")])
    res = prsweep.sweep(conn, repo, "main", 0, transport=t, now=OBSERVED_AT)
    assert res.processed == 1 and res.suspicious == 0
    assert fetch_log == []  # identical commits: zero fetches

    # recorded ONCE from their commits: the head commit's trailer is new,
    # the PR body signature is new; the branch prefix is already a git row
    # at the merge commit (the walk's branch-prefix rule) and is not doubled
    assert rows_at(conn, merge) == {
        ("co-authored-by", TRAILER_VALUE, "yes", "pr", 0),
        ("body-signature", "Generated with Codex.", "yes", "pr", 0),
    }
    git = {
        (r["rule"], r["raw"])
        for r in conn.execute(
            "SELECT e.rule, e.raw FROM ai_evidence e JOIN commits c ON c.id = e.commit_id"
            " WHERE c.sha = ?",
            (merge,),
        )
    }
    assert ("branch-prefix", "claude/merged") in git


# ---------------------------------------------------------------------------
# head-parentage check: suspicious, never a wrong attribution (AC 3)
# ---------------------------------------------------------------------------


def test_force_pushed_head_is_suspicious_marked_never_attribution(tmp_path, repo, fetch_log):
    start_remote(tmp_path, repo)
    run(repo, "checkout", "-b", "claude/fp")
    h1 = commit_files(repo, f"feat: pushed\n\n{TRAILER_LINE}\n", {"h.txt": "h\n"})
    push_head_ref(repo, h1, 9)  # later force-pushed past what landed
    run(repo, "checkout", "main")
    run(repo, "branch", "-D", "claude/fp")
    squash = commit_files(repo, "feat: landed (#9)", {"s.txt": "s\n"})

    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    t = FakeTransport([pr(9, body="Generated with Codex.\n", head="claude/fp")])
    res = prsweep.sweep(conn, repo, "main", 0, transport=t, now=OBSERVED_AT)
    assert res.processed == 1 and res.suspicious > 0

    rows = pr_rows(conn, 9)
    assert rows and all(r[0] is None for r in rows)  # never attached
    assert all(r[7] == 1 for r in rows)  # suspicious-marked
    assert {r[2] for r in rows} >= {"co-authored-by", "body-signature"}
    # and they never become attribution
    eff = prsweep.effective_attribution(conn, commit_id(conn, squash))
    assert eff.verdict == "no-evidence"
    assert eff.harness is None and eff.model is None
    assert eff.evidence == ()


def test_walked_landing_with_unmappable_head_is_suspicious_not_a_wait(tmp_path, repo, fetch_log):
    # the landing sha IS walked (nothing waits for the walk) yet the fetched
    # head maps onto nothing — a post-merge force-push: a genuinely
    # unmappable head records suspicious rows, never a walk-stale stall
    start_remote(tmp_path, repo)
    run(repo, "checkout", "-b", "claude/fp")
    h1 = commit_files(repo, f"feat: pushed\n\n{TRAILER_LINE}\n", {"h.txt": "h\n"})
    push_head_ref(repo, h1, 9)
    run(repo, "checkout", "main")
    run(repo, "branch", "-D", "claude/fp")
    squash = commit_files(repo, "feat: landed, subject edited", {"h.txt": "h\n"})  # no (#9)
    # post-merge force-push: the ref now carries content that never landed
    run(repo, "checkout", "-b", "claude/other")
    never = commit_files(repo, "feat: never landed", {"z.txt": "z\n"})
    run(repo, "push", "-q", "-f", "origin", f"{never}:refs/pull/9/head")  # force-push the ref
    run(repo, "checkout", "main")
    run(repo, "branch", "-D", "claude/other")

    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    t = FakeTransport(
        [
            pr(
                9,
                body="Generated with Codex.\n",
                head="claude/fp",
                merge_commit_sha=squash,
            )
        ]
    )
    res = prsweep.sweep(conn, repo, "main", 0, transport=t, now=OBSERVED_AT)
    assert res.stop is None  # not a wait: the landing IS in the walked set
    assert res.processed == 1 and res.suspicious > 0
    rows = pr_rows(conn, 9)
    assert rows and all(r[0] is None and r[7] == 1 for r in rows)  # suspicious, unattached


def test_vanished_head_ref_is_recorded_suspicious_not_attributed(tmp_path, repo, fetch_log):
    start_remote(tmp_path, repo)  # origin exists; the PR head ref is GONE
    squash = commit_files(repo, "feat: landed (#2)", {"s.txt": "s\n"})
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    t = FakeTransport([pr(2, body="Generated with Codex.\n")])
    res = prsweep.sweep(conn, repo, "main", 0, transport=t, now=OBSERVED_AT)
    assert fetch_log == [2]  # attempted, over git only — and the ref is genuinely gone
    assert res.stop is None  # a gone ref is not a stop: it records suspicious rows
    assert res.suspicious > 0
    rows = pr_rows(conn, 2)
    assert rows and all(r[0] is None and r[7] == 1 for r in rows)
    eff = prsweep.effective_attribution(conn, commit_id(conn, squash))
    assert eff.verdict == "no-evidence"


# ---------------------------------------------------------------------------
# replace-not-duplicate + channel precedence (acceptance criterion 4)
# ---------------------------------------------------------------------------


def test_rerun_replaces_rows_without_duplicating(tmp_path, repo, fetch_log):
    start_remote(tmp_path, repo)
    run(repo, "checkout", "-b", "claude/feature")
    h1 = commit_files(repo, f"feat: core work\n\n{TRAILER_LINE}\n", {"b.txt": "b\n"})
    push_head_ref(repo, h1, 5)
    run(repo, "checkout", "main")
    run(repo, "branch", "-D", "claude/feature")
    commit_files(repo, "feat: the work (#5)", {"b.txt": "b\n"})
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    t = FakeTransport([pr(5, body="Generated with Codex.\n", head="claude/feature")])
    prsweep.sweep(conn, repo, "main", 0, transport=t, now=OBSERVED_AT)
    first = pr_rows(conn, 5)
    prsweep.sweep(conn, repo, "main", 0, transport=t, now=OBSERVED_AT)
    second = pr_rows(conn, 5)
    assert first == second  # replaced in place, never duplicated


def test_partially_recorded_pr_is_replaced_not_extended(tmp_path, repo, fetch_log):
    start_remote(tmp_path, repo)
    run(repo, "checkout", "-b", "claude/feature")
    h1 = commit_files(repo, f"feat: core work\n\n{TRAILER_LINE}\n", {"b.txt": "b\n"})
    push_head_ref(repo, h1, 5)
    run(repo, "checkout", "main")
    run(repo, "branch", "-D", "claude/feature")
    commit_files(repo, "feat: the work (#5)", {"b.txt": "b\n"})
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    # an interrupted earlier run left the PR half-recorded
    cid = commit_id(conn, gitwalk.list_commits(repo, "main")[-1])
    prsweep.replace_pr_rows(
        conn,
        5,
        [prsweep.PrRow("tool-trailer", "stale:row", "yes", cid, False, "claude", None)],
        "2026-10-29T00:00:00+00:00",
    )
    conn.commit()
    t = FakeTransport([pr(5, body="Generated with Codex.\n", head="claude/feature")])
    prsweep.sweep(conn, repo, "main", 0, transport=t, now=OBSERVED_AT)
    rows = pr_rows(conn, 5)
    assert ("tool-trailer", "stale:row") not in {(r[2], r[3]) for r in rows}
    assert len(rows) == len({(r[1], r[2], r[3], r[0]) for r in rows})  # no duplicates


def test_git_evidence_outranks_inherited_pr_evidence(tmp_path, repo):
    # the walk's git-only rows win the identity claim; PR rows only fill
    # what the git rows leave silent; suspicious rows never contribute
    commit_files(repo, "init", {"a.txt": "a\n"})
    sha = commit_files(repo, "feat: x\n\nAssisted-by: codex:gpt-5\n", {"b.txt": "b\n"})
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    cid = commit_id(conn, sha)
    prsweep.replace_pr_rows(
        conn,
        3,
        [
            prsweep.PrRow("tool-trailer", "claude-code:Opus", "yes", cid, False, "claude", None),
            prsweep.PrRow(
                "co-authored-by", "Ghost <g@anthropic.com>", "yes", cid, False, "claude", "Ghost"
            ),
            prsweep.PrRow("name-suffix", "evil[bot]", "yes", None, True, "evil", None),
        ],
        OBSERVED_AT,
    )
    conn.commit()
    eff = prsweep.effective_attribution(conn, cid)
    assert eff.harness == "codex"  # the git-only evidence outranks
    assert eff.model == "gpt-5"
    # audit rows keep their provenance, git first; suspicious rows excluded
    seen = prsweep.effective_evidence(conn, cid)
    assert [c for c, _, _, _ in seen] == ["git", "pr", "pr"]
    assert "evil[bot]" not in {raw for _, _, raw, _ in seen}


def test_pr_evidence_fills_where_git_evidence_is_silent(tmp_path, repo):
    commit_files(repo, "init", {"a.txt": "a\n"})
    sha = commit_files(repo, "feat: x", {"b.txt": "b\n"})
    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    cid = commit_id(conn, sha)
    prsweep.replace_pr_rows(
        conn,
        4,
        [prsweep.PrRow("co-authored-by", TRAILER_VALUE, "yes", cid, False, "claude", "Opus 4.8")],
        OBSERVED_AT,
    )
    conn.commit()
    eff = prsweep.effective_attribution(conn, cid)
    assert eff.verdict == "yes"
    assert eff.harness == "claude" and eff.model == "Opus 4.8"


# ---------------------------------------------------------------------------
# bound, base filter, closed-unmerged
# ---------------------------------------------------------------------------


def test_after_pr_bound_and_skipped_prs_never_fetch(tmp_path, repo, fetch_log):
    start_remote(tmp_path, repo)
    shas = {}
    for n in (1, 2, 3):
        run(repo, "checkout", "-b", f"claude/f{n}")
        h = commit_files(repo, f"feat: {n}\n\n{TRAILER_LINE}\n", {f"f{n}.txt": f"{n}\n"})
        push_head_ref(repo, h, n)
        run(repo, "checkout", "main")
        run(repo, "branch", "-D", f"claude/f{n}")
        shas[n] = commit_files(repo, f"feat: work {n} (#{n})", {f"f{n}.txt": f"{n}\n"})
    # a closed-unmerged PR and a PR merged into another branch
    run(repo, "checkout", "-b", "claude/g")
    h = commit_files(repo, f"feat: g\n\n{TRAILER_LINE}\n", {"g.txt": "g\n"})
    push_head_ref(repo, h, 8)
    run(repo, "checkout", "main")
    run(repo, "branch", "-D", "claude/g")

    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    t = FakeTransport(
        [
            pr(3, head="claude/f3"),
            pr(2, head="claude/f2"),
            pr(1, head="claude/f1"),
            pr(8, head="claude/g", merged=False),
            pr(4, head="claude/f3", base="develop"),
        ]
    )
    res = prsweep.sweep(conn, repo, "main", 2, transport=t, now=OBSERVED_AT)
    assert res.processed == 1
    assert fetch_log == [3]  # bound/below, unmerged, and other-base never fetched
    assert {r[1] for r in pr_rows(conn)} == {3}
    assert pr_rows(conn, 3) and all(r[0] == shas[3] for r in pr_rows(conn, 3))


def test_squash_plausibility_never_trusts_none_on_none(tmp_path, repo, fetch_log):
    # empty diffs carry no patch-id: None == None is NOT a parentage match.
    # The rows are genuinely unmapped — recorded unattached, NOT suspicious,
    # and never trusted onto the landing
    start_remote(tmp_path, repo)
    run(repo, "checkout", "-b", "claude/empty")
    run(repo, "commit", "--allow-empty", "-m", "feat: core\n\nGenerated with Codex.\n")
    h = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    push_head_ref(repo, h, 5)  # an empty-diff head
    run(repo, "checkout", "main")
    run(repo, "branch", "-D", "claude/empty")
    run(repo, "commit", "--allow-empty", "-m", "feat: work (#5)")  # an empty landing
    landing = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    conn = make_db(tmp_path / "w.db", repo)
    gitwalk.walk(repo, conn, "main")
    githubize(conn)
    t = FakeTransport(
        [
            pr(
                5,
                body="Generated with Codex.\n",
                head="claude/empty",
                merge_commit_sha=landing,
            )
        ]
    )
    res = prsweep.sweep(conn, repo, "main", 0, transport=t, now=OBSERVED_AT)
    assert res.stop is None and res.processed == 1
    assert res.suspicious == 0  # an empty diff is not evidence of tampering
    rows = pr_rows(conn, 5)
    assert rows and all(r[0] is None for r in rows)  # genuinely unmapped: unattached
    assert all(r[7] == 0 for r in rows)  # and NOT suspicious
    # no false trust: nothing is attached to any walked commit
    assert (
        conn.execute(
            "SELECT COUNT(*) c FROM pr_evidence WHERE commit_id IS NOT NULL"
        ).fetchone()["c"]
        == 0
    )


# ---------------------------------------------------------------------------
# an empty Target branch errors loudly — never silently skips every PR
# ---------------------------------------------------------------------------


def test_sweep_against_an_empty_branch_errors_loudly(tmp_path, repo):
    commit_files(repo, "init", {"a.txt": "a\n"})
    db = tmp_path / "cli.db"
    cli.main(["init", str(repo), "--db", str(db)])
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("UPDATE settings SET value = '\"\"' WHERE key = 'branch'")
    conn.commit()
    conn.close()
    with pytest.raises(SystemExit) as exc:
        cli.main(["pr-sweep", "--db", str(db)])
    assert "error" in str(exc.value) and "branch" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# CLI verb
# ---------------------------------------------------------------------------


def test_cli_pr_sweep_verb(tmp_path, repo, monkeypatch, capsys):
    start_remote(tmp_path, repo)
    run(repo, "checkout", "-b", "claude/feature")
    h1 = commit_files(repo, f"feat: core work\n\n{TRAILER_LINE}\n", {"b.txt": "b\n"})
    push_head_ref(repo, h1, 5)
    run(repo, "checkout", "main")
    run(repo, "branch", "-D", "claude/feature")
    commit_files(repo, "feat: the work (#5)", {"b.txt": "b\n"})

    db = tmp_path / "cli.db"
    cli.main(["init", str(repo), "--db", str(db)])
    cli.main(["walk", "--db", str(db)])
    import sqlite3

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    githubize(conn)
    conn.close()

    t = FakeTransport([pr(5, body="Generated with Codex.\n", head="claude/feature")])
    monkeypatch.setattr(prsweep, "default_transport", lambda url: t(url))
    cli.main(["pr-sweep", "--db", str(db), "--after-pr", "0"])
    out = capsys.readouterr().out
    assert "pr-sweep" in out and "1 PR" in out
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) c FROM pr_evidence").fetchone()["c"] == 3
    conn.close()


def test_cli_pr_sweep_requires_non_negative_bound(tmp_path, repo):
    commit_files(repo, "init", {"a.txt": "a\n"})
    db = tmp_path / "cli.db"
    cli.main(["init", str(repo), "--db", str(db)])
    with pytest.raises(SystemExit):
        cli.main(["pr-sweep", "--db", str(db), "--after-pr", "-1"])
