"""CI scaffold (ADR-0006 clause 5): the caller workflow and Data-branch
artifact landing.

``ci init`` writes the short caller workflow (``.github/workflows/
coding-stats.yml``) that adopts the composite actions ``Dahep/
coding-stats@v1`` (update; lands on the Data branch) and ``Dahep/
coding-stats/report@v1`` — idempotently: identical content is a no-op
assertion, drifted content refuses with a diff summary unless
``--upgrade``. The rendered-engine workflow, its git-pin rendering and
the Pages job are gone: the engine version is the action ref the caller
pins, and reports belong to the report action.

``ci land`` is the push-side verb the update action calls: it keeps the
landing rules — Q15 -> a distinct artifact-update commit, Q17 ->
GITHUB_TOKEN so no PAT, Q18 -> no-op/stale/abandon — in Python where
they are engine-testable instead of inline shell.  ADR-0006 retargeted
it: the landing commits the `.coding-stats/` store of the Data-branch
worktree (mounted by ``datastore.prepare``) and pushes HEAD to exactly the
Data branch ``coding-stats/<target>`` — the Target branch never receives bot
commits.  It may be run anywhere (no CI-env restriction documented by
choice)."""

import difflib
import os
import subprocess
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from . import datastore

WORKFLOW_REL = ".github/workflows/coding-stats.yml"
SUBJECT = "coding-stats artifact update (auto)"
BOT_NAME = "coding-stats[bot]"
BOT_EMAIL = "coding-stats[bot]@users.noreply.github.com"
_STORE_PATH = ".coding-stats"


class DriftError(RuntimeError):
    """The on-disk file differs from what this version generates."""


@dataclass(frozen=True)
class Landing:
    """The landing outcome. The verb itself stays silent: reporting is the
    CLI's job, so the status is printed exactly once."""

    status: str  # 'landed' | 'noop' | 'abandoned' | 'skipped'
    detail: str


def render_workflow(branch: str, report: bool = True) -> str:
    """The CALLER workflow adopted in a Target repo (ADR-0006 clause 5):
    a `uses: Dahep/coding-stats@v1` update job (which lands on the Data
    branch `coding-stats/<branch>`) plus a report job triggered on the
    cadence the caller keeps scheduling. Tracked branches are the
    workflow's own `on.push.branches` filter."""
    b = _yaml_scalar(branch)
    report_job = """
  # Reports render on their own cadence (the store holds only the SQL
  # artifact). This job's permissions: checkout/fetch need contents: read;
  # the Pages scopes belong HERE only — the update job never gets them, and
  # they matter only when deploy-pages is "true".
  report:
    needs: update
    if: ${{ always() && needs.update.result == 'success' }}
    runs-on: ubuntu-latest
    concurrency:
      group: coding-stats-report
      cancel-in-progress: false
    permissions:
      contents: read
      pages: write       # needed only for deploy-pages: "true"
      id-token: write    # needed only for deploy-pages: "true"
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: Dahep/coding-stats/report@v1
        with:
          deploy-pages: "false"  # flip to "true" to publish to GitHub Pages
"""
    return f"""# coding-stats CI — installed with `coding-stats ci init`;
# upgradable by moving the action ref (manually or via Dependabot).
name: coding-stats

on:
  push:
    branches:
      - {b}

permissions: {{}}

jobs:
  # One `uses:` step after checkout — the action pins the engine version to
  # its own checkout, walks, classifies, and lands the Artifact on the
  # Data branch coding-stats/<branch>; the Target branch is never touched.
  update:
    runs-on: ubuntu-latest
    # The same branch's runs never overlap; a superseded run abandons at
    # landing (never mid-classification, never red).
    concurrency:
      group: coding-${{{{ github.workflow }}}}-${{{{ github.ref_name }}}}
      cancel-in-progress: false
    permissions:
      contents: write  # the landing commit is the only write (GITHUB_TOKEN)
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # walk, blame and partial recompute need full history
      # Work repos: pin the commit sha instead of the moving tag — this job
      # holds contents: write and LLM keys.
      - uses: Dahep/coding-stats@v1
        with:
          opencode-api-key: ${{{{ secrets.OPENCODE_API_KEY }}}}
          openrouter-api-key: ${{{{ secrets.OPENROUTER_API_KEY }}}}
{report_job if report else ""}"""


def tool_version() -> str:
    """This package's own distribution version (fallback pin when the tool
    runs from an installed copy, not a coding-stats checkout)."""
    return metadata.version("coding-stats")


def _yaml_scalar(value: str) -> str:
    """Single-quoted YAML scalar: always a string ('true' never a bool), with
    '' doubling for embedded quotes."""
    return "'" + value.replace("'", "''") + "'"


def init(
    repo_dir: Path,
    branch: str,
    report: bool = True,
    upgrade: bool = False,
) -> list[str]:
    """Write the caller workflow. Identical content is an idempotent
    assertion ('up to date'); drifted content refuses with a diff summary
    unless `upgrade` re-renders over it. Drift is preflighted before
    anything is written — a refusal never leaves a half-written file."""
    datastore.validate_ref(branch)
    plan = [
        (WORKFLOW_REL, render_workflow(branch, report=report)),
    ]
    drifted = []
    for rel, content in plan:
        path = repo_dir / rel
        if path.exists():
            current = path.read_text(encoding="utf-8")
            if current != content:
                drifted.append((rel, current, content))
    if drifted and not upgrade:
        raise DriftError(
            "differs from what this version generates; re-run with --upgrade to"
            " overwrite:\n" + "\n".join(_diff(rel, cur, new) for rel, cur, new in drifted)
        )
    out: list[str] = []
    for rel, content in plan:
        path = repo_dir / rel
        if path.exists() and path.read_text(encoding="utf-8") == content:
            out.append(f"{rel}: up to date")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        out.append(f"{rel}: written")
    return out


def report_landing(landing: Landing) -> None:
    """Print the landing outcome exactly once (the CLI's reporting step):
    neutral ::notice:: annotation + step summary for abandonment and skips
    (Q18/ADR-0006: never a red failure for superseded or skipped runs), a
    plain line otherwise."""
    line = f"coding-stats ci land: {landing.status} — {landing.detail}"
    if landing.status in ("abandoned", "skipped"):
        print(f"::notice::{line}")
    else:
        print(line)
    step_summary(line)


def step_summary(line: str) -> None:
    """Append one line to the GITHUB_STEP_SUMMARY (noop outside Actions)."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### {line}\n")


def land(store: datastore.DataStore, repo: Path, remote: str = "origin") -> Landing:
    """The landing verb, retargeted to the Data branch (ADR-0006 clause 1 —
    the ADR-0003 rules Q15/Q17/Q18 stand):

    'noop' — the store worktree's .coding-stats/ is unchanged (nothing is
    pushed, so unchanged data never lands);
    'skipped' — the caller refused the Target branch (under coding-stats/ or
    a Data-branch ref-path clash): a neutral notice, never a red run;
    'abandoned' — the Data branch tip moved under this run since it was read
    (another run landed first, including the check-to-push race) or the
    history diverged (never force); a run whose *Target* moved on is judged
    on the Data branch alone and still lands its cumulative Artifact;
    'landed' — the artifact update is committed as coding-stats[bot] in the
    Data-branch worktree (its parent is the previous Data tip, or none for
    the first commit of a fresh orphan branch) and pushed to exactly
    refs/heads/<data-branch>. Unrelated push failures (auth, network) raise.

    Identity: `store.target` stays the Repository key's branch — the Data
    branch never becomes one. Remote ops run from `repo` (the main checkout)
    because remote URLs may be relative; only index/worktree ops run inside
    the worktree."""
    if not _store_dirty(store.worktree):
        return Landing("noop", "no artifact changes to land")
    base = _head(store.worktree)  # None on an unborn (first-run) branch
    remote_ok = datastore.remote_url(repo, remote) is not None
    tip = datastore.fetch_tip(repo, store.data_branch, remote) if remote_ok else None
    if tip is not None and tip != base:
        if base is None or _is_ancestor(repo, base, tip):
            return Landing(
                "abandoned",
                f"a newer push landed on {store.data_branch} while this run worked;"
                " its own run covers the data",
            )
        if not _is_ancestor(repo, tip, base):
            return Landing(
                "abandoned", f"history diverged from {remote}/{store.data_branch}; never force"
            )
    _git(store.worktree, "add", "-A", "--", _STORE_PATH)
    _git(
        store.worktree,
        "-c",
        f"user.name={BOT_NAME}",
        "-c",
        f"user.email={BOT_EMAIL}",
        "commit",
        "-m",
        SUBJECT,
        "--",
        _STORE_PATH,
    )
    head = _git(store.worktree, "rev-parse", "HEAD").strip()
    if not remote_ok:
        return Landing("landed", f"{store.data_branch} committed locally; no remote to push")
    try:
        _push(repo, remote, store.data_branch, head)
    except subprocess.CalledProcessError as exc:
        # Q18 race: the Data branch advanced between our staleness check and
        # this push and the push was rejected. Superseded runs end neutrally
        # — never force, never block; anything else (auth, network) is a real
        # error and must surface.
        tip = datastore.fetch_tip(repo, store.data_branch, remote)
        if tip is not None and (base is None or not _is_ancestor(repo, tip, base)):
            return Landing(
                "abandoned",
                f"a newer push landed on {store.data_branch} during the landing attempt;"
                " superseded (the local artifact commit stays unpushed)",
            )
        raise exc
    return Landing("landed", f"artifact-update commit pushed to {remote} {store.data_branch}")


def _push(repo: Path, remote: str, branch: str, head: str) -> None:
    # by sha, not HEAD: HEAD inside the worktree is the landing commit, but
    # remote URLs may be relative and resolve against the main checkout
    _git(repo, "push", remote, f"{head}:refs/heads/{branch}")


def _diff(rel: str, current: str, generated: str) -> str:
    lines = list(
        difflib.unified_diff(
            current.splitlines(),
            generated.splitlines(),
            fromfile=f"{rel} (on disk)",
            tofile=f"{rel} (generated)",
            lineterm="",
        )
    )
    cap = 40
    if len(lines) > cap:
        lines = lines[:cap] + ["... (diff truncated)"]
    return "\n".join(lines)


_git = datastore.git_run
_store_dirty = datastore.store_dirty
_head = datastore.head_sha
_is_ancestor = datastore.is_ancestor
