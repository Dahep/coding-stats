"""The Data-branch store (ADR-0006 clause 1): the Artifact lives on an orphan
branch ``coding-stats/<target>``, never on the Target branch.

One Data branch per tracked Target branch keeps the Target branch's history
free of bot commits and lets rulesets that require PRs into ``main`` adopt
coding-stats. Each Data branch is analyzed and landed independently; the
landing rules (no-op, never force, neutral abandon) stay in ``ci land`` and
operate on this store.

The store is a throwaway git worktree of the Data branch, kept inside the
Target checkout's own ``$GIT_DIR`` (machine-local, invisible to the walk's
history, uses ``git worktree add -b``/``--orphan``; unborn HEAD is tolerated
everywhere because the landing verb commits the first orphan commit).  Note
that remotes may be RELATIVE urls: they resolve against the main checkout, so
every network operation (fetch/push/ls-remote) runs from the main checkout by
sha, and only index/worktree operations run inside the worktree.

``prepare`` is the single entry both ``update`` and ``ci land`` use: a
missing Data branch behaves as "no artifact yet" (first run — the landing
verb's first commit creates the orphan branch), an existing remote Data
branch is checked out / synced to its tip. A Target branch *under*
``coding-stats/`` or a Data-branch name that would clash with another one as
a ref path (``coding-stats/release`` vs ``coding-stats/release/2.x``) is a
neutral skip (``StoreSkip``), printed as a notice, never a red run.
"""

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

CODING_DATA_PREFIX = "coding-stats/"

# machine-local store root inside the checkout's .git dir (default_worktree)
STORE_DIR = "coding-stats-store"

# git/GitHub ref-name rules (check-ref-format); shared with ci's scaffold
# rendering — a Target branch name must be a valid ref, and the composed
# Data-branch name must survive as a ref too.
_INVALID_REF_RE = re.compile(r"[\x00-\x1f\x7f ~^:?*\[\\]")


class StoreSkip(RuntimeError):
    """The run for this Target branch is a neutral skip, not a failure.

    Raised for the two compound layouts git cannot express (a Target branch
    under coding-stats/, a Data branch clashing with another as a ref path);
    callers print the message as a notice and do nothing else."""


@dataclass(frozen=True)
class DataStore:
    """Where one Target branch's artifact lives during a run."""

    target: str  # the identity branch; a Repository key never references this
    data_branch: str  # refs/heads/<data_branch>, e.g. coding-stats/main
    worktree: Path  # checked out (or unborn) at the Data branch; differs on no tip


def validate_ref(branch: str) -> None:
    """Ref-name rules of git/GitHub (check-ref-format). Refuses names git
    itself would reject."""
    bad = (
        not branch
        or bool(_INVALID_REF_RE.search(branch))
        or ".." in branch
        or "@{" in branch
        or branch == "@"
        or branch.startswith("/")
        or branch.endswith("/")
        or "//" in branch
        or branch.endswith(".")
        or branch.endswith(".lock")
        or any(p.startswith(".") or p.endswith(".lock") for p in branch.split("/"))
    )
    if bad:
        raise ValueError(f"invalid branch name {branch!r}: not a valid git/GitHub ref")


def data_branch(target: str) -> str:
    """refs/heads/ branch name holding `target`'s Artifact (raw name after
    the prefix: ``release/2.x`` → ``coding-stats/release/2.x``)."""
    validate_ref(target)
    return CODING_DATA_PREFIX + target


def is_store_target(target: str) -> bool:
    """True when the Target branch itself sits under coding-stats/ — it would
    be a Data branch of itself; skipped with a notice, never analyzed."""
    return target.startswith(CODING_DATA_PREFIX)


def default_worktree(repo: Path, target: str) -> Path:
    """The machine-local worktree path for `target`'s Data branch: inside the
    checkout's .git dir (never part of the tree, invisible to the walk and to
    `git status`). Linked-worktree checkouts carry a .git FILE — the store
    then lives under the platform temp dir keyed by the checkout, still
    stable enough for update → land to share one prepared worktree."""
    git_dir = Path(repo) / ".git"
    if git_dir.is_dir():
        return git_dir / STORE_DIR / target
    key = hashlib.sha256(str(Path(repo).resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / STORE_DIR / f"{Path(repo).name}-{key}" / target


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True
    ).stdout


def remote_url(repo: Path, remote: str = "origin") -> str | None:
    """The configured remote URL, or None when the remote does not exist."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", remote],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def remote_data_refs(repo: Path, remote: str = "origin") -> dict[str, str]:
    """{refname: sha} for every coding-stats/* Data branch the checkout KNOWS
    about: the remote-tracking refs (<remote>/coding-stats/*, freshly fetched
    by the update action) plus local Data branches. The engine does no remote
    I/O here — a Data branch never fetched behaves as 'no artifact yet'
    (first run), and the ls-remote that would add networking is ci land's
    job. {} when neither exists."""
    out = _git(
        repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/heads/" + CODING_DATA_PREFIX,
        f"refs/remotes/{remote}/" + CODING_DATA_PREFIX,
    )
    refs: dict[str, str] = {}
    local_prefix = "refs/heads/"
    tracking_prefix = f"refs/remotes/{remote}/"
    # tracking refs first: the freshest known state of a Data branch is the
    # fetched one; a locally-ahead (never-landed) tip must not win sync
    for line in out.splitlines():
        refname, _, sha = line.partition(" ")
        if refname.startswith(tracking_prefix + CODING_DATA_PREFIX):
            refs[refname[len(tracking_prefix) :]] = sha.strip()
    for line in out.splitlines():
        refname, _, sha = line.partition(" ")
        if refname.startswith(local_prefix + CODING_DATA_PREFIX):
            name = refname[len(local_prefix) :]
            refs.setdefault(name, sha.strip())
    return refs


def remote_tip(repo: Path, data_branch: str, remote: str = "origin") -> str | None:
    """The remote Data branch's tip sha, or None when it does not exist."""
    return remote_data_refs(repo, remote).get(data_branch)


def fetch_tip(repo: Path, data_branch: str, remote: str = "origin") -> str | None:
    """(sha | None) after fetching the Data branch tip into
    refs/remotes/origin/<data_branch> — ancestry checks then work on local
    objects even though the landing HEAD lives in the worktree. None when
    the remote does not hold the branch; unreachable remotes raise (ci
    land's network step, a real failure there)."""
    out = _git(repo, "ls-remote", remote, f"refs/heads/{data_branch}")
    tip = out.strip().split("\t")[0] or None
    if tip is None:
        return None
    _git(repo, "fetch", remote, f"+refs/heads/{data_branch}:refs/remotes/{remote}/{data_branch}")
    return tip


def anchor_ref(repo: Path, data_branch: str, remote: str = "origin") -> str | None:
    """The ref whose history anchor searches walk for the partial-recompute
    recovery: the Data branch (its tip's own history), local branch first so
    never-yet-pushed local landings anchor too, then the fetched remote ref.
    None under no Data branch — full recompute, the safe side. Scoped on
    purpose: the walk sees exactly this one branch."""
    for ref in (f"refs/heads/{data_branch}", f"refs/remotes/{remote}/{data_branch}"):
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            return ref
    return None


def _registered(repo: Path) -> set[Path]:
    out = _git(repo, "worktree", "list", "--porcelain")
    paths = {line[len("worktree ") :] for line in out.splitlines() if line.startswith("worktree ")}
    return {Path(p).resolve() for p in paths}


def _head(worktree: Path) -> str | None:
    """The worktree's HEAD sha, or None on an unborn (orphan) branch."""
    proc = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--verify", "--quiet", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
    )
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _branch_born(repo: Path, branch: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def _discard_worktree(repo: Path, worktree: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
        capture_output=True,
        check=False,
    )
    shutil.rmtree(worktree, ignore_errors=True)


def _dirty(worktree: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain", "--", ".coding-stats/"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


def prepare(
    repo: Path,
    target: str,
    remote: str = "origin",
    sync: bool = True,
    worktree: Path | None = None,
) -> DataStore:
    """Mount (or reuse) the Data-branch worktree for `target`, anchored at the
    remote tip when one exists. sync=True (the update's read point) re-anchors
    an existing worktree on the fetched tip — land rides on the same worktree
    with sync=False so its landing check sees the run's own read state.

    Raises StoreSkip for the two neutral skips; ValueError for a hostile
    Target name; CalledProcessError for real remote/git failures."""
    branch = data_branch(target)  # authoritative ref validation
    if is_store_target(target):
        raise StoreSkip(
            f"target branch {target!r} sits under {CODING_DATA_PREFIX!r}: it is a"
            " Data branch, so it cannot be a Target branch at the same time"
        )
    wt = worktree if worktree is not None else default_worktree(repo, target)
    refs = remote_data_refs(repo, remote)  # {} when the remote is not configured
    clash = next(
        (
            name
            for name in refs
            if name != branch and (name.startswith(branch + "/") or branch.startswith(name + "/"))
        ),
        None,
    )
    if clash:
        raise StoreSkip(
            f"Data branch {branch!r} clashes as a ref path with {clash!r}; pick"
            " branch names that never nest inside each other after the"
            " coding-stats/ prefix"
        )
    tip = refs.get(branch)

    wt_resolve = wt.resolve()
    known = wt_resolve in {p.resolve() for p in _registered(repo)}
    if not known and wt.exists():
        # stale leftovers, registered no more: the store is fully derivable
        # from the working DB + git, so clear them out (and the emptied
        # parent) before re-mounting
        shutil.rmtree(wt)
        if wt.parent != Path(repo) and not any(wt.parent.iterdir()):
            shutil.rmtree(wt.parent, ignore_errors=True)

    if not known:
        wt.parent.mkdir(parents=True, exist_ok=True)
        if tip is not None:
            # the tip's objects are already in this checkout (local Data
            # branch, or the remote-tracking ref the update action fetched)
            if _branch_born(repo, branch):
                subprocess.run(
                    ["git", "-C", str(repo), "worktree", "add", str(wt), branch],
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "-C", str(wt), "reset", "--hard", tip], check=True, capture_output=True
                )
            else:
                subprocess.run(
                    ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(wt), tip],
                    check=True,
                    capture_output=True,
                )
        elif _branch_born(repo, branch):
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", str(wt), branch],
                check=True,
                capture_output=True,
            )
        else:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "--orphan", "-b", branch, str(wt)],
                check=True,
                capture_output=True,
            )
        known = True
    elif sync and tip is not None:
        if _dirty(wt):
            # leftover store state from an interrupted run: keep it — the
            # digest verdict (below) decides whether it is trustworthy and
            # a no-op landing check re-derives correctness from the DB
            print(
                "data branch store: keeping leftover .coding-stats/ changes"
                " (digest will be re-verified)"
            )
        else:
            subprocess.run(
                ["git", "-C", str(wt), "reset", "--hard", tip],
                check=True,
                capture_output=True,
            )
        # tip is None: the Data branch is gone from the remote (or it never
        # was there and the worktree is a fresh orphan); keep what is local
    # landed state: HEAD born at the tip for a fetched Data branch, unborn
    # for a first run with no remote branch — the landing verb's first
    # commit creates the orphan branch.
    head = _head(wt)
    print(
        f"data branch {branch}: mounted at {wt}"
        + (f" (at {head[:12]})" if head else " (unborn; first landing creates it)")
    )
    return DataStore(target=target, data_branch=branch, worktree=wt)
