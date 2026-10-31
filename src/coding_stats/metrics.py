"""Feature-level metrics over SQL + diff scans: features/year, fixes/feature, churn,
and time series (features over time, 1-month-rolling fixes/feature)."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import NamedTuple

from . import aiattr
from .orgview import org


@dataclass
class FeatureTimePoint:
    month: str  # YYYY-MM
    features_new: int
    features_cumulative: int
    commits: int
    fixes: int


@dataclass
class RollingPoint:
    when: str  # YYYY-MM-DD (window end, UTC-normalized)
    fixes_window: int  # fix commits in the trailing window
    features_touched: int  # distinct features those fixes reach
    fixes_per_feature: float


def _add_months(month: str, n: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    total = y * 12 + (m - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _month_fill(first: str, last: str) -> list[str]:
    months = []
    cur = first
    while cur <= last:
        months.append(cur)
        cur = _add_months(cur, 1)
    return months


def features_timeline(conn: sqlite3.Connection) -> list[FeatureTimePoint]:
    """Monthly buckets from first to last walk commit: new/cumulative features,
    commits, and fix commits per bucket. Org-level: a Shared commit (a sha
    held by several keys of one repo) counts once (the earliest verdict)."""
    conn = org(conn)
    features_by_month = {
        r["m"]: r["n"]
        for r in conn.execute(
            "SELECT strftime('%Y-%m', created_at) m, COUNT(*) n FROM features GROUP BY m"
        )
    }
    commits_by_month = {
        r["m"]: r["n"]
        for r in conn.execute(
            "SELECT strftime('%Y-%m', committed_at) m, COUNT(*) n FROM commits GROUP BY m"
        )
    }
    fixes_by_month = {
        r["m"]: r["n"]
        for r in conn.execute(
            """SELECT strftime('%Y-%m', c.committed_at) m, COUNT(*) n
               FROM commits c JOIN commit_verdicts v ON v.commit_id = c.id
               WHERE v.verdict = 'fix' GROUP BY m"""
        )
    }
    if not commits_by_month and not features_by_month:
        return []
    first = min(
        min(commits_by_month, default="9999"),
        min(features_by_month, default="9999"),
    )
    last = max(max(commits_by_month, default=""), max(features_by_month, default=""))
    points: list[FeatureTimePoint] = []
    features_seen = 0
    for m in _month_fill(first, last):
        n_new = features_by_month.get(m, 0)
        features_seen += n_new
        points.append(
            FeatureTimePoint(
                month=m,
                features_new=n_new,
                features_cumulative=features_seen,
                commits=commits_by_month.get(m, 0),
                fixes=fixes_by_month.get(m, 0),
            )
        )
    return points


def fixes_rolling(
    conn: sqlite3.Connection, window_days: int = 30, step_days: int = 7
) -> list[RollingPoint]:
    """fixes/feature with a trailing 1-month window, sampled weekly.

    Numerator: all fix commits falling in the trailing window (end -
    window_days + 1 .. window end). Denominator: the distinct features those
    fixes reach through the fixes_features lineage closure — a feature counts
    once however many fixes touch it. Dates are normalized by SQLite
    (strftime) so month buckets and windows share one timezone convention.
    The last sample is always placed at the latest commit date. Org-level:
    Shared commits count once (the projection dedups them)."""
    conn = org(conn)
    end_s = conn.execute(
        "SELECT MAX(strftime('%Y-%m-%d', committed_at)) d FROM commits"
    ).fetchone()["d"]
    if not end_s:
        return []
    end = date.fromisoformat(end_s)

    fix_dates: dict[int, date] = {}
    for r in conn.execute(
        """SELECT v.commit_id id, strftime('%Y-%m-%d', c.committed_at) d
           FROM commits c JOIN commit_verdicts v ON v.commit_id = c.id
           WHERE v.verdict = 'fix'"""
    ):
        fix_dates[r["id"]] = date.fromisoformat(r["d"])
    if not fix_dates:
        return []

    reached: dict[int, set[str]] = {}
    for r in conn.execute("SELECT fix_commit_id, feature_id FROM fixes_features"):
        reached.setdefault(r["fix_commit_id"], set()).add(r["feature_id"])

    span = timedelta(days=window_days - 1)

    def pt(day: date) -> RollingPoint:
        lo = day - span
        fixes_in = [cid for cid, d in fix_dates.items() if lo <= d <= day]
        touched = set().union(*(reached.get(cid, set()) for cid in fixes_in))
        n = len(fixes_in)
        ratio = round(n / len(touched), 3) if touched else 0.0
        return RollingPoint(day.isoformat(), n, len(touched), ratio)

    points: list[RollingPoint] = []
    cur = min(fix_dates.values())
    while cur <= end:
        points.append(pt(cur))
        cur += timedelta(days=step_days)
    if points[-1].when != end.isoformat():
        points.append(pt(end))
    return points


@dataclass
class MetricSnapshot:
    total_commits: int
    verdict_counts: dict[str, int]
    total_features: int
    total_fixes: int
    years: list[tuple[str, int]]  # year -> features created
    features_per_year: float
    fixes_per_feature: float  # overall mean
    # year -> mean fixes per feature created that year
    fixes_by_year_mean: list[tuple[str, float]]
    top_features_by_fixes: list[tuple[str, str, int]]
    top_features_by_churn: list[tuple[str, str, int]]
    fixes_uncovered: int  # fix verdicts attributed to no feature


def churn_per_feature(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(feature id, title, churn lines) per feature over its linked commits.
    Churn is read from the walk-stored churn_lines columns (derived at walk
    time by gitwalk.prepare_diff — the single source of the counting rules)."""
    conn = org(conn)
    rows = conn.execute(
        """SELECT f.id, f.title, COALESCE(SUM(c.churn_lines), 0) n FROM features f
           JOIN commits_features cf ON cf.feature_id = f.id
           JOIN commits c ON c.id = cf.commit_id
           GROUP BY f.id, f.title"""
    ).fetchall()
    out = [(str(r["id"]), str(r["title"]), int(r["n"])) for r in rows]
    return sorted(out, key=lambda kv: (-kv[2], kv[0]))


class FeatureRank(NamedTuple):
    """One feature with its attributed-fix count, churn and live lines.
    Internal API: callers unpack by name, never by position."""

    fid: str
    title: str
    created: str  # YYYY-MM-DD, first defining-commit date
    fixes: int
    churn: int
    sha: str  # earliest defining commit sha
    live: int  # features.live_lines at the latest live-lines run
    repo: str  # repository slug (REPO column for multi-repo reports)


def feature_ranking(conn: sqlite3.Connection) -> list[FeatureRank]:
    """All features ranked by attributed fixes (lineage closure, ties broken
    by feature id), with churn and the earliest defining-commit sha. Org
    level: one feature row per duplicated (seeded) feature id."""
    conn = org(conn)
    churn = {fid: n for fid, _t, n in churn_per_feature(conn)}
    sha = {
        r["fid"]: r["sha"]
        for r in conn.execute(
            """SELECT cf.feature_id fid, MIN(c.walk_index) w, c.sha
               FROM commits_features cf JOIN commits c ON c.id = cf.commit_id
               WHERE cf.role = 'defines' GROUP BY cf.feature_id"""
        )
    }
    return [
        FeatureRank(
            fid=str(r["fid"]),
            title=str(r["title"]),
            created=str(r["d"])[:10],
            fixes=int(r["n"]),
            churn=int(churn.get(str(r["fid"]), 0)),
            sha=str(sha.get(r["fid"], "")),
            live=int(r["live"]),
            repo=str(r["repo"]),
        )
        for r in conn.execute(
            """SELECT f.id fid, f.title, f.created_at d, f.live_lines live, rp.slug repo,
                      COUNT(ff.fix_commit_id) n
               FROM features f
               JOIN repositories rp ON rp.id = f.repo_id
               LEFT JOIN fixes_features ff ON ff.feature_id = f.id
               GROUP BY f.id ORDER BY n DESC, f.id"""
        )
    ]


def repo_count(conn: sqlite3.Connection) -> int:
    """How many Repository keys this DB covers (1 for a single-repo DB)."""
    return int(conn.execute("SELECT COUNT(*) c FROM repositories").fetchone()["c"])


def feature_repos(conn: sqlite3.Connection) -> dict[str, str]:
    """feature id -> repository slug (the REPO column for multi-repo views;
    org level reads the projection — one row per duplicated id)."""
    conn = org(conn)
    return {
        str(r["fid"]): str(r["slug"])
        for r in conn.execute(
            "SELECT f.id fid, rp.slug slug FROM features f"
            " JOIN repositories rp ON rp.id = f.repo_id"
        )
    }


@dataclass
class AttributionSummary:
    """AI attribution state across the walked commits (ticket #34/#38): the
    3-state verdict mix, the identities the evidence names, the channel each
    commit's attribution resolved through, and the AI share. Every number is
    a LOWER BOUND: the share counts over walked commits and is never
    normalized upward to PR counts."""

    verdict_counts: dict[str, int]  # 'yes'/'possible'/'no-evidence' -> commits
    harness_counts: list[tuple[str, int]]  # harness -> attributed commits
    model_counts: list[tuple[str, int]]  # model -> attributed commits (where known)
    channel_counts: dict[str, int]  # 'git'/'pr' -> commits attributed through it
    github_sourced: bool  # contributing PR-channel evidence present (disclosure)
    ai_share: float  # yes / walked commits — a LOWER BOUND (0.0 with no commits)


@dataclass
class AttributionRow:
    """One attributed commit with its audit evidence (rule id + raw matched
    string, in the rule table's rank order) and the channel its attribution
    resolved through ('git' | 'pr')."""

    sha: str
    verdict: str
    harness: str
    model: str
    evidence: list[tuple[str, str, str]]  # (rule, raw, verdict)
    channel: str = ""


@dataclass
class RepoAttribution:
    """AI-attribution state of one repository's walked commits (the org
    report's AI-share-per-repo table, a lower bound per row)."""

    slug: str
    commits: int
    yes: int
    possible: int
    no_evidence: int
    ai_share: float  # yes / commits — a LOWER BOUND (0.0 with no commits)


@dataclass
class FeatureAttribution:
    """AI-attribution state of one feature's linked commits (the org
    report's AI-share-per-feature table, a lower bound per row). Scope: the
    commits the feature is linked to (defines or touches); a commit claimed
    by two features counts in both."""

    fid: str
    title: str
    repo: str  # repository slug
    commits: int
    yes: int
    possible: int
    no_evidence: int
    ai_share: float  # yes / commits — a LOWER BOUND (0.0 with no commits)


@dataclass
class _Resolved:
    """One commit's union-level attribution: the channel-precedence
    resolution of its contributing evidence rows (aiattr.effective_*)."""

    cid: int
    sha: str
    slug: str
    verdict: str
    harness: str | None
    model: str | None
    channel: str  # '' | 'git' | 'pr'
    evidence: list[tuple[str, str, str]]


def _resolved(conn: sqlite3.Connection) -> list[_Resolved]:
    """Every walked commit's attribution, resolved ONCE per commit from the
    union of its evidence rows — the channel-precedence rule of
    aiattr.effective_attribution reused verbatim at the union level (never
    duplicated): the verdict is the strongest contributing tier, identity
    claims resolve git-first, suspicious rows contribute nothing. The
    channel is the strongest channel that contributed evidence ('git' when
    the walk recorded any, else 'pr'); no contributing evidence reads as
    '' and the verdict as ``no-evidence``."""
    conn = org(conn)
    out: list[_Resolved] = []
    for r in conn.execute(
        """SELECT c.id cid, c.sha sha, rp.slug slug FROM commits c
           JOIN repositories rp ON rp.id = c.repo_id
           ORDER BY c.walk_index, c.sha"""
    ):
        cid = int(r["cid"])
        attr = aiattr.effective_attribution(conn, cid)
        tagged = aiattr.effective_evidence(conn, cid)
        seen: set[tuple[str, str, str]] = set()
        evidence: list[tuple[str, str, str]] = []
        for _ch, rule, raw, verdict in tagged:
            key = (rule, raw, verdict)
            if key not in seen:
                seen.add(key)
                evidence.append(key)
        channels = [ch for ch, _rule, _raw, _v in tagged]
        channel = (
            aiattr.CHANNEL_GIT
            if aiattr.CHANNEL_GIT in channels
            else (aiattr.CHANNEL_PR if channels else "")
        )
        out.append(
            _Resolved(
                cid, str(r["sha"]), str(r["slug"]), attr.verdict, attr.harness, attr.model,
                channel, evidence,
            )
        )
    return out


def attribution_summary(conn: sqlite3.Connection) -> AttributionSummary:
    """Org-level AI attribution counts (the projection dedups Shared commits
    to ONE attribution each). ``no-evidence`` is absence of a signal — never
    a claim about authorship — and every count is a lower bound over walked
    commits, never normalized upward to PR counts."""
    conn = org(conn)
    rows = _resolved(conn)
    verdict_counts: dict[str, int] = {}
    harness_counts: dict[str, int] = {}
    model_counts: dict[str, int] = {}
    channel_counts: dict[str, int] = {}
    yes = 0
    for r in rows:
        verdict_counts[r.verdict] = verdict_counts.get(r.verdict, 0) + 1
        if r.verdict == aiattr.VERDICT_YES:
            yes += 1
        if r.verdict != aiattr.VERDICT_NO_EVIDENCE:
            if r.harness:
                harness_counts[r.harness] = harness_counts.get(r.harness, 0) + 1
            if r.model:
                model_counts[r.model] = model_counts.get(r.model, 0) + 1
        if r.channel:
            channel_counts[r.channel] = channel_counts.get(r.channel, 0) + 1
    github_sourced = (
        conn.execute("SELECT 1 FROM pr_evidence WHERE suspicious = 0 LIMIT 1").fetchone()
        is not None
    )
    return AttributionSummary(
        verdict_counts=verdict_counts,
        harness_counts=sorted(harness_counts.items(), key=lambda kv: (-kv[1], kv[0])),
        model_counts=sorted(model_counts.items(), key=lambda kv: (-kv[1], kv[0])),
        channel_counts=channel_counts,
        github_sourced=github_sourced,
        ai_share=(yes / len(rows)) if rows else 0.0,
    )


def attribution_rows(conn: sqlite3.Connection) -> list[AttributionRow]:
    """Attributed commits (never the no-evidence ones — they carry no
    evidence rows) in walk order, each with its channel-resolved evidence
    rows (git channel first)."""
    out: list[AttributionRow] = []
    for r in _resolved(conn):
        if r.verdict == aiattr.VERDICT_NO_EVIDENCE:
            continue
        out.append(
            AttributionRow(
                sha=r.sha,
                verdict=r.verdict,
                harness=r.harness or "",
                model=r.model or "",
                evidence=r.evidence,
                channel=r.channel,
            )
        )
    return out


def attribution_by_repo(conn: sqlite3.Connection) -> list[RepoAttribution]:
    """Per-repository AI-attribution state over each repo's walked commits —
    the org report's AI-share-per-repo table (a lower bound per row). A key
    whose commits all deduped under another key reads as all-zero: Shared
    commits count ONCE, under the dedup's winner."""
    conn = org(conn)
    by_slug: dict[str, list[_Resolved]] = {}
    for r in _resolved(conn):
        by_slug.setdefault(r.slug, []).append(r)
    out: list[RepoAttribution] = []
    for row in conn.execute("SELECT slug FROM repositories ORDER BY slug"):
        slug = str(row["slug"])
        commits, yes, possible, no_evidence = _tally(by_slug.get(slug, []))
        out.append(
            RepoAttribution(
                slug=slug,
                commits=commits,
                yes=yes,
                possible=possible,
                no_evidence=no_evidence,
                ai_share=(yes / commits) if commits else 0.0,
            )
        )
    return out


def attribution_by_feature(conn: sqlite3.Connection) -> list[FeatureAttribution]:
    """Per-feature AI-attribution state over the feature's linked commits
    (defines or touches) — the org report's AI-share-per-feature table (a
    lower bound per row). A commit claimed by two features counts in both."""
    conn = org(conn)
    by_id = {r.cid: r for r in _resolved(conn)}
    out: list[FeatureAttribution] = []
    for f in conn.execute(
        """SELECT f.id fid, f.title title, rp.slug slug FROM features f
           JOIN repositories rp ON rp.id = f.repo_id ORDER BY f.id"""
    ):
        fid = str(f["fid"])
        linked = sorted(
            {
                int(r["commit_id"])
                for r in conn.execute(
                    "SELECT DISTINCT commit_id FROM commits_features WHERE feature_id = ?",
                    (fid,),
                )
            }
        )
        commits, yes, possible, no_evidence = _tally(
            [by_id[cid] for cid in linked if cid in by_id]
        )
        out.append(
            FeatureAttribution(
                fid=fid,
                title=str(f["title"]),
                repo=str(f["slug"]),
                commits=commits,
                yes=yes,
                possible=possible,
                no_evidence=no_evidence,
                ai_share=(yes / commits) if commits else 0.0,
            )
        )
    return out


def _tally(rows: list[_Resolved]) -> tuple[int, int, int, int]:
    """(commits, yes, possible, no-evidence) over one scope's resolved
    commits — the three states always sum to the commit count."""
    yes = sum(1 for r in rows if r.verdict == aiattr.VERDICT_YES)
    possible = sum(1 for r in rows if r.verdict == aiattr.VERDICT_POSSIBLE)
    return len(rows), yes, possible, len(rows) - yes - possible


def sampling_complete(conn: sqlite3.Connection) -> bool:
    """Whether sample history is complete enough to render gaps as zeros. A
    union DB is only as complete as its LEAST complete constituent
    (union_artifacts carries each artifact's backfill_state); a single-repo DB
    follows its own backfill_state. Unknown states read as incomplete."""
    if _has_table(conn, "union_artifacts"):
        rows = conn.execute("SELECT backfill_state FROM union_artifacts").fetchall()
        if rows:
            return all(_state_done(str(r["backfill_state"])) for r in rows)
    return _state_done(_setting(conn, "backfill_state"))


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def _state_done(raw: str) -> bool:
    try:
        state = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(state, dict) and state.get("status") == "done"


def snapshot(conn: sqlite3.Connection, top: int = 15) -> MetricSnapshot:
    conn = org(conn)
    total_commits = conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"]
    verdict_counts = {
        r["verdict"]: r["n"]
        for r in conn.execute("SELECT verdict, COUNT(*) n FROM commit_verdicts GROUP BY verdict")
    }
    total_features = conn.execute("SELECT COUNT(*) c FROM features").fetchone()["c"]
    total_fixes = verdict_counts.get("fix", 0)

    years = [
        (r["yr"], r["n"])
        for r in conn.execute(
            "SELECT strftime('%Y', created_at) yr, COUNT(*) n FROM features GROUP BY yr ORDER BY yr"
        )
    ]
    span_years = max(1, len(years))
    features_per_year = total_features / span_years

    fixes_per_feature_map = {
        r["fid"]: r["n"]
        for r in conn.execute(
            "SELECT feature_id fid, COUNT(DISTINCT fix_commit_id) n"
            " FROM fixes_features GROUP BY feature_id"
        )
    }
    fixes_per_feature = (
        sum(fixes_per_feature_map.values()) / total_features if total_features else 0.0
    )

    fixes_by_year: dict[str, list[int]] = {}
    for r in conn.execute(
        "SELECT id, title, strftime('%Y', created_at) yr FROM features ORDER BY id"
    ):
        fixes_by_year.setdefault(r["yr"], []).append(fixes_per_feature_map.get(r["id"], 0))
    fixes_by_year_mean = [(yr, sum(vals) / len(vals)) for yr, vals in sorted(fixes_by_year.items())]

    top_features_by_fixes = [(r.fid, r.title, r.fixes) for r in feature_ranking(conn)[:top]]

    fixes_uncovered = conn.execute(
        """SELECT COUNT(*) c FROM commit_verdicts v
           WHERE v.verdict = 'fix'
             AND NOT EXISTS
               (SELECT 1 FROM fixes_features ff WHERE ff.fix_commit_id = v.commit_id)"""
    ).fetchone()["c"]

    churn = churn_per_feature(conn)
    return MetricSnapshot(
        total_commits=total_commits,
        verdict_counts=verdict_counts,
        total_features=total_features,
        total_fixes=total_fixes,
        years=years,
        features_per_year=features_per_year,
        fixes_per_feature=fixes_per_feature,
        fixes_by_year_mean=fixes_by_year_mean,
        top_features_by_fixes=top_features_by_fixes,
        top_features_by_churn=churn[:top],
        fixes_uncovered=fixes_uncovered,
    )


def snapshot_to_json(snap: MetricSnapshot) -> str:
    return json.dumps(
        {
            "total_commits": snap.total_commits,
            "verdicts": snap.verdict_counts,
            "features": snap.total_features,
            "fixes": snap.total_fixes,
            "features_per_year": round(snap.features_per_year, 2),
            "fixes_per_feature": round(snap.fixes_per_feature, 3),
            "features_per_year_byyear": dict(snap.years),
            "fixes_per_feature_byyear": {y: round(v, 2) for y, v in snap.fixes_by_year_mean},
            "top_features_by_fixes": [
                {"id": fid, "title": t, "fixes": n} for fid, t, n in snap.top_features_by_fixes
            ],
            "top_features_by_churn": [
                {"id": fid, "title": t, "churn": n} for fid, t, n in snap.top_features_by_churn
            ],
            "fixes_uncovered": snap.fixes_uncovered,
        },
        indent=2,
    )
