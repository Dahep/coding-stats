"""Channel-pr evidence: the merged-PR sweep of ticket #35 (ADR-0007 clauses 3-5).

The PR tier reconstructs attribution the Target-branch walk alone cannot
see — PR body footers, bot authors, and head branch names recovered from
PR head refs — and records it as evidence rows CHANNELED ``pr``, attached at
the landed Target-branch sha. The pipeline per sweep run from a lower bound:

1. **List** closed PRs with number > the bound via one ``GET
   /repos/{owner}/{repo}/pulls`` per 100 closed PRs (ascending processing
   order). Bodies, titles and authors come from the LIST response: ZERO
   per-PR API calls and no commit-list endpoints anywhere — a 10k-PR repo
   costs ~100 requests total. Closed-unmerged PRs are listed and skipped.
2. **Fetch** ``refs/pull/N/head`` over the git protocol (zero API budget),
   lazily — only for squash/rebase-merged PRs whose shas were rewritten on
   the Target branch. Plain-merge PRs are skipped entirely by the fetch:
   their commits are already in the repo at identical shas, so evidence is
   recorded once from their commits (merge^1..merge^2, locally), never
   twice. PRs merged into another base branch are skipped outright.
3. **Detect** on the head commits with #34's rule table (aiattr.matches)
   plus PR-level signals (aiattr.pr_matches: body footers, PR author bot
   identity, head branch name prefixes) and **map** onto the landed sha:
   subject ``(#N)`` for squash, MERGE_HEAD shape for plain merges,
   patch-id matching for rebase rewrites.
4. **Head-parentage check** before trusting a fetched ref: the squash
   landing must carry the head range's aggregate patch-id and every
   content-carrying rebase head commit must map to a walked twin. When the
   ref is not plausibly the PR's merge head (mapping fails, post-merge
   force-push, or the ref is gone) the PR's rows are recorded
   SUSPICIOUS-MARKED with no commit attached — audit only, never
   attribution.
5. **Record** rows — (PR number, rule id, raw matched string, observed_at,
   channel pr) plus the tier and the identity the raw names — at the mapped
   sha. Rows are RECORDED, never re-derived by recompute (they are not pure
   functions of the Target branch); a re-run replaces that PR's rows
   wholesale rather than duplicating them (``replace_pr_rows``).

Channel precedence (consumption): git-only evidence on the same commit
outranks inherited PR evidence. The storage encodes it — where the walk
already recorded the identical match at the same commit, the PR row is not
stored a second time ("recorded once, not twice"), and ``effective_*``
resolves identity claims git-first with suspicious rows excluded from every
resolution. No LLM calls anywhere: the PR tier is detection-only.
"""

import json
import os
import re
import sqlite3
import subprocess
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import aiattr, gitwalk, identity

CHANNEL_PR = "pr"
DEFAULT_PER_PAGE = 100

Transport = Callable[[str], Any]  # url -> parsed JSON body


class SweepError(RuntimeError):
    """The sweep cannot run against this repository (no GitHub identity)."""


@dataclass(frozen=True)
class PullRequest:
    """The PR-list fields the sweep reads (one list response row)."""

    number: int
    title: str
    body: str
    author_login: str | None
    author_is_bot: bool
    head_ref: str | None
    base_ref: str | None
    merged: bool


@dataclass(frozen=True)
class PrRow:
    """One recorded PR-channel evidence row (the ticket's row shape plus the
    tier and the identity the raw matched string names)."""

    rule: str
    raw: str
    verdict: str  # 'yes' | 'possible'
    commit_id: int | None  # mapped landed sha; None for suspicious rows
    suspicious: bool
    harness: str | None
    model: str | None


@dataclass(frozen=True)
class SweepResult:
    processed: int  # PRs whose rows were (re)recorded
    rows: int
    suspicious: int
    requests: int  # API requests spent (list pages only)


def default_transport(url: str) -> Any:
    """The stdlib GitHub API transport (zero runtime dependencies). Auth via
    GITHUB_TOKEN when present (``pull-requests: read`` is enough); unauthen‐
    cated works for public repos within the smaller rate budget."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "coding-stats",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_pr(item: dict[str, Any]) -> PullRequest:
    user = item.get("user") or {}
    head = item.get("head") or {}
    base = item.get("base") or {}
    login = user.get("login")
    return PullRequest(
        number=int(item["number"]),
        title=str(item.get("title") or ""),
        body=str(item.get("body") or ""),
        author_login=str(login) if login else None,
        author_is_bot=user.get("type") == "Bot",
        head_ref=str(head["ref"]) if head.get("ref") else None,
        base_ref=str(base["ref"]) if base.get("ref") else None,
        merged=bool(item.get("merged")),
    )


def list_merged_pulls(
    owner: str, repo: str, after: int, transport: Transport | None = None
) -> tuple[list[PullRequest], int]:
    """Closed PRs with number > ``after``, ascending, merged ones only
    (ticket #35 step 1). Cost: one GET per 100 closed PRs — the LIST response
    carries bodies/titles/authors, so there are ZERO per-PR API calls.
    Pages arrive newest-first and paging stops at the bound (the numbering
    sequence is total and append-only)."""
    call = transport or default_transport
    kept: dict[int, PullRequest] = {}
    requests = 0
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{owner}/{repo}/pulls"
            f"?state=closed&sort=created&direction=desc"
            f"&per_page={DEFAULT_PER_PAGE}&page={page}"
        )
        items = call(url)
        requests += 1
        if not isinstance(items, list) or not items:
            break
        numbers = []
        for item in items:
            parsed = _parse_pr(item)
            numbers.append(parsed.number)
            if parsed.number > after and parsed.merged:
                kept[parsed.number] = parsed
        if len(items) < DEFAULT_PER_PAGE or max(numbers) <= after:
            break
        page += 1
    return [kept[n] for n in sorted(kept)], requests


def fetch_head_ref(repo: Path, pr_number: int) -> str | None:
    """Fetch GitHub's ``refs/pull/N/head`` over the git protocol (zero API
    budget) and return its tip sha. None when the ref is gone or unfetchable
    — the sweep then records suspicious rows, never attribution. The ref is
    force-updated locally: a post-merge force-push shows up as content that
    no longer maps onto what landed."""
    refspec = f"+refs/pull/{pr_number}/head:refs/pull/{pr_number}/head"
    fetch = subprocess.run(
        ["git", "-C", str(repo), "fetch", "--no-tags", "-q", "origin", refspec],
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        return None
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/pull/{pr_number}/head"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    sha = out.stdout.strip()
    return sha or None


def replace_pr_rows(
    conn: sqlite3.Connection,
    pr_number: int,
    rows: Sequence[PrRow],
    observed_at: str,
) -> int:
    """Record one PR's evidence rows, REPLACING that PR's previous rows —
    the replace-not-duplicate rule of ticket #35: a partially-recorded PR is
    replaced wholesale (by PR number), never extended into duplicates.
    Recorded, never re-derived: no recompute ever rebuilds these rows.
    Returns the stored count."""
    repo_id = identity.repo_id_for(conn)
    conn.execute(
        "DELETE FROM pr_evidence WHERE repo_id = ? AND pr_number = ?", (repo_id, pr_number)
    )
    stored = 0
    seen: set[tuple[int | None, str, str]] = set()
    for r in rows:
        key = (r.commit_id, r.rule, r.raw)
        if key in seen:
            continue
        seen.add(key)
        conn.execute(
            "INSERT INTO pr_evidence (repo_id, pr_number, commit_id, rule, raw, verdict,"
            " harness, model, channel, observed_at, suspicious)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                repo_id,
                pr_number,
                r.commit_id,
                r.rule,
                r.raw,
                r.verdict,
                r.harness,
                r.model,
                CHANNEL_PR,
                observed_at,
                1 if r.suspicious else 0,
            ),
        )
        stored += 1
    return stored


def effective_evidence(conn: sqlite3.Connection, commit_id: int) -> list[tuple[str, str, str, str]]:
    """(channel, rule, raw, verdict) contributing rows of one commit, the
    git channel first (it outranks inherited PR evidence). Suspicious PR
    rows never contribute — they are audit records, not attribution."""
    out: list[tuple[str, str, str, str]] = []
    for channel, sql, args in (
        (
            "git",
            "SELECT rule, raw, verdict FROM ai_evidence WHERE commit_id = ?",
            (commit_id,),
        ),
        (
            CHANNEL_PR,
            "SELECT rule, raw, verdict FROM pr_evidence WHERE commit_id = ? AND suspicious = 0",
            (commit_id,),
        ),
    ):
        for r in conn.execute(sql, args):
            out.append((channel, str(r["rule"]), str(r["raw"]), str(r["verdict"])))
    out.sort(key=lambda t: (0 if t[0] == "git" else 1, aiattr.RULE_RANK.get(t[1], 99), t[2]))
    return out


def effective_attribution(conn: sqlite3.Connection, commit_id: int) -> aiattr.Attribution:
    """The channel-precedence resolution of one commit (the consumption
    side of "git-only evidence on the same commit outranks inherited PR
    evidence"):

    * the verdict is the strongest tier of the contributing rows — a
      signature matched anywhere (git commit text or a mapped head commit)
      is still a signature match;
    * the identity claims (harness/model) resolve git-first: the walk's own
      summary, then PR rows only where the git evidence is silent;
    * suspicious rows contribute nothing, ever.
    """
    evs = effective_evidence(conn, commit_id)
    rows = tuple(aiattr.Evidence(rule, raw, verdict) for _, rule, raw, verdict in evs)
    if any(e.verdict == aiattr.VERDICT_YES for e in rows):
        verdict = aiattr.VERDICT_YES
    elif rows:
        verdict = aiattr.VERDICT_POSSIBLE
    else:
        verdict = aiattr.VERDICT_NO_EVIDENCE
    harness: str | None = None
    model: str | None = None
    git = conn.execute(
        "SELECT harness, model FROM ai_attribution WHERE commit_id = ?", (commit_id,)
    ).fetchone()
    if git is not None:
        harness = str(git["harness"]) if git["harness"] is not None else None
        model = str(git["model"]) if git["model"] is not None else None
    if harness is None or model is None:
        prs = conn.execute(
            "SELECT rule, raw, harness, model FROM pr_evidence"
            " WHERE commit_id = ? AND suspicious = 0",
            (commit_id,),
        ).fetchall()
        prs.sort(key=lambda r: (aiattr.RULE_RANK.get(str(r["rule"]), 99), str(r["raw"])))
        for r in prs:
            if harness is None and r["harness"] is not None:
                harness = str(r["harness"])
            if model is None and r["model"] is not None:
                model = str(r["model"])
            if harness is not None and model is not None:
                break
    return aiattr.Attribution(verdict, harness, model, rows)


def sweep(
    conn: sqlite3.Connection,
    repo: Path,
    branch: str,
    after_pr: int,
    *,
    transport: Transport | None = None,
    now: str | None = None,
) -> SweepResult:
    """One manual channel-pr sweep run (ticket #35's numbered behavior).
    Run after the walk: the mapping reads the walked commits, and a PR whose
    landing is not walked yet is recorded suspicious and healed by a re-run
    (replace-per-PR) once the walk has caught up."""
    if after_pr < 0:
        raise SweepError(f"--after-pr must be >= 0 (got {after_pr})")
    owner, name = _github_repo(conn)
    pulls, requests = list_merged_pulls(owner, name, after_pr, transport)
    observed_at = now or datetime.now(UTC).isoformat(timespec="seconds")
    processed = rows_n = suspicious_n = 0
    for pr in pulls:
        if pr.base_ref != branch:
            continue  # merged into another branch: never part of this Target
        rows = _pr_rows(conn, repo, branch, pr)
        stored = replace_pr_rows(conn, pr.number, rows, observed_at)
        conn.commit()  # each PR's record lands atomically (watermark-ready)
        processed += 1
        rows_n += stored
        suspicious_n += sum(1 for r in rows if r.suspicious)
    return SweepResult(processed, rows_n, suspicious_n, requests)


def _github_repo(conn: sqlite3.Connection) -> tuple[str, str]:
    row = conn.execute("SELECT clone_url FROM repositories ORDER BY id LIMIT 1").fetchone()
    url = str(row["clone_url"]) if row is not None and row["clone_url"] else ""
    m = re.match(r"^https://github\.com/([^/]+)/(.+)$", url)
    if m is None:
        raise SweepError(
            "pr-sweep needs a GitHub origin: the repository's clone URL resolves to"
            f" {url or '(unknown)'} (init/walk resolve it from the origin URL)"
        )
    return m.group(1), m.group(2)


@dataclass(frozen=True)
class _Walked:
    """One walked commit the mapping can land evidence on."""

    commit_id: int
    sha: str
    title: str
    subject_patch_id: str | None
    walk_index: int


def _walked_by_title(conn: sqlite3.Connection, pattern: str) -> list[_Walked]:
    return [
        _Walked(
            int(r["id"]),
            str(r["sha"]),
            str(r["title"]),
            r["subject_patch_id"],
            int(r["walk_index"]),
        )
        for r in conn.execute(
            "SELECT id, sha, title, subject_patch_id, walk_index FROM commits"
            " WHERE title LIKE ? ORDER BY walk_index",
            (pattern,),
        )
    ]


def _locate_landing(conn: sqlite3.Connection, number: int) -> tuple[str, _Walked] | None:
    """('squash'|'merge', walked commit) when the walk already names the PR:
    a squash subject ``… (#N)`` (the first one — later twins are copies) or
    the MERGE_HEAD-shaped ``Merge pull request #N`` commit. None means the
    PR landed by rebase rewrite (no walked commit names it) — its mapping
    comes from patch-id matching. The subject mapping is only trusted after
    the head-parentage check below (an edited squash subject mapping the
    wrong commit is caught there)."""
    for w in _walked_by_title(conn, f"% (#{number})"):
        if re.search(rf"\(#{number}\)$", w.title):
            return "squash", w
    for w in _walked_by_title(conn, f"Merge pull request #{number} from %"):
        return "merge", w
    return None


def _pr_rows(conn: sqlite3.Connection, repo: Path, branch: str, pr: PullRequest) -> list[PrRow]:
    """Steps 2-5 for one PR: fetch lazily, detect on head commits + PR-level
    signals, check head parentage, map, and assemble the row set (deduped
    per (commit, rule, raw), git-recorded matches never doubled)."""
    signals = aiattr.pr_matches(pr.body or "", pr.author_login, pr.author_is_bot, pr.head_ref)
    landed = _locate_landing(conn, pr.number)
    if landed is not None:
        kind, walk = landed
        if kind == "merge":
            # plain merge: skipped entirely by the fetch — the identical
            # commits are already in the repo; record once from them
            hits = _detect_heads(
                repo, _merge_head_commits(repo, walk.sha), attach_all=walk.commit_id
            )
            return _assemble(conn, walk.commit_id, hits, signals, suspicious=False)
        tip = fetch_head_ref(repo, pr.number)
        if tip is None:
            return _assemble(conn, None, [], signals, suspicious=True)
        base = gitwalk.merge_base(repo, tip, branch)
        aggregate = gitwalk.patch_id_for_range(repo, base, tip) if base else None
        if not base or aggregate != walk.subject_patch_id:
            # not plausibly the PR's merge head (post-merge force-push or a
            # squash subject that named the wrong commit)
            return _assemble(
                conn,
                None,
                _detect_heads(repo, _safe_range(repo, base, tip)),
                signals,
                suspicious=True,
            )
        hits = _detect_heads(repo, gitwalk.list_range(repo, base, tip), attach_all=walk.commit_id)
        return _assemble(conn, walk.commit_id, hits, signals, suspicious=False)

    # rebase-merged (or an unrecognised landing): map the fetched head by
    # patch-id — the rebase twins keep each head commit's patch
    tip = fetch_head_ref(repo, pr.number)
    if tip is None:
        return _assemble(conn, None, [], signals, suspicious=True)
    base = gitwalk.merge_base(repo, tip, branch)
    if base is None:
        return _assemble(conn, None, [], signals, suspicious=True)
    head = gitwalk.list_range(repo, base, tip)
    twins: dict[str, _Walked] = {}
    mapping_failed = False
    for hc in head:
        pid = gitwalk.patch_id_for(repo, hc)
        if pid is None:
            continue  # empty-diff or merge head commit: nothing to map
        twin = _twin_by_patch_id(conn, pid)
        if twin is None:
            mapping_failed = True
        else:
            twins[hc] = twin
    if mapping_failed or not twins:
        return _assemble(
            conn, None, _detect_heads(repo, head), signals, suspicious=True
        )
    anchor = max(twins.values(), key=lambda w: w.walk_index).commit_id
    hits = _detect_heads(repo, head, attach={hc: w.commit_id for hc, w in twins.items()})
    return _assemble(conn, anchor, hits, signals, suspicious=False)


def _safe_range(repo: Path, base: str | None, tip: str) -> list[str]:
    return gitwalk.list_range(repo, base, tip) if base else []


def _merge_head_commits(repo: Path, merge_sha: str) -> list[str]:
    """The PR's commits of a plain merge: merge^1..merge^2 (identical shas,
    already local — never fetched)."""
    try:
        return gitwalk.list_range(repo, f"{merge_sha}^1", f"{merge_sha}^2")
    except subprocess.CalledProcessError:
        return []


def _twin_by_patch_id(conn: sqlite3.Connection, patch_id: str) -> _Walked | None:
    """The landed twin of one head commit (first by walk order when a patch
    was landed more than once — the original landing)."""
    row = conn.execute(
        "SELECT id, sha, title, subject_patch_id, walk_index FROM commits"
        " WHERE subject_patch_id = ? ORDER BY walk_index LIMIT 1",
        (patch_id,),
    ).fetchone()
    if row is None:
        return None
    return _Walked(
        int(row["id"]), str(row["sha"]), str(row["title"]), row["subject_patch_id"],
        int(row["walk_index"]),
    )


def _detect_heads(
    repo: Path,
    head_commits: Sequence[str],
    attach: dict[str, int] | None = None,
    attach_all: int | None = None,
) -> list[tuple[int | None, aiattr.Match]]:
    """Rule-table detection over head commits (#34's engine on the head
    commits' own facts), each match carried toward its mapped commit id."""
    hits: list[tuple[int | None, aiattr.Match]] = []
    for hc in head_commits:
        cid = (attach or {}).get(hc) if attach is not None else attach_all
        for m in aiattr.matches(gitwalk.commit_facts(repo, hc)):
            hits.append((cid, m))
    return hits


def _assemble(
    conn: sqlite3.Connection,
    anchor: int | None,
    head_hits: Sequence[tuple[int | None, aiattr.Match]],
    signals: Sequence[aiattr.Match],
    suspicious: bool,
) -> list[PrRow]:
    """The PR's row set: head matches at their mapped shas plus PR-level
    signals at the PR's landing, deduped per (commit, rule, raw) and — the
    storage side of channel precedence — never doubling a match the walk
    already recorded at the same commit ("recorded once, not twice").
    Suspicious rows keep no commit: recorded, marked, never attributed."""
    rows: list[PrRow] = []
    for cid, m in head_hits:
        rows.append(
            PrRow(
                m.rule,
                m.raw,
                m.verdict,
                None if suspicious else (cid if cid is not None else anchor),
                suspicious,
                m.harness,
                m.model,
            )
        )
    for m in signals:
        rows.append(
            PrRow(
                m.rule,
                m.raw,
                m.verdict,
                None if suspicious else anchor,
                suspicious,
                m.harness,
                m.model,
            )
        )
    out: list[PrRow] = []
    seen: set[tuple[int | None, str, str]] = set()
    for r in sorted(rows, key=lambda r: (aiattr.RULE_RANK.get(r.rule, 99), r.raw)):
        key = (r.commit_id, r.rule, r.raw)
        if key in seen:
            continue
        seen.add(key)
        if not suspicious and r.commit_id is not None and _git_recorded(
            conn, r.commit_id, r.rule, r.raw
        ):
            continue  # git-only evidence outranks: it is the record here
        out.append(r)
    return out


def _git_recorded(conn: sqlite3.Connection, commit_id: int, rule: str, raw: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM ai_evidence WHERE commit_id = ? AND rule = ? AND raw = ?",
            (commit_id, rule, raw),
        ).fetchone()
        is not None
    )
