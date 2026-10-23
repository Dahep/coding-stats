"""CI scaffold tests: workflow rendering (ci init) and the landing verb
(ci land) — local bare remotes, no GH API."""

import subprocess
from pathlib import Path

import pytest

from coding_stats import ci, datastore
from conftest import bare_remote, commit_files, git_out, run, store_worktree, with_remote


def _store_changes(repo):
    (store_worktree(repo) / ".coding-stats").mkdir(exist_ok=True)
    (store_worktree(repo) / ".coding-stats" / "report.html").write_text(
        "<html>report</html>", encoding="utf-8"
    )
    (store_worktree(repo) / ".coding-stats" / "coding-stats.sql").write_text(
        "-- coding-stats artifact\n-- digest:\n", encoding="utf-8"
    )


def test_ci_init_writes_caller_workflow_and_is_idempotent(repo):
    import yaml

    ci.init(repo, "main")
    wf = repo / ".github" / "workflows" / "coding-stats.yml"
    text = wf.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    on = doc.get("on", doc.get(True))  # PyYAML 1.1 reads bare `on` as bool
    assert on["push"]["branches"] == ["main"]
    jobs = doc["jobs"]
    assert set(jobs) == {"update", "report"}
    update = jobs["update"]
    assert update["permissions"] == {"contents": "write"}  # the only write
    assert update["concurrency"]["cancel-in-progress"] is False  # never kills a classifying run
    uses = [step.get("uses", "") for step in update["steps"]]
    assert "Dahep/coding-stats@v1" in uses  # one `uses:` step adoption
    checkout = [s for s in update["steps"] if s.get("uses", "").startswith("actions/checkout")]
    assert checkout[0]["with"]["fetch-depth"] == 0
    assert "fetch-depth: 0" in text
    report = jobs["report"]
    # Pages permissions live in the report job only (ADR-0006 clause 5);
    # checkout/fetch need contents: read (job-level permissions deny the rest)
    assert report["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    assert report["steps"][-1]["with"]["deploy-pages"] == "false"  # Pages stays opt-in
    assert "Dahep/coding-stats/report@v1" in [step.get("uses", "") for step in report["steps"]]
    assert "glm" not in text  # no model names: settings travel in the artifact

    msgs = ci.init(repo, "main")  # idempotent assertion
    assert wf.read_text(encoding="utf-8") == text
    assert any("up to date" in m for m in msgs)


def test_ci_init_no_report_omits_the_report_job(repo):
    import yaml

    ci.init(repo, "main", report=False)
    doc = yaml.safe_load((repo / ".github" / "workflows" / "coding-stats.yml").read_text("utf-8"))
    assert set(doc["jobs"]) == {"update"}


def test_ci_init_refuses_drift_unless_upgrade(repo):
    ci.init(repo, "main")
    wf = repo / ".github" / "workflows" / "coding-stats.yml"
    wf.write_text(wf.read_text(encoding="utf-8") + "\n# hand edit\n", encoding="utf-8")
    with pytest.raises(ci.DriftError, match="--upgrade") as excinfo:
        ci.init(repo, "main")
    assert "hand edit" in str(excinfo.value)  # drift surfaced with a diff summary
    assert "# hand edit" in wf.read_text(encoding="utf-8")  # refused: untouched
    ci.init(repo, "main", upgrade=True)
    assert "# hand edit" not in wf.read_text(encoding="utf-8")


def _newer_data_landing(tmp_path, bare, base) -> None:
    """Simulate another run landing first: a commit parented on `base`,
    pushed to the bare remote's coding-stats/main."""
    scratch = tmp_path / "newer"
    subprocess.run(["git", "clone", "-q", bare, str(scratch)], check=True)
    run(scratch, "config", "user.name", "T")
    run(scratch, "config", "user.email", "t@t.io")
    run(scratch, "checkout", "-q", "--detach", base)
    (scratch / ".coding-stats").mkdir(exist_ok=True)
    (scratch / ".coding-stats" / "newer.sql").write_text("-- newer landing\n")
    run(scratch, "add", "-A", "--", ".coding-stats")
    run(scratch, "commit", "-m", "newer push")
    run(scratch, "push", "-q", "origin", "HEAD:refs/heads/coding-stats/main")


def _branch_tip(git_dir, branch) -> str:
    return git_out(git_dir, "rev-parse", "refs/heads/" + branch).strip()


def _branch_exists(git_dir, branch) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(git_dir), "rev-parse", "--verify", "-q", "refs/heads/" + branch],
        capture_output=True,
    )
    return proc.returncode == 0


def test_ci_land_noop_without_store_changes(repo, tmp_path):
    commit_files(repo, "base", {"a.txt": "a\n"})
    with_remote(repo, bare_remote(tmp_path))
    store = datastore.prepare(repo, "main")
    before = git_out(repo, "rev-parse", "HEAD").strip()
    assert ci.land(store, repo, "origin").status == "noop"
    assert git_out(repo, "rev-parse", "HEAD").strip() == before  # Target untouched
    assert not _branch_exists(bare_remote(tmp_path), "coding-stats/main")  # nothing pushed


def test_ci_land_commits_and_pushes(repo, tmp_path):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = bare_remote(tmp_path)
    with_remote(repo, bare)
    target_before = git_out(bare, "rev-parse", "refs/heads/main").strip()
    git_out(bare, "update-ref", "refs/heads/other", target_before)
    other_before = git_out(bare, "rev-parse", "refs/heads/other").strip()
    store = datastore.prepare(repo, "main")
    _store_changes(repo)

    assert ci.land(store, repo, "origin").status == "landed"
    assert (
        git_out(store.worktree, "log", "-1", "--format=%an|%ae|%s").strip()
        == "coding-stats[bot]|coding-stats[bot]@users.noreply.github.com"
        "|coding-stats artifact update (auto)"
    )
    head = git_out(store.worktree, "rev-parse", "HEAD").strip()
    assert _branch_tip(bare, "coding-stats/main") == head  # pushed to the Data branch
    with pytest.raises(subprocess.CalledProcessError):
        git_out(store.worktree, "rev-parse", "HEAD^")  # first commit: no parent (orphan root)
    main_tip = git_out(bare, "rev-parse", "refs/heads/main").strip()
    assert main_tip == target_before  # the Target branch never moved
    assert git_out(bare, "rev-parse", "refs/heads/other").strip() == other_before


def test_ci_land_abandons_when_newer_data_push_landed(repo, tmp_path, capsys):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = bare_remote(tmp_path)
    with_remote(repo, bare)
    store = datastore.prepare(repo, "main")
    _store_changes(repo)
    # first: our own run lands (an orphan tip exists on the remote now)
    assert ci.land(store, repo, "origin").status == "landed"
    base = _branch_tip(bare, "coding-stats/main")
    # then this run reads its base and works...
    (store.worktree / ".coding-stats" / "incoming.sql").write_text("-- v2\n", encoding="utf-8")
    # ...while a newer push advances the remote Data tip
    _newer_data_landing(tmp_path, bare, base)
    tip = _branch_tip(bare, "coding-stats/main")

    result = ci.land(store, repo, "origin")
    assert result.status == "abandoned"
    ci.report_landing(result)  # the CLI reports; the verb stays silent
    out = capsys.readouterr().out
    assert "::notice::" in out  # neutral annotation: never a red run (Q18)
    assert _branch_tip(bare, "coding-stats/main") == tip  # nothing pushed
    # the abandoned run did not add a second artifact-update commit
    log = git_out(store.worktree, "log", "--format=%s")
    assert log.count("coding-stats artifact update (auto)") == 1


# ------------------------------------------- push race + single reporting ---


def test_ci_land_push_race_abandons_neutrally(repo, tmp_path, monkeypatch):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = bare_remote(tmp_path)
    with_remote(repo, bare)
    store = datastore.prepare(repo, "main")
    _store_changes(repo)
    # our own first landing so the remote Data branch exists for the race
    assert ci.land(store, repo, "origin").status == "landed"
    base = _branch_tip(bare, "coding-stats/main")
    (store.worktree / ".coding-stats" / "incoming.sql").write_text("-- v2\n", encoding="utf-8")

    def racy_push(repo_, remote, branch, head):
        _newer_data_landing(tmp_path, bare, base)
        raise subprocess.CalledProcessError(1, ["git", "push"], stderr="non-fast-forward")

    monkeypatch.setattr(ci, "_push", racy_push)
    result = ci.land(store, repo, "origin")
    assert result.status == "abandoned"  # neutral outcome, not a red failure


def test_ci_land_push_race_diverged_abandons(repo, tmp_path, monkeypatch):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = bare_remote(tmp_path)
    with_remote(repo, bare)
    store = datastore.prepare(repo, "main")
    _store_changes(repo)
    assert ci.land(store, repo, "origin").status == "landed"
    (store.worktree / ".coding-stats" / "incoming.sql").write_text("-- v2\n", encoding="utf-8")
    # an UNRELATED (diverged) remote tip: no ancestry closure either way
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    run(orphan, "init", "-q", "-b", "main")
    run(orphan, "config", "user.name", "T")
    run(orphan, "config", "user.email", "t@t.io")
    (orphan / "o.txt").write_text("o\n", encoding="utf-8")
    run(orphan, "add", "-A")
    run(orphan, "commit", "-m", "orphan root")
    orphan_sha = git_out(orphan, "rev-parse", "HEAD").strip()

    def racy_push(repo_, remote, branch, head):
        # force-move the remote Data tip to the unrelated (diverged) orphan root
        run(orphan, "push", "-q", "--force", bare, f"HEAD:refs/heads/{branch}")
        raise subprocess.CalledProcessError(1, ["git", "push"], stderr="non-fast-forward")

    monkeypatch.setattr(ci, "_push", racy_push)
    result = ci.land(store, repo, "origin")
    assert result.status == "abandoned"  # divergence also ends neutrally (never force)
    assert git_out(bare, "rev-parse", "refs/heads/coding-stats/main").strip() == orphan_sha


def test_ci_land_unrelated_push_failure_raises(repo, tmp_path, monkeypatch):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = bare_remote(tmp_path)
    with_remote(repo, bare)
    store = datastore.prepare(repo, "main")
    _store_changes(repo)
    stored_head_before = git_out(repo, "rev-parse", "refs/heads/main").strip()

    def auth_fail(repo_, remote, branch, head):
        raise subprocess.CalledProcessError(128, ["git", "push"], stderr="Authentication failed")

    monkeypatch.setattr(ci, "_push", auth_fail)
    with pytest.raises(subprocess.CalledProcessError):
        ci.land(store, repo, "origin")  # real errors still raise
    assert git_out(repo, "rev-parse", "refs/heads/main").strip() == stored_head_before


def test_ci_land_local_without_remote(repo):
    """Local landing without any origin: the orphan Data-branch commit is
    created (the local branch becomes the record), nothing is pushed."""
    commit_files(repo, "base", {"a.txt": "a\n"})
    store = datastore.prepare(repo, "main")
    _store_changes(repo)
    result = ci.land(store, repo, "origin")
    assert result.status == "landed" and "no remote" in result.detail
    head = git_out(store.worktree, "rev-parse", "HEAD").strip()
    assert _branch_tip(repo, "coding-stats/main") == head  # branch exists locally
    # the landing never touched the Target branch
    assert store.worktree == store_worktree(repo)


def test_ci_land_cli_prints_status_once(tmp_path, repo, capsys, monkeypatch):
    from coding_stats.cli import main

    commit_files(repo, "base", {"a.txt": "a\n"})
    with_remote(repo, bare_remote(tmp_path))
    datastore.prepare(repo, "main")
    _store_changes(repo)
    monkeypatch.chdir(repo)
    main(["ci", "land", "--branch", "main"])
    out = capsys.readouterr().out
    assert out.count("coding-stats ci land: landed") == 1  # printed once, by the CLI
    assert out.count("landed") == 1


# ------------------------ parameter hygiene, tool install, init hardening ---


def test_render_workflow_is_injection_safe_for_hostile_values():
    import yaml

    for branch in ("feature;id", "true", "café branch", "1.0"):
        text = ci.render_workflow(branch)
        doc = yaml.safe_load(text)
        on = doc.get("on", doc.get(True))  # PyYAML 1.1 reads bare `on` as bool
        assert on["push"]["branches"] == [branch]  # clean strings: 'true' is not a bool
        # the branch name only ever appears as the quoted YAML scalar — the
        # caller workflow has no run: lines and no env interpolation
        for job in doc["jobs"].values():
            for step in job["steps"]:
                assert branch not in step.get("run", "")
                assert ";id" not in step.get("run", "")


def test_ci_init_validates_branch_ref_name(repo):
    import yaml

    with pytest.raises(ValueError, match="café branch"):
        ci.init(repo, "café branch")  # spaces are not valid ref characters
    ci.init(repo, "feature;id")  # valid ref, hostile shell-wise: renders safely
    doc = yaml.safe_load((repo / ".github" / "workflows" / "coding-stats.yml").read_text("utf-8"))
    on = doc.get("on", doc.get(True))
    assert on["push"]["branches"] == ["feature;id"]


def test_ci_land_parents_previous_data_tip_and_survives_target_movement(repo, tmp_path):
    """A second landing parents on the previous Data tip; a Target branch
    that moved on in between does NOT abandon — the Data tip alone judges
    staleness, so paid classification always lands (ADR-0006 clause 1)."""
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = bare_remote(tmp_path)
    with_remote(repo, bare)
    store = datastore.prepare(repo, "main")
    _store_changes(repo)
    assert ci.land(store, repo, "origin").status == "landed"
    first = _branch_tip(bare, "coding-stats/main")

    # the Target branch moves on (a human commit) while our run works
    commit_files(repo, "human work on main", {"a.txt": "a\nb\n"})
    run(repo, "push", "-q", "origin", "main")

    (store.worktree / ".coding-stats" / "incoming.sql").write_text("-- v2\n", encoding="utf-8")
    result = ci.land(store, repo, "origin")
    assert result.status == "landed"  # Target movement is not staleness
    second = _branch_tip(bare, "coding-stats/main")
    assert git_out(store.worktree, "rev-parse", "HEAD^").strip() == first  # parented
    assert second != first


def test_dogfood_workflow_is_the_generated_caller_workflow():
    """This repo adopts like any other caller: the committed workflow is
    `ci init`'s render, with only this repo's per-repo opt-in (Pages)
    flipped on — the actions ref is the same public `@v1`, never `./`."""
    committed = Path(__file__).parents[1] / ".github" / "workflows" / "coding-stats.yml"
    got = committed.read_text(encoding="utf-8")
    generated = ci.render_workflow("main")
    # the one per-repo opt-in (Pages), normalized away for the byte-compare
    pages_flipped = 'deploy-pages: "true"  # this repo\'s per-repo opt-in (publishes the report)'
    not_flipped = 'deploy-pages: "false"  # flip to "true" to publish to GitHub Pages'
    assert got.replace(pages_flipped, not_flipped) == generated
    assert "uses: Dahep/coding-stats@v1" in got
    assert "uses: Dahep/coding-stats/report@v1" in got
