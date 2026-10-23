"""Data-branch store tests (ADR-0006 clause 1): the datastore primitive and
the layout skips update/land surface as neutral notices."""

import subprocess

import pytest

from coding_stats import ci, datastore
from coding_stats.cli import main
from conftest import commit_files, run, store_worktree


def _git(repo, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _bare(tmp_path) -> str:
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(path)], check=True)
    return str(path)


def _with_remote(repo, bare) -> None:
    run(repo, "remote", "add", "origin", bare)
    run(repo, "push", "-q", "-u", "origin", "main")
    subprocess.run(
        ["git", "-C", bare, "symbolic-ref", "HEAD", "refs/heads/main"], check=True
    )


# ------------------------------------------------------- datastore.prepare ---


def test_prepare_creates_unborn_orphan_worktree(repo):
    commit_files(repo, "base", {"a.txt": "a\n"})  # Target branch has history
    store = datastore.prepare(repo, "main")
    assert store.data_branch == "coding-stats/main"
    assert store.worktree == store_worktree(repo)
    assert store.worktree.exists()
    # unborn orphan: no files but the .git marker, no parentless commit yet,
    # no branch ref
    assert ci._head(store.worktree) is None
    with pytest.raises(subprocess.CalledProcessError):
        _git(repo, "rev-parse", "refs/heads/coding-stats/main")
    assert not (store.worktree / ".coding-stats").exists()  # empty checkout


def test_prepare_with_remote_tip_anchors_the_worktree(repo, tmp_path):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    # a first run lands once (data branch exists on the remote)
    store = datastore.prepare(repo, "main")
    (store.worktree / ".coding-stats").mkdir(exist_ok=True)
    (store.worktree / ".coding-stats" / "coding-stats.sql").write_text("-- v1\n")
    assert ci.land(store, repo, "origin").status == "landed"
    tip = _git(bare, "rev-parse", "refs/heads/coding-stats/main").strip()

    # a fresh checkout-like state: the worktree re-syncs onto the new tip
    store2 = datastore.prepare(repo, "main")
    assert datastore._head(store2.worktree) == tip
    assert (store2.worktree / ".coding-stats" / "coding-stats.sql").read_text() == "-- v1\n"


def test_prepare_resyncs_existing_worktree_to_newer_tip(repo, tmp_path):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    store = datastore.prepare(repo, "main")
    (store.worktree / ".coding-stats").mkdir(exist_ok=True)
    (store.worktree / ".coding-stats" / "coding-stats.sql").write_text("-- v1\n")
    assert ci.land(store, repo, "origin").status == "landed"

    # another run lands on top of it
    scratch = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", bare, str(scratch)], check=True)
    run(scratch, "config", "user.name", "T")
    run(scratch, "config", "user.email", "t@t.io")
    run(scratch, "checkout", "-q", "--detach", datastore.remote_tip(repo, "coding-stats/main"))
    (scratch / ".coding-stats" / "incoming.sql").write_text("-- v2\n")
    run(scratch, "add", "-A", "--", ".coding-stats")
    run(scratch, "commit", "-m", "newer landing")
    run(scratch, "push", "-q", "origin", "HEAD:refs/heads/coding-stats/main")
    newer = _git(bare, "rev-parse", "refs/heads/coding-stats/main").strip()

    # the update action's fetch step brings the newer tip into the checkout
    fetch = "+refs/heads/coding-stats/main:refs/remotes/origin/coding-stats/main"
    run(repo, "fetch", "origin", fetch)

    store2 = datastore.prepare(repo, "main")
    assert datastore._head(store2.worktree) == newer  # hard-resynced to the tip
    assert (store2.worktree / ".coding-stats" / "incoming.sql").read_text() == "-- v2\n"


def test_prepare_keeps_last_fetched_state_when_remote_branch_disappears(repo, tmp_path):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    store = datastore.prepare(repo, "main")
    (store.worktree / ".coding-stats").mkdir(exist_ok=True)
    (store.worktree / ".coding-stats" / "coding-stats.sql").write_text("-- v1\n")
    assert ci.land(store, repo, "origin").status == "landed"
    tip = _git(bare, "rev-parse", "refs/heads/coding-stats/main").strip()
    run(bare, "update-ref", "-d", "refs/heads/coding-stats/main")

    store2 = datastore.prepare(repo, "main", sync=True)
    # the remote branch is gone: the last known local state stays readable
    # (never destroyed, never force-reset past it)
    assert datastore._head(store2.worktree) == tip
    assert (store2.worktree / ".coding-stats" / "coding-stats.sql").read_text() == "-- v1\n"


def test_prepare_skips_target_under_coding_stats_prefix(repo):
    commit_files(repo, "base", {"a.txt": "a\n"})
    with pytest.raises(datastore.StoreSkip, match="Data branch"):
        datastore.prepare(repo, "coding-stats/main")


def test_prepare_skips_ref_path_clash(repo, tmp_path):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    # another tracked target already landed data under the deeper name
    subprocess.run(
        ["git", "-C", repo, "push", "-q", "origin", "HEAD:refs/heads/coding-stats/release/2.x"],
        check=True,
        capture_output=True,
    )
    with pytest.raises(datastore.StoreSkip, match="release"):
        datastore.prepare(repo, "release")  # data branch nests under the clash


def test_prepare_raises_on_hostile_branch_names(repo):
    with pytest.raises(ValueError, match="invalid branch"):
        datastore.prepare(repo, "bad branch")
    with pytest.raises(ValueError, match="invalid branch"):
        datastore.prepare(repo, "../escape")


def test_prepare_without_remote(repo):
    commit_files(repo, "base", {"a.txt": "a\n"})
    # no origin configured at all: first-run behaviour, orphan worktree
    store = datastore.prepare(repo, "main")
    assert datastore._head(store.worktree) is None


def test_default_worktree_linked_worktree_checkout(repo, tmp_path):
    """Inside a linked git worktree (a .git FILE), the store stays out of
    git's sqlite path and lands in temp storage — but still deterministic
    for both update and land in one machine."""
    other = tmp_path / "linked"
    run(repo, "worktree", "add", other, "-b", "feature/x")
    assert (other / ".git").is_file()
    wt = datastore.default_worktree(other, "feature/x")
    assert ".git" not in wt.parts or ".git" not in str(wt.relative_to(other))
    assert str(other) not in str(wt)  # outside the linked checkout
    store = datastore.prepare(other, "feature/x")
    assert store.worktree == wt
    assert datastore._head(store.worktree) is None


# ---------------------------------------- update + land against a bare origin ---


def test_update_and_land_first_run_creates_orphan_data_branch(
    tmp_path, repo, capsys, monkeypatch
):
    """The full outside-visible seam: update writes the store on the Data
    branch worktree, land pushes it; the Target branch is untouched."""

    class FakeSession:
        session_id = None

        def __init__(self, replies):
            self.replies = list(replies)

        def send(self, message):
            return self.replies.pop(0)

    commit_files(repo, "feature a", {"a.txt": "a1\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    target_before = _git(bare, "rev-parse", "refs/heads/main").strip()
    db_path = str(tmp_path / "u.db")
    main(["init", str(repo), "--db", db_path])
    monkeypatch.setattr(
        "coding_stats.cli.Session",
        lambda model, sid=None: FakeSession(['{"k":"new","title":"a","about":"","why":"y"}']),
    )
    monkeypatch.chdir(repo)
    main(["update", "--db", db_path])
    main(["ci", "land", "--branch", "main"])
    out = capsys.readouterr().out

    assert "coding-stats ci land: landed" in out
    assert _git(bare, "rev-parse", "refs/heads/coding-stats/main")  # Data branch created
    assert _git(bare, "rev-parse", "refs/heads/main").strip() == target_before
    assert "artifact update (auto)" not in _git(bare, "log", "--format=%s", "refs/heads/main")

    # orphan root: the landing commit has no parent
    tip = _git(bare, "rev-parse", "refs/heads/coding-stats/main").strip()
    with pytest.raises(subprocess.CalledProcessError):
        _git(bare, "rev-parse", f"{tip}^")


def test_update_skips_legacy_store_without_data_branch(tmp_path, repo, capsys):
    commit_files(repo, "feature", {"a.txt": "a1\n"})
    (repo / ".coding-stats").mkdir()
    (repo / ".coding-stats" / "coding-stats.sql").write_bytes(
        b"-- coding-stats artifact\n-- format: 1\n"
    )
    db_path = str(tmp_path / "u.db")
    main(["init", str(repo), "--db", db_path])
    capsys.readouterr()
    main(["update", "--db", db_path])  # exit 0: a skip is neutral
    out = capsys.readouterr().out
    assert "::notice::" in out
    assert "coding-stats migrate" in out
    assert "update skipped; nothing was walked, classified, or landed" in out
    # never silently re-classified on top of it: nothing was walked either
    assert "walked" not in out.split("::notice::", 1)[1].split("update skipped;", 1)[0]


def test_update_reprefixes_old_format_artifact_in_place(
    tmp_path, repo, capsys, monkeypatch
):
    """An artifact minted under the branch-less {repo-slug}-{n} scheme is
    re-prefixed to {repo}@{branch} when the update adopts it — classified
    history kept, the digest honestly recomputed."""
    import hashlib
    import sqlite3
    import unittest.mock as mock

    class SeedSession:
        session_id = None

        def __init__(self, replies):
            self.replies = list(replies)

        def send(self, message):
            return self.replies.pop(0)

    commit_files(repo, "feature one", {"a.txt": "alpha\n"})
    repo_dir = store_worktree(repo, "main")
    repo_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "u.db")
    main(["init", str(repo), "--db", db_path])
    with mock.patch(
        "coding_stats.cli.Session",
        lambda model, sid=None: SeedSession(
            ['{"k":"new","title":"alpha module","about":"","why":"y"}']
        ),
    ):
        main(["update", "--db", db_path])
    capsys.readouterr()
    art_path = repo_dir / ".coding-stats" / "coding-stats.sql"

    # demote the ids to the branch-less old shape, digest honestly rebuilt
    def demote(text: str) -> str:
        text = text.replace("repo@main-1", "repo-1")
        lines = text.splitlines(True)
        lines = ["-- digest:\n" if line.startswith("-- digest:") else line for line in lines]
        blanked = "".join(lines)
        digest = hashlib.sha256(blanked.encode("utf-8")).hexdigest()
        return blanked.replace("-- digest:\n", f"-- digest: {digest}\n", 1)

    art_path.write_text(demote(art_path.read_text(encoding="utf-8")))
    capsys.readouterr()

    # the adopting update re-prefixes in place and keeps the verdicts
    main(["update", "--db", db_path])
    mat = sqlite3.connect(":memory:")
    mat.executescript(art_path.read_text(encoding="utf-8"))
    ids = [r[0] for r in mat.execute("SELECT id FROM features ORDER BY id")]
    verdicts = mat.execute("SELECT COUNT(*) FROM commit_verdicts").fetchone()[0]
    assert ids == ["repo@main-1"]
    assert verdicts == 1  # the classified history survived the re-prefix
    mat.close()


def test_two_target_branches_stay_separate(repo, tmp_path):
    """Two Target branches keep separate data branches; landing one side
    moves nothing else."""
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    store_a = datastore.prepare(repo, "main")
    store_b = datastore.prepare(repo, "rel-1")
    assert store_a.data_branch == "coding-stats/main"
    assert store_b.data_branch == "coding-stats/rel-1"
    assert store_a.worktree != store_b.worktree
    (store_a.worktree / ".coding-stats").mkdir(exist_ok=True)
    (store_a.worktree / ".coding-stats" / "main.sql").write_text("a\n")
    assert ci.land(store_a, repo, "origin").status == "landed"
    assert _git(bare, "rev-parse", "refs/heads/coding-stats/main")  # landed by sha
    with pytest.raises(subprocess.CalledProcessError):
        _git(bare, "rev-parse", "refs/heads/coding-stats/rel-1")  # sibling untouched
    assert _git(bare, "rev-parse", "refs/heads/main")  # Target branch untouched
