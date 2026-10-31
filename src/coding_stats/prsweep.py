"""Channel-pr evidence: the merged-PR sweep of ticket #35 (ADR-0007 clauses 3-5)
with the watermark-driven per-increment machinery of ticket #36 (clause 2).

The PR tier reconstructs attribution the Target-branch walk alone cannot
see — PR body footers, bot authors, and head branch names recovered from
PR head refs — and records it as evidence rows CHANNELED ``pr``, attached at
the landed Target-branch sha. The pipeline per sweep run from a lower bound:

1. **List** PRs via one ``GET /repos/{owner}/{repo}/pulls?state=all`` per
   100 PRs (pages newest-number first). Bodies, titles and authors come
   from the LIST response: ZERO per-PR API calls and no commit-list
   endpoints anywhere — a 10k-PR repo costs ~100 requests total.
   Mergedness is the wire's ``merged_at`` timestamp-or-null (there is no
   ``merged`` boolean on the wire); merged ones are candidates, closed-
   unmerged and still-open PRs are listed and skipped (an open PR that
   merges later simply becomes a candidate then — its merge date is newer
   than the marker's).
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
   content-carrying rebase head commit must map to a walked twin. Only a
   genuinely-mapped-but-parentage-failed head (mapping fails, post-merge
   force-push) or a genuinely-unmappable one (the ref is gone) is recorded
   SUSPICIOUS-MARKED with no commit attached — audit only, never
   attribution. A PR whose landing is not in the walked set (merged after
   the last walk) is never recorded and never claimed: the run stops at it,
   waiting for the walk to catch up.
5. **Record** rows — (PR number, rule id, raw matched string, observed_at,
   channel pr) plus the tier and the identity the raw names — at the mapped
   sha. Rows are RECORDED, never re-derived by recompute (they are not pure
   functions of the Target branch); a re-run replaces that PR's rows
   wholesale rather than duplicating them (``replace_pr_rows``).

The run machinery (ticket #36, ADR-0007 clause 2) — keyed by MERGE DATE,
ties by PR number (the 2026-10-30 user ruling; ticket #36's letter keyed it
by PR number, which lost out-of-order merges — a still-open lower number
sat below the number watermark forever):

* **Resume marker** = the ``(merged_at, number)`` tuple of the last
  INCORPORATED PR, recorded in the Artifact next to the evidence rows and
  surviving partial recompute and gather exactly as they do
  (``pr_watermark``: ``last_merged_at`` + ``last_pr_number``, schema step
  7). The PR number stays the per-row key and the ``--after-pr`` bounded
  face's bound; only the RESUME point moved to merge dates — completeness
  over the number key's append-only convenience.
* **Oldest-first by merge date** (ascending ``(merged_at, number)``) over
  merged PRs whose tuple is strictly newer than the marker; the marker
  advances AFTER EACH PR's evidence is recorded (rows + marker land in one
  per-PR transaction, ``incorporate_pr``) — never per page. Every
  interruption leaves a contiguous processed prefix in merge-date order and
  the next run resumes at the first PR past the marker, skipping nothing
  and redoing at worst the single interrupted PR (its rows are replaced,
  never extended). A merge-date tie breaks by PR number ascending
  (deterministic); an equal date with a higher number counts as newer.
* **Window** (the pagination rule): the increment's LIST walk descends the
  pages to the END of the list and candidates are cut from the complete
  window by the tuple comparison. No date-based early stop is sound on
  number-descending pages: an older-numbered PR can merge later and sit in
  a page below any date cutoff — exactly the out-of-order case the
  merge-date key repairs. One request per 100 PRs; the ``--after-pr``
  bounded faces keep their NUMBER bound as the walk stop (their bound keys
  on ``last_pr_number``).
* Closed-unmerged PRs and PRs merged into another base branch are never
  incorporated and never advance the marker: they can never become
  candidates (a closed-unmerged PR never merges; an other-base PR is not
  part of this Target).
* **Stop conditions** end a run cleanly with a visible notice — never a
  failure exit: genuine rate-limit exhaustion (``RateLimited`` — 429, or a
  403 with the rate budget at zero; a permissions 403 is a distinct stop
  naming ``pull-requests: read``, never "rate limit exhausted"), the per-run
  processing cap, an interruption, a PR whose landing is not walked yet
  (``WalkStale`` — waiting for the walk to catch up), or a transient git
  fetch failure (``FetchUnavailable``). The next run resumes automatically.
* **Full-history sweep** (``sweep_full``, explicit operator invocation):
  the same merge-date walk sharing the marker, merely walked further — its
  listing walk continues past the number bound until the beginning of the
  PR numbering (or a stop condition), re-sweeping the full history (the
  marker never regresses while history below it is re-recorded); later push
  runs continue after the shared marker. A marker with a NULL merge date (a
  migrated step-6 row) claims no date: every merged PR is a candidate and
  the increment re-examines the history oldest-first.

Channel precedence (consumption): git-only evidence on the same commit
outranks inherited PR evidence. The storage encodes it — where the walk
already recorded the identical match at the same commit, the PR row is not
stored a second time ("recorded once, not twice"), and ``aiattr.effective_*``
resolves identity claims git-first with suspicious rows excluded from every
resolution. No LLM calls anywhere: the PR tier is detection-only.
"""

import json
import os
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import aiattr, gitwalk, identity

# shared resolution vocabulary, owned by the aiattr leaf and re-exported
# here for prsweep's own callers (back-compat)
from .aiattr import CHANNEL_GIT as CHANNEL_GIT
from .aiattr import CHANNEL_PR as CHANNEL_PR
from .aiattr import DISCLOSURE_LINE as DISCLOSURE_LINE
from .aiattr import effective_attribution as effective_attribution
from .aiattr import effective_evidence as effective_evidence

DEFAULT_PER_PAGE = 100
DEFAULT_RUN_CAP = 25  # increment runs ("a few PRs per push run")

STOP_CAP = "cap"
STOP_RATE_LIMIT = "rate-limit"
STOP_INTERRUPTED = "interrupted"
STOP_WALK_STALE = "walk-stale"
STOP_FETCH = "fetch-failed"
STOP_FORBIDDEN = "forbidden"

REQUEST_TIMEOUT = 30  # seconds: a hung API never hangs the sweep

Transport = Callable[[str], Any]  # url -> parsed JSON body


class SweepError(RuntimeError):
    """The sweep cannot run against this repository (no GitHub identity)."""


class RateLimited(RuntimeError):
    """The GitHub API rate budget is exhausted: a stop condition, not a
    failure — the run ends cleanly and the next one resumes."""


class WalkStale(RuntimeError):
    """A merged PR's landing is not in the walked set (it merged after the
    last walk): a resumable stop — the run stops AT that PR, recording
    nothing and never claiming past it, and the next run (after a newer
    walk) redoes it (replace-per-PR)."""


@dataclass(frozen=True)
class PullRequest:
    """The PR-list fields the sweep reads (one list response row — GitHub's
    Pull Request Simple schema). ``merged`` is PARSED from the wire's
    ``merged_at`` timestamp-or-null (the wire carries no ``merged`` boolean);
    the normalized ``merged_at`` is the resume marker's merge date."""

    number: int
    title: str
    body: str
    author_login: str | None
    author_is_bot: bool
    head_ref: str | None
    base_ref: str | None
    merged_at: str | None  # normalized ISO UTC; None = never merged
    landing_sha: str | None  # the wire's merge_commit_sha (walk-coverage check)

    @property
    def merged(self) -> bool:
        return self.merged_at is not None


@dataclass(frozen=True)
class PrRow:
    """One recorded PR-channel evidence row (the ticket's row shape plus the
    tier and the identity the raw matched string names)."""

    rule: str
    raw: str
    verdict: str  # 'yes' | 'possible'
    commit_id: int | None  # mapped landed sha; None when unattached (suspicious or unmapped)
    suspicious: bool
    harness: str | None
    model: str | None


@dataclass(frozen=True)
class SweepResult:
    processed: int  # PRs whose rows were (re)recorded
    rows: int
    suspicious: int
    requests: int  # API requests spent (list pages only)
    watermark: int = 0  # the marker's PR number after the run (audit + display)
    stop: str | None = None  # None = ran to its end; else a stop condition


def _http_stop(exc: urllib.error.HTTPError) -> str | None:
    """Classify an API HTTP failure into a stop condition — one classifier for
    403 AND 429 on every surface. Rate-limit exhaustion ONLY when it
    genuinely is one (429 is throttling by definition, or a 403 carrying
    ``X-RateLimit-Remaining: 0``); a permissions 403/401 ("Resource not
    accessible by integration") is a distinct stop naming the permission,
    never laundered into "rate limit exhausted". None = re-raise as a
    failure."""
    if exc.code == 429 or (
        exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0"
    ):
        return STOP_RATE_LIMIT
    if exc.code in (401, 403):
        return STOP_FORBIDDEN
    return None


def _stop_reason(exc: BaseException) -> str | None:
    """ONE classifier for the stop-condition triple the listing and
    processing phases share: genuine rate-limit exhaustion (``RateLimited``,
    or an HTTP failure ``_http_stop`` classifies as one), a distinct
    forbidden stop, or an interruption. None = not a stop condition — the
    caller re-raises (an unclassified HTTP failure is a failure, never a
    clean stop)."""
    if isinstance(exc, urllib.error.HTTPError):
        return _http_stop(exc)
    return STOP_RATE_LIMIT if isinstance(exc, RateLimited) else STOP_INTERRUPTED


def default_transport(url: str) -> Any:
    """The stdlib GitHub API transport (zero runtime dependencies). Auth via
    GITHUB_TOKEN when present (``pull-requests: read`` is enough); unauthen‐
    cated works for public repos within the smaller rate budget. GENUINE
    rate-limit responses raise ``RateLimited`` — the run's clean-stop signal,
    never a failure exit; a permissions 403 is re-raised (the sweep turns it
    into the forbidden stop). Every request carries a timeout."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "coding-stats",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if _http_stop(exc) == STOP_RATE_LIMIT:
            raise RateLimited(f"GitHub API rate limit exhausted (HTTP {exc.code})") from exc
        raise


def _norm_ts(value: str) -> str:
    """Wire timestamp -> normalized ISO UTC (second precision): the resume
    marker's comparable form — lexicographic order equals chronological, so
    the tuple comparison is a plain string comparison."""
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(UTC)
        .isoformat(timespec="seconds")
    )


def _parse_pr(item: dict[str, Any]) -> PullRequest:
    """One LIST row -> the sweep's fields. The wire format is GitHub's Pull
    Request Simple schema: mergedness is the ``merged_at`` timestamp-or-null
    (there is NO ``merged`` boolean on the wire — ``merged: true`` in docs and
    ticket bodies is the intent, not the wire format), the landing sha is
    ``merge_commit_sha``, and ``merged_at`` doubles as the resume marker's
    merge date."""
    user = item.get("user") or {}
    head = item.get("head") or {}
    base = item.get("base") or {}
    login = user.get("login")
    raw_merged_at = item.get("merged_at")
    return PullRequest(
        number=int(item["number"]),
        title=str(item.get("title") or ""),
        body=str(item.get("body") or ""),
        author_login=str(login) if login else None,
        author_is_bot=user.get("type") == "Bot",
        head_ref=str(head["ref"]) if head.get("ref") else None,
        base_ref=str(base["ref"]) if base.get("ref") else None,
        merged_at=_norm_ts(str(raw_merged_at)) if raw_merged_at else None,
        landing_sha=str(item["merge_commit_sha"]) if item.get("merge_commit_sha") else None,
    )


@dataclass(frozen=True)
class Listing:
    """One LIST walk's result: the merged PRs kept above the number bound
    (bound 0 = the increment's complete window), ordered oldest-first by
    (merged_at, number)."""

    merged: list[PullRequest]
    requests: int  # API requests spent (list pages only)


def list_pulls(
    owner: str, repo: str, after: int, transport: Transport | None = None
) -> Listing:
    """The LIST query (ticket #35 step 1): ``state=all``, one GET per 100
    PRs. Bodies, titles and authors come from the LIST response: ZERO per-PR
    API calls and no commit-list endpoints anywhere — a 10k-PR repo costs
    ~100 requests total. Merged PRs with number > ``after`` are kept
    (mergedness = the wire's ``merged_at``); closed-unmerged and still-open
    PRs are listed and skipped. The kept rows come back oldest-first by
    (merged_at, number) — the processing order of the merge-date key.

    ``after`` is the NUMBER bound of the ``--after-pr`` bounded faces, and
    the walk stops at it (the numbering sequence is total and append-only).
    The increment face passes 0: its window is COMPLETE (the walk descends
    to the end of the list) because no date-based stop is sound — an
    older-numbered PR can merge later and sit in a page below any date
    cutoff (the out-of-order case the merge-date key repairs)."""
    call = transport or default_transport
    kept: dict[int, PullRequest] = {}
    requests = 0
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{owner}/{repo}/pulls"
            f"?state=all&sort=created&direction=desc"
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
    return Listing(sorted(kept.values(), key=lambda p: (p.merged_at or "", p.number)), requests)


class FetchUnavailable(RuntimeError):
    """The git fetch of a PR head ref failed TRANSIENTLY (network, auth,
    remote config): a resumable stop — never recorded as a vanished ref."""


def fetch_head_ref(repo: Path, pr_number: int) -> str | None:
    """Fetch GitHub's ``refs/pull/N/head`` over the git protocol (zero API
    budget) and return its tip sha. None ONLY when the ref is genuinely gone
    (GitHub answers "couldn't find remote ref" — abandoned heads,
    force-pushed-then-deleted): the sweep then records suspicious rows, never
    attribution. Transient failures (unreachable remote, auth, config) raise
    ``FetchUnavailable`` — the run stops cleanly instead; a transient error
    is never recorded as suspicious. The ref is force-updated locally: a
    post-merge force-push shows up as content that no longer maps onto what
    landed. Never interactive: ``GIT_TERMINAL_PROMPT=0`` and a closed stdin,
    so a fetch can never hang asking for credentials."""
    refspec = f"+refs/pull/{pr_number}/head:refs/pull/{pr_number}/head"
    fetch = subprocess.run(
        ["git", "-C", str(repo), "fetch", "--no-tags", "-q", "origin", refspec],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if fetch.returncode != 0:
        if "couldn't find remote ref" in fetch.stderr:
            return None  # genuinely gone
        raise FetchUnavailable(
            f"git fetch refs/pull/{pr_number}/head failed:"
            f" {fetch.stderr.strip() or fetch.returncode}"
        )
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


def watermark(conn: sqlite3.Connection) -> int:
    """The Watermark's PR number: the number of the last incorporated PR (0
    when nothing is incorporated yet) — audit trail plus the ``--after-pr``
    bounded face's contiguity check key on it. Recorded state — it rides the
    Artifact next to the evidence rows and survives partial recompute and
    gather unchanged. (Resume itself keys on the merge date, ``resume_key``.)"""
    row = conn.execute(
        "SELECT last_pr_number FROM pr_watermark WHERE repo_id = ?",
        (identity.repo_id_for(conn),),
    ).fetchone()
    return int(row["last_pr_number"]) if row is not None else 0


def resume_key(conn: sqlite3.Connection) -> tuple[str | None, int]:
    """The resume marker: the (merged_at, number) tuple of the last
    INCORPORATED PR. A merged PR is a candidate exactly when its own tuple
    is strictly newer — merge date first, PR number as the deterministic
    tie break. A NULL merge date (a migrated step-6 marker) claims no date
    and sorts below every real one: such a marker re-examines every merged
    PR (completeness over the number key's convenience)."""
    row = conn.execute(
        "SELECT last_pr_number, last_merged_at FROM pr_watermark WHERE repo_id = ?",
        (identity.repo_id_for(conn),),
    ).fetchone()
    if row is None:
        return None, 0
    merged_at = row["last_merged_at"]
    return (str(merged_at) if merged_at is not None else None), int(row["last_pr_number"])


def _is_newer(merged_at: str | None, number: int, marker: tuple[str | None, int]) -> bool:
    """Whether (merged_at, number) is strictly newer than the marker tuple
    (a NULL date sorts below every real one)."""
    return (merged_at or "", number) > (marker[0] or "", marker[1])


def advance_watermark(
    conn: sqlite3.Connection, merged_at: str | None, pr_number: int
) -> None:
    """Move the resume marker to ``(merged_at, pr_number)`` — the two
    columns describe ONE PR, the last incorporated one — monotonically in
    tuple order (never backwards): a full-history sweep re-recording history
    below the marker never regresses the resume point."""
    if not _is_newer(merged_at, pr_number, resume_key(conn)):
        return
    conn.execute(
        "INSERT INTO pr_watermark (repo_id, last_pr_number, last_merged_at) VALUES (?, ?, ?)"
        " ON CONFLICT(repo_id) DO UPDATE"
        " SET last_pr_number = excluded.last_pr_number,"
        " last_merged_at = excluded.last_merged_at",
        (identity.repo_id_for(conn), pr_number, merged_at),
    )


def incorporate_pr(
    conn: sqlite3.Connection,
    repo: Path,
    branch: str,
    pr: PullRequest,
    *,
    now: str | None = None,
    advance: bool = False,
) -> tuple[int, int]:
    """The per-PR step of the run machinery (ticket #36): record one PR's
    evidence rows (replace-not-duplicate) and — when the run may claim
    contiguous coverage — advance the resume marker to this PR's own
    (merged_at, number) tuple, BOTH in one transaction. Interruption at any
    point leaves the previously incorporated prefix intact; the interrupted
    PR is at worst redone (replaced) by the next run. Returns (stored rows,
    suspicious)."""
    observed_at = now or datetime.now(UTC).isoformat(timespec="seconds")
    rows = _pr_rows(conn, repo, branch, pr)
    stored = replace_pr_rows(conn, pr.number, rows, observed_at)
    if advance:
        advance_watermark(conn, pr.merged_at, pr.number)
    conn.commit()  # rows + marker land atomically, per PR (never per page)
    return stored, sum(1 for r in rows if r.suspicious)


def _run(
    conn: sqlite3.Connection,
    repo: Path,
    branch: str,
    *,
    after_pr: int,
    resume: bool,
    transport: Transport | None,
    now: str | None,
    max_prs: int | None,
) -> SweepResult:
    """The shared run machinery. ``resume=True`` (the increment face): a
    complete listing window (the walk descends to the end of the list — no
    date-based early stop is sound), candidates = merged PRs whose
    (merged_at, number) tuple is newer than the resume marker, processed
    oldest-first by merge date (ties by PR number); the marker always
    advances. ``resume=False`` (the bounded face): candidates = merged PRs
    with number > ``after_pr``, the walk stops at the number bound, and the
    marker advances only while the run claims CONTIGUOUS coverage — a bound
    above the watermark number leaves the range in between unexamined, so
    the marker never claims past a gap ("zero skipped PRs" stays true and a
    later increment walks the gap). ``max_prs`` caps the run (a stop
    condition, not a failure). A rate-limit or interruption during the
    LISTING stops the run before anything is processed — a partial listing
    would leave unseen PRs below the claimed marker, so its PRs are never
    walked."""
    if not branch:
        # an empty Target branch would silently skip every PR through the
        # base-ref mismatch — error loudly instead
        raise SweepError(
            "pr-sweep needs a Target branch: none configured (settings.branch"
            " is empty — run init --branch <target>)"
        )
    owner, name = _github_repo(conn)
    pulls: list[PullRequest] = []
    requests = 0
    stop: str | None = None
    try:
        listing = list_pulls(owner, name, 0 if resume else after_pr, transport)
        pulls, requests = listing.merged, listing.requests
    except (RateLimited, urllib.error.HTTPError, KeyboardInterrupt) as exc:
        stop = _stop_reason(exc)  # incomplete listing: nothing may be processed
        if stop is None:
            raise  # an unclassified HTTP failure: a failure, never a clean stop
    if resume:
        marker = resume_key(conn)
        pulls = [p for p in pulls if _is_newer(p.merged_at, p.number, marker)]
        can_advance = True
    else:
        can_advance = after_pr <= watermark(conn)
    processed = rows_n = suspicious_n = 0
    if stop is None:
        try:
            for pulled in pulls:
                if pulled.base_ref != branch:
                    continue  # merged into another branch: never part of this Target
                if max_prs is not None and processed >= max_prs:
                    stop = STOP_CAP
                    break
                stored, susp = incorporate_pr(
                    conn, repo, branch, pulled, now=now, advance=can_advance
                )
                processed += 1
                rows_n += stored
                suspicious_n += susp
        except WalkStale:
            stop = STOP_WALK_STALE  # at that PR: waiting for the walk to catch up
        except FetchUnavailable:
            stop = STOP_FETCH  # transient git failure: retried by the next run
        except (RateLimited, urllib.error.HTTPError, KeyboardInterrupt) as exc:
            stop = _stop_reason(exc)
            if stop is None:
                raise
    return SweepResult(processed, rows_n, suspicious_n, requests, watermark(conn), stop)


def sweep(
    conn: sqlite3.Connection,
    repo: Path,
    branch: str,
    after_pr: int,
    *,
    transport: Transport | None = None,
    now: str | None = None,
    max_prs: int | None = None,
) -> SweepResult:
    """One channel-pr sweep run over merged PRs with number > ``after_pr``
    — the ``--after-pr`` bounded face (``sweep_full`` is this at bound 0),
    keyed by PR NUMBER: its bound is a number and the walk stops at it.
    Run after the walk: the mapping reads the walked commits, and a PR whose
    landing is not walked yet stops the run AT that PR (``STOP_WALK_STALE`` —
    waiting for the walk): nothing is recorded for it, the marker never
    claims past it, and the next run after a newer walk redoes it
    (replace-per-PR)."""
    if after_pr < 0:
        raise SweepError(f"--after-pr must be >= 0 (got {after_pr})")
    return _run(
        conn, repo, branch, after_pr=after_pr, resume=False,
        transport=transport, now=now, max_prs=max_prs,
    )


def sweep_increment(
    conn: sqlite3.Connection,
    repo: Path,
    branch: str,
    *,
    transport: Transport | None = None,
    now: str | None = None,
    max_prs: int | None = DEFAULT_RUN_CAP,
) -> SweepResult:
    """The watermark-driven per-increment run (the push-run face): merged
    PRs whose (merged_at, number) tuple is newer than the resume marker,
    oldest first by merge date (ties by PR number), capped per run. The next
    run — push run or full-history sweep — resumes automatically past
    wherever this one's marker stopped."""
    return _run(
        conn, repo, branch, after_pr=0, resume=True,
        transport=transport, now=now, max_prs=max_prs,
    )


def sweep_full(
    conn: sqlite3.Connection,
    repo: Path,
    branch: str,
    *,
    transport: Transport | None = None,
    now: str | None = None,
    max_prs: int | None = None,
) -> SweepResult:
    """The full-history sweep (explicit operator invocation): the same
    merge-date walk sharing the resume marker, merely walked further — its
    listing continues past the number bound until the beginning of the PR
    numbering (or a stop condition), so the whole history is re-swept
    (replace-not-duplicate keeps it clean, and the marker never regresses
    while history below it is re-recorded). Later push runs continue after
    the shared marker."""
    return _run(
        conn, repo, branch, after_pr=0, resume=False,
        transport=transport, now=now, max_prs=max_prs,
    )


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


def _walked_row(r: sqlite3.Row) -> _Walked:
    """One ``_Walked`` from a commits row — the shared row shape of the
    subject and patch-id lookups."""
    return _Walked(
        int(r["id"]), str(r["sha"]), str(r["title"]), r["subject_patch_id"], int(r["walk_index"])
    )


def _walked_by_title(conn: sqlite3.Connection, pattern: str) -> list[_Walked]:
    return [
        _walked_row(r)
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


def _walked_sha(conn: sqlite3.Connection, sha: str) -> bool:
    """Whether the walk already carries this landed sha (its landing is in
    the walked set)."""
    return (
        conn.execute("SELECT 1 FROM commits WHERE sha = ?", (sha,)).fetchone() is not None
    )


def _contentless_landing(repo: Path, sha: str) -> bool:
    """True when the repo holds this landing but the walk can NEVER record
    it: its diff strips to nothing (empty or store-only), so walk() skips it
    entirely. Such a landing is genuinely contentless — the mapping resolves
    it as unmapped instead of waiting for a walk that will never carry it."""
    try:
        diff = gitwalk.diff_for(repo, sha)
    except subprocess.CalledProcessError:
        return False  # the repo does not know this sha: the walk is behind
    return not gitwalk.prepare_diff(diff).diff.strip()


def _pr_rows(conn: sqlite3.Connection, repo: Path, branch: str, pr: PullRequest) -> list[PrRow]:
    """Steps 2-5 for one PR: fetch lazily, detect on head commits + PR-level
    signals, check head parentage, map, and assemble the row set (deduped
    per (commit, rule, raw), git-recorded matches never doubled).

    A PR whose landing is NOT in the walked set raises ``WalkStale`` instead
    of producing rows: the run stops at it (waiting for the walk), never
    records suspicious rows for it and never claims past it — the evidence
    is not lost, the next run after a newer walk redoes it."""
    signals = aiattr.pr_matches(pr.body or "", pr.author_login, pr.author_is_bot, pr.head_ref)
    landed = _locate_landing(conn, pr.number)
    if (
        landed is None
        and pr.landing_sha is not None
        and not _walked_sha(conn, pr.landing_sha)
        and not _contentless_landing(repo, pr.landing_sha)
    ):
        raise WalkStale(
            f"PR {pr.number}: its landing {pr.landing_sha[:12]} is not in the walked set"
            " (merged after the last walk) — waiting for the walk to catch up"
        )
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
        if not base or (
            walk.subject_patch_id is not None
            and aggregate is not None
            and aggregate != walk.subject_patch_id
        ):
            # not plausibly the PR's merge head (post-merge force-push or a
            # squash subject that named the wrong commit)
            return _assemble(
                conn,
                None,
                _detect_heads(repo, _safe_range(repo, base, tip)),
                signals,
                suspicious=True,
            )
        if aggregate is None or walk.subject_patch_id is None:
            # empty diffs carry no patch-id: None on None is NOT a match —
            # no false trust. Genuinely unmapped per the squash strategy, but
            # not suspicious: recorded unattached, never attributed
            return _assemble(
                conn,
                None,
                _detect_heads(repo, _safe_range(repo, base, tip)),
                signals,
                suspicious=False,
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
    has_content = False
    for hc in head:
        pid = gitwalk.patch_id_for(repo, hc)
        if pid is None:
            continue  # empty-diff or merge head commit: nothing to map
        has_content = True
        twin = _twin_by_patch_id(conn, pid)
        if twin is None:
            mapping_failed = True
        else:
            twins[hc] = twin
    if not twins:
        if not has_content:
            # the head carries no patchable content (empty diffs): nothing to
            # map — genuinely unmapped, not suspicious, never a false trust
            return _assemble(
                conn, None, _detect_heads(repo, head), signals, suspicious=False
            )
        if pr.landing_sha is not None:
            # the landing is walked (checked above) yet nothing maps: a
            # genuinely unmappable head (post-merge force-push) — suspicious
            return _assemble(
                conn, None, _detect_heads(repo, head), signals, suspicious=True
            )
        # nothing names the PR and nothing maps: its landing cannot be shown
        # to be walked — wait for the walk rather than lose the evidence
        raise WalkStale(
            f"PR {pr.number}: no walked commit maps its head (its landing is"
            " not in the walked set) — waiting for the walk to catch up"
        )
    if mapping_failed:
        # some head commits map but not all: mapped-but-parentage-failed
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
    return _walked_row(row)


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
