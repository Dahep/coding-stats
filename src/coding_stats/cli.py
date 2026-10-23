"""CLI: uv run coding-stats {init,walk,classify,detect,lineage,report,update,ci,gather,migrate}.
Requires the
opencode CLI on PATH for classify (ADR-0001 rolling session). update is the
orchestrating verb (ADR-0003 clause 7); the stage subcommands stay plumbing;
ci renders the workflow and lands artifact updates; migrate moves a legacy
skill-stats layout to coding-stats (ADR-0005)."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import ci, datastore, db, gitwalk, identity
from .classify import DEFAULT_MODEL, SESSION_DIR, Session, classify_pending
from .lineage import close_lineage
from .targets import annotate_all


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

    det = sub.add_parser("detect", help="blame evidence -> fix_touches/fix_targets")
    det.add_argument("--db", default="coding-stats.db")

    lin = sub.add_parser("lineage", help="fixes_features transitive closure")
    lin.add_argument("--db", default="coding-stats.db")

    rep = sub.add_parser("report", help="metrics text summary + optional charts")
    rep.add_argument("--db", default="coding-stats.db")
    rep.add_argument("--charts", action="store_true")
    rep.add_argument("--html", nargs="?", const="coding-stats-report.html", default=None)
    rep.add_argument("--out", default="coding-stats-report")
    rep.add_argument("--json", action="store_true")

    up = sub.add_parser(
        "update",
        help="verify -> walk -> classify -> detect -> lineage -> live-lines ->"
        " report -> serialize + digest",
    )
    up.add_argument("--db", default="coding-stats.db")
    up.add_argument("--no-classify", action="store_true", help="skip LLM classification")
    up.add_argument("--no-detect", action="store_true", help="skip blame-based fix detection")
    up.add_argument("--no-lineage", action="store_true", help="skip lineage closure")
    up.add_argument("--no-report", action="store_true", help="skip report.html regeneration")
    up.add_argument("--no-serialize", action="store_true", help="skip artifact write")

    ci_grp = sub.add_parser("ci", help="CI scaffold: workflow rendering + artifact landing")
    ci_sub = ci_grp.add_subparsers(dest="ci_cmd", required=True)
    ci_init = ci_sub.add_parser(
        "init", help="write .github/workflows/coding-stats.yml (idempotent, refuses drift)"
    )
    ci_init.add_argument("--branch", default=None, help="Target branch (default: current)")
    ci_init.add_argument(
        "--db", default=ci.DEFAULT_CI_DB, help="working DB the workflow runs update against"
    )
    ci_init.add_argument("--no-push", action="store_true", help="render without the landing step")
    ci_init.add_argument("--upgrade", action="store_true", help="overwrite a drifted workflow")
    ci_init.add_argument("--pages", action="store_true", help="publish report.html to GitHub Pages")
    ci_init.add_argument(
        "--tool-ref",
        default=None,
        help="git ref to install the tool from (default: this checkout's commit)",
    )
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

    args = parser.parse_args(argv)

    settings: dict[str, Any] = {}
    if args.cmd == "init":
        repo = Path(args.repo).resolve()
        if not (repo / ".git").exists():
            sys.exit(f"not a git repo: {repo}")
        db_path = new_db(args.db, args)
        conn = db.connect(db_path)
        branch = args.branch or gitwalk.current_branch(repo)
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
        if args.ci_cmd == "init":
            try:
                for msg in ci.init(
                    top,
                    branch,
                    db=args.db,
                    push=not args.no_push,
                    upgrade=args.upgrade,
                    tool_ref=args.tool_ref,
                    pages=args.pages,
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
    elif args.cmd == "detect":
        assert repo_dir is not None, "run init first"
        n_fixes = annotate_all(repo_dir, conn, int(settings.get("min_target_lines", 1)))
        print(f"annotated {n_fixes} fix commits")
    elif args.cmd == "lineage":
        n_rows = close_lineage(conn)
        rows_now = conn.execute("SELECT COUNT(*) c FROM fixes_features").fetchone()["c"]
        print(f"closure rows now {rows_now} (+{n_rows})")
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
        conn = _run_update(conn, Path(args.db), args)
    conn.close()


def _pending_count(conn: sqlite3.Connection) -> int:
    """Commits without a verdict row — the classification backlog."""
    return int(
        conn.execute(
            "SELECT COUNT(*) c FROM commits c LEFT JOIN commit_verdicts v"
            " ON v.commit_id = c.id WHERE v.commit_id IS NULL"
        ).fetchone()["c"]
    )


def _run_update(
    conn: sqlite3.Connection, db_path: Path, args: argparse.Namespace, legacy_store: bool = False
) -> sqlite3.Connection:
    """One orchestrating run (ADR-0003 clause 7, Q11): verify the existing
    artifact first, then walk -> classify -> detect -> lineage -> live-lines ->
    report -> serialize + digest + verify. Deterministic stages always run;
    classification is best-effort — when it cannot run the walked backlog stays
    pending, is printed loudly, and reprocesses on a later successful update
    (Q13). The exit code stays 0 with a backlog.

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
    seeds itself from a verified artifact."""
    import os as _os

    from . import artifact, livelines, recovery
    from .htmlreport import write_report

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

    if legacy_store:
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
            ci.step_summary(f"### coding-stats update skipped: {exc}")
            print("update skipped; nothing was walked, classified, or landed")
            return conn
        store_dir = store.worktree
        art_path = store_dir / ".coding-stats" / "coding-stats.sql"
    legacy_path = repo_dir / ".coding-stats" / "coding-stats.sql"
    recovered = False
    if not art_path.exists():
        if not legacy_store and legacy_path.exists():
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
                if legacy_store
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

    if not args.no_classify:
        from .seeding import seed_from_siblings

        if not legacy_store:
            seed = seed_from_siblings(conn, repo_dir, store.data_branch)
            if seed.seeded:
                print(f"seeded {seed.seeded} commits from {', '.join(seed.sources)}")

    if not args.no_classify:
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

    if not args.no_detect:
        n_fixes = annotate_all(repo_dir, conn, int(settings.get("min_target_lines", 1)))
        print(f"annotated {n_fixes} fix commits")
    if not args.no_lineage:
        close_lineage(conn)
        print("lineage closure updated")

    live = livelines.update_live_lines(conn, repo_dir)
    print(
        f"live lines at {live.at_sha[:12] or '(none)'}: unattributed"
        f" {live.unattributed_lines}, {live.samples_written} samples written"
    )

    if not args.no_report:
        report_path = write_report(conn, store_dir / ".coding-stats" / "report.html")
        print(f"wrote {report_path}")
    if not args.no_serialize:
        path = artifact.write(conn, store_dir, with_report=False)
        err = artifact.verify(path)
        print(f"wrote {path} (digest {'ok' if err is None else 'MISMATCH ' + str(err)})")
        if recovered:
            print(f"new coverage horizon: {artifact.covered_through(conn)[:12]}")

    total = conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"]
    classified = conn.execute("SELECT COUNT(*) c FROM commit_verdicts").fetchone()["c"]
    pending = int(total) - int(classified)
    suffix = "artifact updated" if not args.no_serialize else "artifact not written"
    print(f"{total} walked, {classified} classified, {pending} pending, {suffix}")
    if pending:
        print(
            f"WARNING: {pending} commits unclassified — backlog; a later successful"
            " update reprocesses them"
        )
    return conn


if __name__ == "__main__":
    main()
