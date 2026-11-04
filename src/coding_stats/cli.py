"""CLI: uv run coding-stats {init,walk,classify,derive,report,update,ci,gather,migrate}.
Requires the
opencode CLI on PATH for classify (ADR-0001 rolling session). update is the
orchestrating verb (ADR-0003 clause 7); the stage subcommands stay plumbing;
ci renders the workflow and lands artifact updates; migrate moves a legacy
skill-stats layout to coding-stats (ADR-0005)."""

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import aiattr, ci, datastore, db, gitwalk, identity, prsource, prsweep
from .classify import DEFAULT_MODEL, SESSION_DIR, Session, classify_pending
from .derive import run_derive

if TYPE_CHECKING:
    _SubParsers = argparse._SubParsersAction[argparse.ArgumentParser]


class StoreLayout(Enum):
    """Which committed store a run executes against: the default Data branch
    (ADR-0006) or the legacy store still committed on the Target branch —
    the migrate verb regenerates the artifact ON the Target checkout
    (ADR-0006 clause 6), and the update skips tied to the Data-branch
    machinery must not apply there."""

    DATA_BRANCH = "data-branch"
    LEGACY_COMMITTED = "legacy-committed"


@dataclass(frozen=True)
class UpdateOptions:
    """The update verb's options. The argparse boundary for `_run_update`: a
    typed, frozen record instead of a duck-typed Namespace, so no caller
    (notably the migration, which used to fabricate a Namespace by hand) can
    smuggle an alien field into the run. The fields are exactly the update
    parser's flags; `update_options` is the one bridge from argparse, and the
    drift guard in tests/test_update.py pins the two sides together."""

    db: str = "coding-stats.db"
    no_classify: bool = False
    # No-detect / no-lineage debug trims: they skip derive's attribution
    # rebuild (the live-lines pass always runs — how the old pipeline's
    # trims composed).
    no_detect: bool = False
    no_lineage: bool = False
    no_serialize: bool = False
    detect_pr: bool = False
    pr_backfill: str | None = None


def update_options(args: argparse.Namespace) -> UpdateOptions:
    """Build UpdateOptions from the update parser's namespace: reads exactly
    UpdateOptions' field names off vars(args), so a dest the parser dropped
    fails loudly here instead of silently defaulting."""
    given = vars(args)
    return UpdateOptions(**{f.name: given[f.name] for f in fields(UpdateOptions)})


def _update_parser(sub: "_SubParsers") -> argparse.ArgumentParser:
    """The update verb's parser. Extracted from main so the drift-guard test
    reads the same construction point: every flag defined here must be an
    UpdateOptions field (tests/test_update.py)."""
    up = sub.add_parser(
        "update",
        help="verify -> walk -> seed -> classify -> derive -> serialize + digest (ADR-0006:"
        " the store holds only the SQL artifact; reports render via the report"
        " verb/action)",
    )
    up.add_argument("--db", default="coding-stats.db")
    up.add_argument("--no-classify", action="store_true", help="skip LLM classification")
    up.add_argument(
        "--no-detect",
        action="store_true",
        help="debug trim: skip the derive stage's fix attribution + lineage"
        " closure rebuild (the live-lines pass still runs; --no-lineage is"
        " the same trim)",
    )
    up.add_argument(
        "--no-lineage",
        action="store_true",
        help="debug trim: as --no-detect (derive is one stage: fix"
        " attribution, lineage closure, live-lines attribution)",
    )
    up.add_argument("--no-serialize", action="store_true", help="skip artifact write")
    up.add_argument(
        "--detect-pr",
        action="store_true",
        help="opt-in PR tier (ADR-0007 clause 1): run the watermark-driven"
        " PR-channel increment after the deterministic stages, before"
        " serialization (detection-only, no LLM calls)",
    )
    up.add_argument(
        "--pr-backfill",
        choices=("full",),
        default=None,
        metavar="full",
        help="batched history backfill (the workflow_dispatch passthrough"
        " `pr-backfill: full`): oldest-unincorporated merged PRs past the"
        " watermark — the increment's resume machinery at 250 PRs per"
        " invocation (DEFAULT_BACKFILL_CAP). Each run lands one disjoint chunk"
        " and its watermark, and the next identical dispatch continues past it"
        " (never restarting at PR 1) until history is caught up. NOT a re-sweep:"
        " history below the marker is the direct `pr-sweep --full` face's job",
    )
    return up


def _settings(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM settings")
    }


def _set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )
    conn.commit()


def new_db(default: str, args: argparse.Namespace) -> Path:
    p = Path(getattr(args, "db", None) or default)
    p.parent.mkdir(parents=True, exist_ok=True)
    db.connect(p)
    return p


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="coding-stats")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="create a DB and store run config")
    init.add_argument("repo")
    init.add_argument("--branch", default=None)
    init.add_argument("--model", default=DEFAULT_MODEL)
    init.add_argument("--min-target-lines", type=int, default=1)
    init.add_argument("--db", default="coding-stats.db")

    walk = sub.add_parser("walk", help="walk Target branch into DB")
    walk.add_argument("--db", default="coding-stats.db")

    cls = sub.add_parser("classify", help="LLM roll walk (opencode CLI)")
    cls.add_argument("--db", default="coding-stats.db")
    cls.add_argument("--limit", type=int, default=None)
    cls.add_argument("--fresh", action="store_true", help="drop saved opencode session")

    der = sub.add_parser(
        "derive",
        help="one deterministic pass: fix attribution -> lineage closure -> live lines",
    )
    der.add_argument("--db", default="coding-stats.db")

    rep = sub.add_parser("report", help="metrics text summary + optional charts")
    rep.add_argument("--db", default="coding-stats.db")
    rep.add_argument("--charts", action="store_true")
    rep.add_argument("--html", nargs="?", const="coding-stats-report.html", default=None)
    rep.add_argument("--out", default="coding-stats-report")
    rep.add_argument("--json", action="store_true")

    _update_parser(sub)

    ci_grp = sub.add_parser("ci", help="CI scaffold: workflow rendering + artifact landing")
    ci_sub = ci_grp.add_subparsers(dest="ci_cmd", required=True)
    ci_init = ci_sub.add_parser(
        "init",
        help="write the short caller workflow that uses the coding-stats actions"
        " (idempotent, refuses drift)",
    )
    ci_init.add_argument("--branch", default=None, help="Target branch (default: current)")
    ci_init.add_argument(
        "--no-report", action="store_true", help="write only the update workflow (no report job)"
    )
    ci_init.add_argument(
        "--detect-pr",
        action="store_true",
        help='render the ADR-0007 clause 1 opt-in: detect-pr: "true" on the'
        " action plus pull-requests: read on the update job",
    )
    ci_init.add_argument("--upgrade", action="store_true", help="overwrite a drifted workflow")
    ci_land = ci_sub.add_parser(
        "land", help="land the artifact-update commit (Q15/Q17/Q18 rules; usable anywhere)"
    )
    ci_land.add_argument("--branch", default=None, help="Target branch (default: current)")
    ci_land.add_argument("--remote", default="origin")

    gath = sub.add_parser("gather", help="ingest artifacts into a union DB keyed by Repository key")
    gath.add_argument("--db", default="coding-stats-union.db", help="union DB to build/refresh")
    gath.add_argument(
        "sources", nargs="+", help="repo checkouts (read .coding-stats/) or artifact file paths"
    )

    sweep_p = sub.add_parser(
        "pr-sweep",
        help="channel-pr evidence sweep (ADR-0007 clauses 2-5): watermark-driven"
        " per-increment backfill of PR-channel evidence rows (detection-only, no LLM)",
    )
    sweep_p.add_argument("--db", default="coding-stats.db")
    bound = sweep_p.add_mutually_exclusive_group()
    bound.add_argument(
        "--after-pr",
        type=int,
        default=None,
        metavar="N",
        help="explicit lower bound: record PRs with number > N (N >= 0;"
        " a positive bound never advances the resume marker — only bound 0/full"
        " claims coverage past the gap below the bound)",
    )
    bound.add_argument(
        "--full",
        action="store_true",
        help="full-history sweep (explicit operator invocation): re-sweep every"
        " merged PR from the beginning of the numbering, oldest first by merge"
        " date (ties by PR number), sharing the watermark (later push runs"
        " continue after it) — the explicit re-sweep face (history below the"
        " marker included), uncapped unless --max-prs is given. NOT the CI"
        " backfill: update --pr-backfill full is the batched"
        " oldest-unincorporated-history face instead",
    )
    bound.add_argument(
        "--skip-pr",
        type=int,
        default=None,
        metavar="N",
        help="mark PR N to skip (the operator override for a permanently"
        " unprocessable PR that would stall every run at it): records ONLY an"
        " audit-only operator-skip row (suspicious, never attribution) and never"
        " touches the PR's other rows. It does NOT move the marker — the next"
        " ordered run advances past N when it reaches it, so the marker never"
        " jumps over earlier unprocessed PRs; the row is audit-only",
    )
    bound.add_argument(
        "--unskip-pr",
        type=int,
        default=None,
        metavar="N",
        help="clear PR N's operator skip (deletes only the operator-skip row):"
        " later runs process it normally while it is at/past the resume marker;"
        " once the marker has passed N, run pr-sweep --full to record it again",
    )
    sweep_p.add_argument(
        "--max-prs",
        type=int,
        default=None,
        metavar="K",
        help="stop cleanly after K PRs this run (default: 25 for the watermark"
        " increment, unlimited for --full / --after-pr)",
    )

    mig = sub.add_parser(
        "migrate",
        help="one-time migration from the legacy skill-stats layout (ADR-0005)",
        epilog="Moves the committed artifact (.skill-stats/ -> a regenerated .coding-stats/"
        " store), the machine-local working DB (reused when it corresponds to the legacy"
        " artifact, else rebuilt from its verdicts), and the CI scaffold; relabels the"
        " repository key when the GitHub rename changed it.",
    )
    mig.add_argument("repo", nargs="?", default=".")
    mig.add_argument("--db", default=None, help="destination working DB (default: coding-stats.db)")
    mig.add_argument(
        "--legacy-db",
        default=None,
        help="legacy working DB to reuse (default: ~/.skill-stats/skill-stats.db when present)",
    )
    mig.add_argument("--branch", default=None, help="Target branch (default: current)")
    mig.add_argument(
        "--fresh", action="store_true", help="ignore legacy artifact and DB: full recompute"
    )
    mig.add_argument("--no-classify", action="store_true", help="skip LLM classification")
    mig.add_argument(
        "--ci", action="store_true", help="also write the coding-stats CI scaffold (ci init)"
    )
    mig.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    mig.add_argument(
        "--data-branch",
        action="store_true",
        help="ADR-0006 clause 6: move the committed .coding-stats/ store onto the"
        " orphan Data branch (instead of the legacy skill-stats migration)",
    )
    args = parser.parse_args(argv)

    settings: dict[str, Any] = {}
    if args.cmd == "init":
        repo = Path(args.repo).resolve()
        if not (repo / ".git").exists():
            sys.exit(f"not a git repo: {repo}")
        db_path = new_db(args.db, args)
        conn = db.connect(db_path)
        branch = args.branch or gitwalk.current_branch(repo)
        if branch == "HEAD":
            sys.exit("detached HEAD: pass --branch <target> explicitly")
        identity.register_repository(conn, repo, branch)  # writes repositories row + repo_id
        cfg = {
            "repo": str(repo),
            "branch": branch,
            "model": args.model,
            "min_target_lines": args.min_target_lines,
        }
        for key, value in cfg.items():
            _set(conn, key, value)
        conn.close()
        print(f"initialized {db_path} for {repo} (branch {branch}, model {args.model})")
        return

    if args.cmd == "ci":
        # git-side commands: act on the repository at the current directory,
        # no working DB required
        top = identity.toplevel()
        if top is None:
            sys.exit("not inside a git repository")
        branch = args.branch or gitwalk.current_branch(top)
        if branch == "HEAD":
            sys.exit(
                "detached HEAD: pass --branch <target> explicitly"
                " (a detached checkout names no Target branch)"
            )
        if args.ci_cmd == "init":
            try:
                for msg in ci.init(
                    top,
                    branch,
                    report=not args.no_report,
                    upgrade=args.upgrade,
                    detect_pr=args.detect_pr,
                ):
                    print(msg)
            except ci.DriftError as exc:
                sys.exit(f"error: {exc}")
        else:
            try:
                store = datastore.prepare(top, branch, remote=args.remote, sync=False)
            except datastore.StoreSkip as exc:
                ci.report_landing(ci.Landing("skipped", str(exc)))
            else:
                ci.report_landing(
                    ci.land(store, top, args.remote),
                )
        return

    if args.cmd == "gather":
        from .gather import GatherError
        from .gather import gather as run_gather

        try:
            result = run_gather(Path(args.db), [Path(s) for s in args.sources])
        except GatherError as exc:
            sys.exit(f"error: {exc}")
        print(
            f"{args.db}: {result.ingested} ingested, {result.skipped} skipped (identical artifact)"
        )
        return

    if args.cmd == "migrate":
        from . import migration

        repo = Path(args.repo).resolve()
        if identity.toplevel(repo) is None:
            sys.exit(f"not a git repo: {repo}")
        branch = args.branch or gitwalk.current_branch(repo) or "main"
        if branch == "HEAD":
            sys.exit("detached HEAD: pass --branch <target> explicitly")
        if args.data_branch:
            # ADR-0006 clause 6: committed ADR-0003 store → the Data branch
            try:
                for line in migration.migrate_to_data_branch(repo, branch):
                    print(line)
            except migration.MigrationError as exc:
                sys.exit(f"error: {exc}")
            return
        try:
            plan = migration.detect(repo, args.db, args.legacy_db, args.fresh, branch)
        except migration.MigrationError as exc:
            sys.exit(f"error: {exc}")
        for line in migration.describe(plan):
            print(line)
        if args.dry_run:
            return
        try:
            for line in migration.run(plan, repo, no_classify=args.no_classify):
                print(line)
        except migration.MigrationError as exc:
            sys.exit(f"error: {exc}")
        if args.ci:
            for msg in migration.init_ci(repo, branch):
                print(msg)
        return

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)  # throwaway CI DBs nest deep
    conn = db.connect(Path(args.db))
    settings = _settings(conn)
    repo_dir: Path | None = Path(settings["repo"]) if "repo" in settings else None

    if args.cmd == "walk":
        assert repo_dir is not None, "run init first"
        n = gitwalk.walk(repo_dir, conn, settings.get("branch") or None)
        total = conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"]
        print(f"walked {n} new commits (total {total})")
    elif args.cmd == "pr-sweep":
        assert repo_dir is not None, "run init first"
        if args.skip_pr is not None or args.unskip_pr is not None:
            # operator skip/unskip: one audit-only row recorded or cleared.
            # The marker is never moved here — the next ordered run advances
            # past a marked PR when it reaches it (never jumping over earlier
            # unprocessed PRs), and the row is audit-only.
            try:
                if args.skip_pr is not None:
                    prsweep.skip_pr(conn, args.skip_pr)
                    print(f"pr-sweep: PR {args.skip_pr} marked to skip (audit-only)")
                else:
                    prsweep.unskip_pr(conn, args.unskip_pr)
                    print(f"pr-sweep: PR {args.unskip_pr} skip cleared")
            except prsweep.SweepError as exc:
                sys.exit(f"error: {exc}")
        else:
            branch = str(settings.get("branch") or "")
            try:
                res = prsweep.run(
                    conn,
                    repo_dir,
                    branch,
                    after_pr=args.after_pr,
                    full=args.full,
                    max_prs=args.max_prs,
                )
            except prsweep.SweepError as exc:
                sys.exit(f"error: {exc}")
            _report_sweep(res)
    elif args.cmd == "classify":
        assert repo_dir is not None, "run init first"
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        sid = None if args.fresh else (settings.get("classify_session_id") or None)
        session = Session(settings.get("model", DEFAULT_MODEL), sid)
        try:
            done = classify_pending(
                repo_dir, conn, settings.get("model", DEFAULT_MODEL), session, limit=args.limit
            )
        finally:
            if session.session_id:
                _set(conn, "classify_session_id", session.session_id)
        print(f"classified {len(done)} commits (session {session.session_id})")
    elif args.cmd == "derive":
        assert repo_dir is not None, "run init first"
        derived = run_derive(conn, repo_dir, int(settings.get("min_target_lines", 1)))
        print(
            f"annotated {derived.fixes_annotated} fix commits,"
            f" closure rows {derived.closure_rows}"
        )
        _print_live_lines(derived.at_sha, derived.unattributed_lines, derived.samples_written)
    elif args.cmd == "report":
        from .report import text_report

        out = text_report(conn)
        if args.json:
            from .metrics import snapshot as _snap
            from .metrics import snapshot_to_json

            print(snapshot_to_json(_snap(conn)))
        else:
            print(out)
        if args.charts:
            from .report import _charts

            paths = _charts(conn, args.out)
            for p in paths:
                print(f"wrote {p}")
        if getattr(args, "html", None):
            from .htmlreport import write_report

            html_path = write_report(conn, args.html)
            print(f"wrote {html_path}")
    elif args.cmd == "update":
        conn = _run_update(conn, update_options(args))
    conn.close()


def _pending_count(conn: sqlite3.Connection) -> int:
    """Commits without a verdict row — the classification backlog."""
    return int(
        conn.execute(
            "SELECT COUNT(*) c FROM commits c LEFT JOIN commit_verdicts v"
            " ON v.commit_id = c.id WHERE v.commit_id IS NULL"
        ).fetchone()["c"]
    )


def _print_live_lines(at_sha: str, unattributed_lines: int, samples_written: int) -> None:
    """The live-lines tail of the derive stage's report (same shape as before
    the derive consolidation)."""
    print(
        f"live lines at {at_sha[:12] or '(none)'}: unattributed"
        f" {unattributed_lines}, {samples_written} samples written"
    )


# The stop-notice policy: ONE table entry per stop reason — the notice body
# (after "pr-sweep ": its face plus message) plus its resume wording. Every
# resumable stop promises the same resume; forbidden keeps its distinct
# wording as data — a permissions failure is NOT a rate limit and does not
# resume, so its message names the missing permission and the watermark is
# only "kept". The unavailable body is its own face (no "stopped:" prefix)
# and carries the {detail} sanitization marker.
_STOP_NOTICES: dict[str, tuple[str, str]] = {
    prsweep.STOP_CAP: ("stopped: processing cap reached", ", the next run resumes past it"),
    prsource.STOP_RATE_LIMIT: ("stopped: rate limit exhausted", ", the next run resumes past it"),
    prsource.STOP_INTERRUPTED: ("stopped: interrupted", ", the next run resumes past it"),
    prsweep.STOP_WALK_STALE: (
        "stopped: waiting for the walk to catch up",
        ", the next run resumes past it",
    ),
    prsweep.STOP_FETCH: (
        "stopped: a PR head fetch failed (transient git failure)",
        ", the next run resumes past it",
    ),
    prsource.STOP_FORBIDDEN: (
        "stopped: the GitHub token cannot read pull requests (403 Resource"
        " not accessible by integration) — grant `pull-requests: read`",
        " kept",
    ),
    prsweep.STOP_UNAVAILABLE: ("unavailable{detail}", ", the next run resumes past it"),
}


def _report_sweep(res: prsweep.SweepResult) -> None:
    """The pr-sweep outcome, printed once per run — shared by the `pr-sweep`
    verb and the update verb's PR tier so both faces report identically."""
    print(
        f"pr-sweep: {res.processed} PRs recorded ({res.rows} rows,"
        f" {res.suspicious} suspicious), {res.requests} API requests,"
        f" watermark {res.watermark}"
    )
    if res.stop is not None:
        # the one sanitization point (only unavailable's body carries
        # {detail}): exception text is untrusted, potentially multiline
        # input to a workflow command — keep the notice on one line
        detail = " ".join(
            "".join(ch if ch.isprintable() else " " for ch in (res.detail or "")).split()
        )
        suffix = f" ({detail})" if detail else ""
        body, resume = _STOP_NOTICES[res.stop]
        print(f"::notice::pr-sweep {body.format(detail=suffix)}; watermark {res.watermark}{resume}")


def _run_update(
    conn: sqlite3.Connection, options: UpdateOptions, layout: StoreLayout = StoreLayout.DATA_BRANCH
) -> sqlite3.Connection:
    """One orchestrating run (ADR-0003 clause 7, Q11): verify the existing
    artifact first, then walk -> classify -> derive -> serialize + digest +
    verify (no report stage since ADR-0006: the store holds only the SQL
    artifact; reports render via the report verb/action).
    Deterministic stages always run;
    classification is best-effort — when it cannot run the walked backlog stays
    pending, is printed loudly, and reprocesses on a later successful update
    (Q13). The exit code stays 0 with a backlog.

    `layout` tells the run which committed store it executes against: the
    default Data branch, or StoreLayout.LEGACY_COMMITTED when the migrate
    verb regenerates the store ON the Target checkout and the Data-branch
    skips must not apply.

    ADR-0006 clause 1: the store is the Data branch `coding-stats/<target>` —
    prepare mounts its worktree, and the artifact is verified / rebuilt /
    written there; `ci land` pushes it while the Target branch never
    receives artifact commits. Layout skips (a Target branch under
    coding-stats/, a Data-branch ref-path clash, a legacy committed store
    with no Data branch) end with a neutral notice instead of silently
    re-classifying.

    On digest MISMATCH the run recovers per ADR-0003 clause 3 (partial
    recompute anchored at the newest self-verifying committed artifact
    version — found in the Data branch's history now; full recompute when
    none verifies). A fresh/throwaway working DB (CI runs are stateless — the
    artifact is the store) adopts the git repo at the current directory and
    seeds itself from a verified artifact.

    ADR-0007 clause 1 (ticket #37): the opt-in PR tier — ``--detect-pr``
    runs the watermark-driven PR-channel increment after the deterministic
    stages and before serialization (so the evidence rows and the watermark
    ride the artifact), ``--pr-backfill full`` is the BATCHED backfill face:
    oldest-unincorporated history past the shared watermark at
    ``DEFAULT_BACKFILL_CAP`` PRs per invocation, each run landing a disjoint
    chunk (never a restart at PR 1, never a re-sweep below the marker — the
    direct ``pr-sweep --full`` face owns that re-sweep) until history is
    caught up. Detection-only: no LLM calls. The off
    default runs exactly this pipeline, unchanged."""
    import os as _os

    from . import artifact, derive, recovery

    db_path = Path(options.db)
    settings = _settings(conn)
    if "repo" in settings:
        repo_dir = Path(str(settings["repo"]))
    else:
        top = identity.toplevel()
        if top is None:
            sys.exit(
                "no repo recorded in the DB and the current directory is not a"
                " git repository; run init first"
            )
        repo_dir = top
        _set(conn, "repo", str(repo_dir))
        settings["repo"] = str(repo_dir)
    branch = str(settings.get("branch") or "") or (
        _os.environ.get("GITHUB_REF_NAME") or gitwalk.current_branch(repo_dir)
    )
    if "branch" not in settings:
        _set(conn, "branch", branch)
        settings["branch"] = branch
    identity.register_repository(conn, repo_dir, branch)

    if layout is StoreLayout.LEGACY_COMMITTED:
        # the ADR-0005 migration verb regenerates the store ON the Target
        # checkout (legacy layout); the skips below apply only to the
        # default Data-branch layout
        store_dir = repo_dir
        art_path = repo_dir / ".coding-stats" / "coding-stats.sql"
    else:
        try:
            store = datastore.prepare(repo_dir, branch)
        except datastore.StoreSkip as exc:
            print(f"::notice::coding-stats update skipped: {exc}")
            ci.step_summary(f"coding-stats update skipped: {exc}")
            print("update skipped; nothing was walked, classified, or landed")
            return conn
        store_dir = store.worktree
        art_path = store_dir / ".coding-stats" / "coding-stats.sql"
    legacy_path = repo_dir / ".coding-stats" / "coding-stats.sql"
    recovered = False
    if not art_path.exists():
        if layout is StoreLayout.DATA_BRANCH and legacy_path.exists():
            exposed = "coding-stats update skipped: the Target branch carries a committed"
            msg = (
                f"::notice::{exposed} .coding-stats/ store (ADR-0003 layout) but its Data"
                f" branch {store.data_branch} has no artifact yet, so updates would"
                " silently re-classify the whole history. Run `coding-stats migrate`"
                " to move the committed artifact onto the Data branch with its"
                " classified history, or delete the Target branch's .coding-stats/"
                " to start coverage fresh."
            )
            print(msg)
            ci.step_summary(f"### {exposed} artifact — migration needed")
            print("update skipped; nothing was walked, classified, or landed")
            return conn
        print("artifact: none yet (first run)")
    else:
        err = artifact.verify(art_path)
        if err is None:
            print("artifact digest: ok")
            if not conn.execute("SELECT 1 FROM commits LIMIT 1").fetchone():
                # fresh/throwaway working DB: the artifact is the store —
                # continue from it instead of re-analyzing everything
                conn = recovery.rebuild(db_path, art_path.read_bytes(), conn, repo_dir, branch)
                settings = _settings(conn)
                print("seeded working db from the artifact")
        else:
            print(f"artifact digest: MISMATCH ({err})")
            anchor = (
                f"refs/heads/{branch}"
                if layout is StoreLayout.LEGACY_COMMITTED
                else datastore.anchor_ref(repo_dir, store.data_branch)
            )
            rec, conn = recovery.recover(conn, db_path, repo_dir, branch, anchor)
            settings = _settings(conn)
            recovered = True
            if rec.mode == "restored":
                assert rec.anchor_sha is not None
                print(f"partial recompute: anchored at artifact version {rec.anchor_sha[:12]}")
            else:
                print(
                    "FULL-RECOMPUTE: no committed artifact version verifies;"
                    " rebuilding from scratch"
                )

    n = gitwalk.walk(repo_dir, conn, branch)
    print(f"walked {n} new commits")

    if not options.no_classify:
        if layout is StoreLayout.DATA_BRANCH:
            from .seeding import seed_from_siblings

            seed = seed_from_siblings(conn, repo_dir, store.data_branch)
            if seed.seeded:
                print(f"seeded {seed.seeded} commits from {', '.join(seed.sources)}")
        pending_before = _pending_count(conn)
        try:
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            sid = settings.get("classify_session_id") or None
            session = Session(settings.get("model", DEFAULT_MODEL), sid)
            try:
                done = classify_pending(
                    repo_dir, conn, settings.get("model", DEFAULT_MODEL), session
                )
            finally:
                if session.session_id:
                    _set(conn, "classify_session_id", session.session_id)
            print(f"classified {len(done)} commits")
        except (OSError, RuntimeError, ValueError) as exc:
            # ValueError covers bad model replies reaching apply_reply (cited
            # unknown feature ids; JSONDecodeError is one). classify_pending
            # aborts mid-loop: what landed stays counted, the raising commit
            # and the rest wait for a later update (Q13), and nothing below is
            # blocked. The `classify` plumbing verb keeps raising.
            processed = pending_before - _pending_count(conn)
            print(
                f"WARNING: classification unavailable ({exc}); {processed} classified"
                " before the abort, the rest stay pending and reprocess on a later"
                " update"
            )

    # The derive stage (ADR-0008): one deterministic pass rebuilding the
    # blame-derived state — fix attribution, lineage closure, the tip's
    # live-lines accounting. The derived-only tables are fully rebuilt each
    # run, so a raised min_target_lines drops the targets that fall below
    # it. Runs after classify and before anything consumes it (the PR tier
    # below).
    derived = derive.run_derive(
        conn,
        repo_dir,
        int(settings.get("min_target_lines", 1)),
        attribution=not (options.no_detect or options.no_lineage),
    )
    print(f"annotated {derived.fixes_annotated} fix commits, closure rows {derived.closure_rows}")
    _print_live_lines(derived.at_sha, derived.unattributed_lines, derived.samples_written)

    # ADR-0007 clause 1 (ticket #37): the opt-in PR tier runs as the last
    # stage before serialization, so its evidence rows and the watermark
    # ride the same artifact the landing pushes — and the off default never
    # enters it at all (no API call, no disclosure).
    if options.detect_pr or options.pr_backfill:
        try:
            if options.pr_backfill:
                # UPDATE's --pr-backfill full: the BATCHED backfill face —
                # DEFAULT_BACKFILL_CAP PRs of oldest-unincorporated history
                # per invocation (never a restart at PR 1, never a re-sweep
                # below the marker: each dispatch continues past the landed
                # chunk until history is caught up). The named cap is the
                # update path's per-invocation bound.
                res = prsweep.sweep_backfill(
                    conn, repo_dir, branch, max_prs=prsweep.DEFAULT_BACKFILL_CAP
                )
            else:
                res = prsweep.run(conn, repo_dir, branch)
        except prsweep.SweepError as exc:
            # best-effort like classification (Q13): a configuration the PR
            # tier cannot run against (no GitHub origin, no Target branch)
            # never fails the deterministic pipeline. Expected operational
            # failures never reach here — prsweep.run converts them into a
            # STOP_UNAVAILABLE result, reported below without cli catching
            # any exception family. KeyboardInterrupt is deliberately NOT
            # caught — an interrupt is the sweep's own stop condition (and
            # otherwise stays the operator's).
            print(
                f"WARNING: pr-sweep unavailable ({exc}); no PR-channel evidence"
                " recorded this run — a later update resumes past the watermark"
            )
        else:
            _report_sweep(res)
        print(f"::notice::{aiattr.DISCLOSURE_LINE}")

    if not options.no_serialize:
        path = artifact.write(conn, store_dir)
        err = artifact.verify(path)
        print(f"wrote {path} (digest {'ok' if err is None else 'MISMATCH ' + str(err)})")
        if recovered:
            print(f"new coverage horizon: {artifact.covered_through(conn)[:12]}")

    total = conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"]
    classified = conn.execute("SELECT COUNT(*) c FROM commit_verdicts").fetchone()["c"]
    pending = int(total) - int(classified)
    suffix = "artifact updated" if not options.no_serialize else "artifact not written"
    print(f"{total} walked, {classified} classified, {pending} pending, {suffix}")
    if pending:
        print(
            f"WARNING: {pending} commits unclassified — backlog; a later successful"
            " update reprocesses them"
        )
    return conn


if __name__ == "__main__":
    main()
