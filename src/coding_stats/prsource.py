"""The GitHub PR source behind the channel-pr sweep: the stdlib API transport,
PR wire parsing, and list pagination (the LIST half of ADR-0007 clause 3 —
the canonical design; detection, mapping, and recording live in ``prsweep``).

One ``GET /repos/{owner}/{repo}/pulls?state=all`` page per 100 PRs
(``list_pulls``): bodies, titles and authors come from the LIST response —
ZERO per-PR API calls and no commit-list endpoints anywhere. Mergedness is the
wire's ``merged_at`` timestamp-or-null (there is NO ``merged`` boolean on the
wire); ``_norm_ts`` normalizes it into the resume marker's comparable form.

The stop/error vocabulary (``STOP_RATE_LIMIT``/``STOP_INTERRUPTED``/
``STOP_FORBIDDEN``, ``RateLimited``) and its classifiers (``_http_stop``,
``_stop_reason``) live here because they classify THIS layer's HTTP failures
(429, 403, 401) and interruptions into run stops. The run-policy labels
(cap, walk-stale, fetch-failed, unavailable) belong to ``prsweep``'s run
machinery, which assigns them from this set. ``list_pulls`` binds the
optional default transport, so ``coding_stats.prsource.default_transport``
is the patch seam callers and tests override.
"""

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

DEFAULT_PER_PAGE = 100

# THIS layer's HTTP-classified stops; the run-policy labels (cap,
# walk-stale, fetch-failed, unavailable) live in prsweep's run machinery
STOP_RATE_LIMIT = "rate-limit"
STOP_INTERRUPTED = "interrupted"
STOP_FORBIDDEN = "forbidden"

REQUEST_TIMEOUT = 30  # seconds: a hung API never hangs the sweep

Transport = Callable[[str], Any]  # url -> parsed JSON body


class RateLimited(RuntimeError):
    """The GitHub API rate budget is exhausted: a stop condition, not a
    failure — the run ends cleanly and the next one resumes."""


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


def _http_stop(exc: urllib.error.HTTPError) -> str | None:
    """Classify an API HTTP failure into a stop condition — one classifier for
    403 AND 429 on every surface. Rate-limit exhaustion ONLY when it
    genuinely is one (429 is throttling by definition, or a 403 carrying
    ``X-RateLimit-Remaining: 0``); a permissions 403/401 ("Resource not
    accessible by integration") is a distinct stop naming the permission,
    never laundered into "rate limit exhausted". None = not one of these
    classified stops; the run machinery reports it as its STOP_UNAVAILABLE
    label (``prsweep`` owns the run-policy labels)."""
    if exc.code == 429 or (exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0"):
        return STOP_RATE_LIMIT
    if exc.code in (401, 403):
        return STOP_FORBIDDEN
    return None


def _stop_reason(exc: BaseException) -> str | None:
    """ONE classifier for the stop-condition triple the listing and
    processing phases share: genuine rate-limit exhaustion (``RateLimited``,
    or an HTTP failure ``_http_stop`` classifies as one), a distinct
    forbidden stop, or an interruption. None = an unclassified HTTP failure;
    the run machinery converts that operational failure to its
    STOP_UNAVAILABLE label rather than mislabelling it as a rate limit or
    interruption."""
    if isinstance(exc, urllib.error.HTTPError):
        return _http_stop(exc)
    return STOP_RATE_LIMIT if isinstance(exc, RateLimited) else STOP_INTERRUPTED


def default_transport(url: str) -> Any:
    """The stdlib GitHub API transport (zero runtime dependencies). Auth via
    GITHUB_TOKEN when present (``pull-requests: read`` is enough);
    unauthenticated access works for public repos within the smaller rate
    budget. GENUINE
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


def list_pulls(owner: str, repo: str, after: int, transport: Transport | None = None) -> Listing:
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
    cutoff (the out-of-order case the merge-date key repairs). The transport
    is OPTIONAL: ``transport=None`` binds ``default_transport``, so
    ``coding_stats.prsource.default_transport`` is the single patch seam
    callers and tests override."""
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
