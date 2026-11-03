"""Channel-pr evidence: the merged-PR sweep of ticket #35 (ADR-0007 clauses 3-5)
with the watermark-driven per-increment machinery of ticket #36 (clause 2).

Canonical design: ADR-0007 (``docs/adr/0007-pr-channel-attribution.md``) and
the PR-channel vocabulary in ``CONTEXT.md`` — this docstring orients, it does
not restate the clauses. The PR tier reconstructs attribution the Target-branch
walk cannot see (PR body footers, bot authors, head branch names) as evidence
rows CHANNELED ``pr``, attached at the landed Target-branch sha. Per sweep run
from a lower bound: list PRs through the source layer (``prsource`` — one GET
per 100 PRs, ZERO per-PR API calls), fetch ``refs/pull/N/head`` over git only
for squash/rebase-merged PRs, detect with ``aiattr``'s rule table plus
PR-level signals, map onto the landed sha (squash ``(#N)`` subject, MERGE_HEAD
shape, patch-id twins — head parentage checked before trust), and record the
rows at that sha.

Run machinery (clause 2): resume marker = the last incorporated PR's
``(merged_at, number)`` tuple in ``pr_watermark`` (merge-date keyed, PR-number
tie break — the 2026-10-30 ruling); candidates run oldest-first by merge date
with the marker advancing per PR in one transaction (``incorporate_pr``), and
the listing window always descends to the end of the list — no date-based
early stop is sound on number-descending pages. Rows are RECORDED, never
re-derived; a re-run replaces a PR's rows wholesale (``replace_pr_rows``), and
a PR whose landing is not walked yet stops the run AT it (``WalkStale``),
waiting for the walk to catch up. Stop conditions — this layer's run-policy
labels (``STOP_CAP``/``STOP_WALK_STALE``/``STOP_FETCH``/``STOP_UNAVAILABLE``)
plus ``prsource``'s HTTP-classified stops — end a run cleanly with a visible
notice, never a failure exit.
Channel precedence is encoded in storage — git-only evidence outranks
inherited PR evidence (``aiattr.resolve`` resolves git-first, suspicious
rows never attribute). No LLM calls anywhere: the PR tier is detection-only.
"""

import os
import re
import sqlite3
import subprocess
import urllib.error
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import aiattr, gitwalk, identity, prsource
from .aiattr import CHANNEL_PR
from .prsource import PullRequest, RateLimited, Transport, _stop_reason

DEFAULT_RUN_CAP = 25  # increment runs ("a few PRs per push run")
DEFAULT_BACKFILL_CAP = 250  # the CI backfill face's per-invocation bound (UPDATE's
# `--pr-backfill full`): one disjoint chunk of oldest-unincorporated history per
# dispatch — a CI run always ends cleanly at the cap and lands that chunk instead
# of dying to a timeout mid-history; the next identical dispatch continues past it

# THIS layer's run-policy stop labels (the cap, the walk-stale wait, the
# transient fetch failure, an operational unavailability) — assigned by the
# run machinery below. prsource keeps the HTTP-classified stops
# (rate-limit, interrupted, forbidden); every label is imported from its
# owner, never re-exported across the two layers.
STOP_CAP = "cap"
STOP_WALK_STALE = "walk-stale"
STOP_FETCH = "fetch-failed"
STOP_UNAVAILABLE = "unavailable"

# the operator's skip mark: ONE audit-only evidence row per skipped PR —
# suspicious like every audit row, never attribution (prsweep-owned, never
# matched by the detection rule table)
RULE_OPERATOR_SKIP = "operator-skip"
OPERATOR_SKIP_RAW = "operator skip"  # the row's raw when no reason is given


class SweepError(RuntimeError):
    """The sweep cannot run against this repository (no GitHub identity)."""


class WalkStale(RuntimeError):
    """A merged PR's landing is not in the walked set (it merged after the
    last walk): a resumable stop — the run stops AT that PR, recording
    nothing and never claiming past it, and the next run (after a newer
    walk) redoes it (replace-per-PR)."""


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
    detail: str | None = None  # failure detail when a stop carries one (STOP_UNAVAILABLE)


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
    Row uniqueness is owned by the producer: ``_assemble`` already dedupes
    per (commit, rule, raw); storage stays a dumb sink guarded by the
    table's UNIQUE constraint. Returns the stored count."""
    repo_id = identity.repo_id_for(conn)
    conn.execute(
        "DELETE FROM pr_evidence WHERE repo_id = ? AND pr_number = ?", (repo_id, pr_number)
    )
    stored = 0
    for r in rows:
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
    when nothing is incorporated yet) — audit trail over the marker the
    resume keys on (a positive ``--after-pr`` bound never moves it).
    Recorded state — it rides the
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


def advance_watermark(conn: sqlite3.Connection, merged_at: str | None, pr_number: int) -> None:
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


def record_disclosure(conn: sqlite3.Connection) -> None:
    """Record the PR-channel disclosure marker (CONTEXT.md "PR-channel
    disclosure marker"): ONE row per repository, a row meaning the PR tier
    has RUN against it — nothing else. Recorded state exactly like the
    evidence rows and the watermark: it rides the Artifact, survives partial
    recompute and gather unchanged, and is never removed. Idempotent.

    Every sweep face records it BEFORE listing any PR (after config
    validation): a run that finds zero non-suspicious rows — or dies
    operationally right after — still leaves disclosure on, because reports
    must never lose the fact that GitHub-sourced evidence was consulted. No
    sweep face, no marker: a config error (``SweepError``) raises before
    this and records nothing."""
    with conn:  # the marker lands as one explicit transaction
        conn.execute(
            "INSERT OR IGNORE INTO pr_disclosure (repo_id) VALUES (?)",
            (identity.repo_id_for(conn),),
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
    (merged_at, number) tuple, BOTH in one explicit transaction (``with
    conn:``: commit together, roll back together). Interruption at any
    point leaves the previously incorporated prefix intact; the interrupted
    PR is at worst redone (replaced) by the next run. The git/API work
    (``_pr_rows``) stays OUTSIDE the write transaction: only the row
    replacement and the marker advance are written atomically, so a failure
    after the deletion rolls the PR's previous rows and the marker back —
    never partial state for a later write to pick up. Returns (stored rows,
    suspicious)."""
    observed_at = now or datetime.now(UTC).isoformat(timespec="seconds")
    rows = _pr_rows(conn, repo, branch, pr)  # git/API work: outside the write transaction
    with conn:  # rows + marker land atomically, per PR (never per page)
        stored = replace_pr_rows(conn, pr.number, rows, observed_at)
        if advance:
            advance_watermark(conn, pr.merged_at, pr.number)
    return stored, sum(1 for r in rows if r.suspicious)


def _repo_id(conn: sqlite3.Connection) -> int:
    """This DB's repository id — the operator commands' config errors come
    back as ``SweepError`` (their CLI face exits on that vocabulary alone)."""
    try:
        return identity.repo_id_for(conn)
    except RuntimeError as exc:
        raise SweepError(str(exc)) from exc


def skip_pr(
    conn: sqlite3.Connection,
    pr_number: int,
    reason: str | None = None,
    *,
    now: str | None = None,
) -> None:
    """Mark PR ``pr_number`` to be SKIPPED by the sweep — the operator
    override for a permanently unprocessable PR (unwalkable, unmappable)
    that would otherwise stall every increment/full run at it. Records
    exactly ONE audit-only ``operator-skip`` row for the PR (commit_id NULL,
    suspicious, verdict ``possible``, harness/model NULL, raw = ``reason``
    or ``operator skip``), REPLACING only a previous operator-skip row —
    the PR's other evidence rows are never deleted or rewritten. The row is
    excluded from attribution exactly like every suspicious row.

    Deliberately NO marker movement here (and no network, no git): the
    command records the mark and stops. The next ordered run advances past
    the marked PR when its walk reaches it — explicitly, one transaction at
    a time, in processing order — so the marker never jumps over earlier
    unprocessed PRs and the row stays audit-only evidence of the operator's
    decision."""
    if pr_number <= 0:
        raise SweepError(f"--skip-pr must be > 0 (got {pr_number})")
    repo_id = _repo_id(conn)
    observed_at = now or datetime.now(UTC).isoformat(timespec="seconds")
    with conn:  # the mark lands as one explicit transaction
        conn.execute(
            "DELETE FROM pr_evidence WHERE repo_id = ? AND pr_number = ? AND rule = ?",
            (repo_id, pr_number, RULE_OPERATOR_SKIP),
        )
        conn.execute(
            "INSERT INTO pr_evidence (repo_id, pr_number, commit_id, rule, raw, verdict,"
            " harness, model, channel, observed_at, suspicious)"
            " VALUES (?, ?, NULL, ?, ?, 'possible', NULL, NULL, ?, ?, 1)",
            (
                repo_id,
                pr_number,
                RULE_OPERATOR_SKIP,
                reason or OPERATOR_SKIP_RAW,
                CHANNEL_PR,
                observed_at,
            ),
        )


def unskip_pr(conn: sqlite3.Connection, pr_number: int) -> None:
    """Clear PR ``pr_number``'s operator skip: deletes ONLY its
    ``operator-skip`` row — every other evidence row and the marker stay
    untouched — so a later run (increment or sweep) processes the PR
    normally again."""
    if pr_number <= 0:
        raise SweepError(f"--unskip-pr must be > 0 (got {pr_number})")
    repo_id = _repo_id(conn)
    with conn:
        conn.execute(
            "DELETE FROM pr_evidence WHERE repo_id = ? AND pr_number = ? AND rule = ?",
            (repo_id, pr_number, RULE_OPERATOR_SKIP),
        )


def _operator_skipped(conn: sqlite3.Connection) -> set[int]:
    """The PR numbers currently marked to skip (their ``operator-skip``
    audit rows). Read once per run — marks only change between runs."""
    return {
        int(r["pr_number"])
        for r in conn.execute(
            "SELECT pr_number FROM pr_evidence WHERE repo_id = ? AND rule = ?",
            (identity.repo_id_for(conn), RULE_OPERATOR_SKIP),
        )
    }


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
    with number > ``after_pr``, the walk stops at the number bound, and a
    positive bound NEVER advances the marker — a number bound cannot see a
    lower-numbered PR that merged after the marker, so claiming coverage
    past such a gap would make a later increment skip it forever ("zero
    skipped PRs" stays true and a later increment walks the gap). Only
    bound 0 (the full history) claims coverage and advances the marker.
    ``max_prs`` caps the run (a stop
    condition, not a failure). A rate-limit or interruption during the
    LISTING stops the run before anything is processed — a partial listing
    would leave unseen PRs below the claimed marker, so its PRs are never
    walked. A PR carrying an operator-skip row is passed over entirely (no
    rows, no counters, no cap consumed): the faces that may claim coverage
    advance past it explicitly — its own transaction, in processing order —
    so the marker never jumps over earlier unprocessed PRs and the next run
    resumes past the wedge; a positive ``after_pr`` bound still never
    advances."""
    if not branch:
        # an empty Target branch would silently skip every PR through the
        # base-ref mismatch — error loudly instead
        raise SweepError(
            "pr-sweep needs a Target branch: none configured (settings.branch"
            " is empty — run init --branch <target>)"
        )
    owner, name = _github_repo(conn)
    record_disclosure(conn)  # config validated: the tier RUNS — disclosure on
    # before any listing work, so zero PRs (or a listing failure right after)
    # still leaves the marker; SweepError above records nothing
    pulls: list[PullRequest] = []
    requests = 0
    stop: str | None = None
    detail: str | None = None
    try:
        listing = prsource.list_pulls(owner, name, 0 if resume else after_pr, transport)
        pulls, requests = listing.merged, listing.requests
    except (RateLimited, urllib.error.HTTPError, KeyboardInterrupt) as exc:
        stop = _stop_reason(exc)  # incomplete listing: nothing may be processed
        if stop is None:
            stop = STOP_UNAVAILABLE
            detail = str(exc) or type(exc).__name__
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        stop = STOP_UNAVAILABLE
        detail = str(exc) or type(exc).__name__
    if resume:
        marker = resume_key(conn)
        pulls = [p for p in pulls if _is_newer(p.merged_at, p.number, marker)]
        can_advance = True
    else:
        # only a full-history walk (bound 0) claims contiguous coverage; a
        # positive bound leaves every number below it unexamined, and the
        # marker may never claim past that gap
        can_advance = after_pr == 0
    processed = rows_n = suspicious_n = 0
    if stop is None:
        operator_skipped = _operator_skipped(conn)
        try:
            for pulled in pulls:
                if pulled.base_ref != branch:
                    continue  # merged into another branch: never part of this Target
                if pulled.number in operator_skipped:
                    # the operator's audit-only skip: nothing is processed or
                    # recorded and no processing cap is consumed. When this face
                    # may claim coverage, advancing past it is explicit — its own
                    # transaction, in processing order — so the marker never
                    # jumps over earlier unprocessed PRs and the next run
                    # resumes past the wedge; a positive bound still never
                    # advances.
                    if can_advance:
                        with conn:
                            advance_watermark(conn, pulled.merged_at, pulled.number)
                    continue
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
                stop = STOP_UNAVAILABLE
                detail = str(exc) or type(exc).__name__
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            # stop where the counters live: an already-committed prefix stays
            # visible in the result and its watermark is never described as kept
            stop = STOP_UNAVAILABLE
            detail = str(exc) or type(exc).__name__
    return SweepResult(processed, rows_n, suspicious_n, requests, watermark(conn), stop, detail)


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
    A positive bound NEVER advances the resume marker (only bound 0/full
    may claim coverage): the numbers below the bound are unexamined, and a
    lower-numbered PR can merge after the marker — the next increment picks
    both gaps up.
    Run after the walk: the mapping reads the walked commits, and a PR whose
    landing is not walked yet stops the run AT that PR (``STOP_WALK_STALE`` —
    waiting for the walk): nothing is recorded for it, the marker never
    claims past it, and the next run after a newer walk redoes it
    (replace-per-PR)."""
    if after_pr < 0:
        raise SweepError(f"--after-pr must be >= 0 (got {after_pr})")
    return _run(
        conn,
        repo,
        branch,
        after_pr=after_pr,
        resume=False,
        transport=transport,
        now=now,
        max_prs=max_prs,
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
        conn,
        repo,
        branch,
        after_pr=0,
        resume=True,
        transport=transport,
        now=now,
        max_prs=max_prs,
    )


def sweep_backfill(
    conn: sqlite3.Connection,
    repo: Path,
    branch: str,
    *,
    transport: Transport | None = None,
    now: str | None = None,
    max_prs: int | None = DEFAULT_BACKFILL_CAP,
) -> SweepResult:
    """The CI backfill face (UPDATE's ``--pr-backfill full``): BATCHED
    oldest-unincorporated history, NOT a re-sweep. The increment's resume
    machinery at the per-invocation CI cap (``DEFAULT_BACKFILL_CAP``):
    candidates are the merged PRs newer than the resume marker, oldest first
    by merge date (ties by PR number), operator skips included — so every
    dispatch lands one disjoint chunk, the marker advances over it, and the
    next identical dispatch continues past it (never restarting at PR 1)
    until history is caught up. History BELOW the marker is never
    re-recorded here: that explicit re-sweep is the ``sweep_full`` face
    (``pr-sweep --full``, uncapped by default)."""
    return _run(
        conn,
        repo,
        branch,
        after_pr=0,
        resume=True,
        transport=transport,
        now=now,
        max_prs=max_prs,
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
    the shared marker. NOT the CI backfill: update's ``--pr-backfill full``
    is the batched ``sweep_backfill`` face (oldest-unincorporated history
    only, ``DEFAULT_BACKFILL_CAP`` per invocation)."""
    return _run(
        conn,
        repo,
        branch,
        after_pr=0,
        resume=False,
        transport=transport,
        now=now,
        max_prs=max_prs,
    )


def run(
    conn: sqlite3.Connection,
    repo: Path,
    branch: str,
    *,
    after_pr: int | None = None,
    full: bool = False,
    max_prs: int | None = None,
    transport: Transport | None = None,
    now: str | None = None,
) -> SweepResult:
    """THE pr-sweep entry point (the one the CLI verbs call): selects the
    increment / ``--full`` full-history / ``--after-pr`` bounded face.
    Default caps live HERE: the increment face caps at ``DEFAULT_RUN_CAP``
    (a few PRs per push run); the full and bounded faces are unlimited
    unless ``max_prs`` says otherwise. The CI backfill (update's
    ``--pr-backfill full``) is NOT one of these faces: it is the batched
    ``sweep_backfill`` face at ``DEFAULT_BACKFILL_CAP`` per invocation.

    The shared run machinery converts expected API/git/OS failures to
    ``STOP_UNAVAILABLE`` at the phase that owns the counters, so an
    already-committed prefix stays visible. Configuration errors
    (``SweepError``) and programmer failures stay explicit and propagate."""
    if full and after_pr is not None:
        raise SweepError("--full and --after-pr are mutually exclusive")
    if full:
        return sweep_full(conn, repo, branch, transport=transport, now=now, max_prs=max_prs)
    if after_pr is not None:
        return sweep(conn, repo, branch, after_pr, transport=transport, now=now, max_prs=max_prs)
    return sweep_increment(
        conn,
        repo,
        branch,
        transport=transport,
        now=now,
        max_prs=DEFAULT_RUN_CAP if max_prs is None else max_prs,
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
        # the LIKE pattern already anchors the ``(#N)`` suffix — no second
        # regex check to imply a discrimination it does not make
        return "squash", w
    for w in _walked_by_title(conn, f"Merge pull request #{number} from %"):
        return "merge", w
    return None


def _walked_sha(conn: sqlite3.Connection, sha: str) -> bool:
    """Whether the walk already carries this landed sha (its landing is in
    the walked set)."""
    return conn.execute("SELECT 1 FROM commits WHERE sha = ?", (sha,)).fetchone() is not None


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
    mapped = _map_landing(conn, repo, branch, pr)  # the git/API work lives there
    hits = _detect_heads(repo, mapped.head_commits, mapped.attach)
    return _assemble(conn, mapped.anchor, hits, signals, mapped.suspicious)


@dataclass(frozen=True)
class _Mapping:
    """One PR's landing map — the single result of the squash/rebase
    decision tree: the anchor the evidence attaches at, whether the rows
    are suspicious, the head commits to detect on, and each head's
    attachment (a head absent from the map falls back to the anchor in
    ``_assemble``)."""

    anchor: int | None
    suspicious: bool
    head_commits: Sequence[str] = ()
    attach: dict[str, int] = field(default_factory=dict)


def _map_landing(conn: sqlite3.Connection, repo: Path, branch: str, pr: PullRequest) -> _Mapping:
    """The squash/rebase mapping decision tree, producing ONE ``_Mapping``
    (raising ``WalkStale`` where required): where the evidence attaches,
    whether it is suspicious, and which head commits to detect on. Behavior
    is identical across squash, merge, rebase, suspicious, unmapped and
    walk-stale evidence."""
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
            heads = _merge_head_commits(repo, walk.sha)
            return _Mapping(walk.commit_id, False, heads, dict.fromkeys(heads, walk.commit_id))
        tip = fetch_head_ref(repo, pr.number)
        if tip is None:
            return _Mapping(None, True)
        base = gitwalk.merge_base(repo, tip, branch)
        aggregate = gitwalk.patch_id_for_range(repo, base, tip) if base else None
        if not base or (
            walk.subject_patch_id is not None
            and aggregate is not None
            and aggregate != walk.subject_patch_id
        ):
            # not plausibly the PR's merge head (post-merge force-push or a
            # squash subject that named the wrong commit)
            return _Mapping(None, True, _safe_range(repo, base, tip))
        if aggregate is None or walk.subject_patch_id is None:
            # empty diffs carry no patch-id: None on None is NOT a match —
            # no false trust. Genuinely unmapped per the squash strategy, but
            # not suspicious: recorded unattached, never attributed
            return _Mapping(None, False, _safe_range(repo, base, tip))
        heads = gitwalk.list_range(repo, base, tip)
        return _Mapping(walk.commit_id, False, heads, dict.fromkeys(heads, walk.commit_id))

    # rebase-merged (or an unrecognised landing): map the fetched head by
    # patch-id — the rebase twins keep each head commit's patch
    tip = fetch_head_ref(repo, pr.number)
    if tip is None:
        return _Mapping(None, True)
    base = gitwalk.merge_base(repo, tip, branch)
    if base is None:
        return _Mapping(None, True)
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
            return _Mapping(None, False, head)
        if pr.landing_sha is not None:
            # the landing is walked (checked above) yet nothing maps: a
            # genuinely unmappable head (post-merge force-push) — suspicious
            return _Mapping(None, True, head)
        # nothing names the PR and nothing maps: its landing cannot be shown
        # to be walked — wait for the walk rather than lose the evidence
        raise WalkStale(
            f"PR {pr.number}: no walked commit maps its head (its landing is"
            " not in the walked set) — waiting for the walk to catch up"
        )
    if mapping_failed:
        # some head commits map but not all: mapped-but-parentage-failed
        return _Mapping(None, True, head)
    anchor = max(twins.values(), key=lambda w: w.walk_index).commit_id
    return _Mapping(anchor, False, head, {hc: w.commit_id for hc, w in twins.items()})


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
    attach: Mapping[str, int],
) -> list[tuple[int | None, aiattr.Match]]:
    """Rule-table detection over head commits (#34's engine on the head
    commits' own facts), each match carried toward its mapped commit id —
    ONE per-head attachment map (a head absent from it falls back to the
    anchor in ``_assemble``)."""
    hits: list[tuple[int | None, aiattr.Match]] = []
    for hc in head_commits:
        cid = attach.get(hc)
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
    signals at the PR's landing — THE one dedup owner of row uniqueness
    (per (commit, rule, raw), here alone: storage never dedupes again, its
    UNIQUE constraint is the last guard) and — the storage side of channel
    precedence — never doubling a match the walk already recorded at the
    same commit ("recorded once, not twice"). Suspicious rows keep no
    commit: recorded, marked, never attributed."""
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
        if (
            not suspicious
            and r.commit_id is not None
            and _git_recorded(conn, r.commit_id, r.rule, r.raw)
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
