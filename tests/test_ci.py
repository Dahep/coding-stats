"""CI scaffold tests: workflow rendering (ci init) and the landing verb
(ci land) — local bare remotes, no GH API."""

import subprocess
from pathlib import Path

import pytest

from coding_stats import ci, datastore
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
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")


def _store_changes(repo):
    (store_worktree(repo) / ".coding-stats").mkdir(exist_ok=True)
    (store_worktree(repo) / ".coding-stats" / "report.html").write_text(
        "<html>report</html>", encoding="utf-8"
    )
    (store_worktree(repo) / ".coding-stats" / "coding-stats.sql").write_text(
        "-- coding-stats artifact\n-- digest:\n", encoding="utf-8"
    )


def test_ci_init_writes_workflow_and_is_idempotent(repo):
    ci.init(repo, "main")
    wf = repo / ".github" / "workflows" / "coding-stats.yml"
    text = wf.read_text(encoding="utf-8")
    for needle in (
        "paths-ignore",
        ".coding-stats/**",
        "fetch-depth: 0",
        "contents: write",
        "astral-sh/setup-uv",
        "https://opencode.ai/install",
        "OPENCODE_API_KEY",
        "coding-stats update --db",
        "coding-stats ci land --branch",
        "schedule",  # cadence configurable in one obvious place
    ):
        assert needle in text
    # reports render through Dahep/coding-stats/report@v1 (ADR-0006): the
    # rendered update workflow carries no report/upload step
    assert "upload-artifact" not in text
    assert "glm" not in text  # no model names: settings travel in the artifact
    gi = repo / ".coding-stats" / ".gitignore"
    assert "working.db" in gi.read_text(encoding="utf-8")  # throwaway DB never commits

    msgs = ci.init(repo, "main")  # idempotent assertion
    assert wf.read_text(encoding="utf-8") == text
    assert any("up to date" in m for m in msgs)


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


def test_ci_init_no_push_omits_landing_step(repo):
    import yaml

    ci.init(repo, "main", push=False)
    doc = yaml.safe_load(
        (repo / ".github" / "workflows" / "coding-stats.yml").read_text("utf-8")
    )
    runs = [step.get("run", "") for step in doc["jobs"]["coding-stats"]["steps"]]
    assert not any("ci land" in run for run in runs)
    assert "contents: write" not in (
        repo / ".github" / "workflows" / "coding-stats.yml"
    ).read_text("utf-8")  # nothing is written without the landing step


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
    return _git(git_dir, "rev-parse", "refs/heads/" + branch).strip()


def _branch_exists(git_dir, branch) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(git_dir), "rev-parse", "--verify", "-q", "refs/heads/" + branch],
        capture_output=True,
    )
    return proc.returncode == 0


def test_ci_land_noop_without_store_changes(repo, tmp_path):
    commit_files(repo, "base", {"a.txt": "a\n"})
    _with_remote(repo, _bare(tmp_path))
    store = datastore.prepare(repo, "main")
    before = _git(repo, "rev-parse", "HEAD").strip()
    assert ci.land(store, repo, "origin").status == "noop"
    assert _git(repo, "rev-parse", "HEAD").strip() == before  # Target untouched
    assert not _branch_exists(_bare(tmp_path), "coding-stats/main")  # nothing pushed


def test_ci_land_commits_and_pushes(repo, tmp_path):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    target_before = _git(bare, "rev-parse", "refs/heads/main").strip()
    _git(bare, "update-ref", "refs/heads/other", target_before)
    other_before = _git(bare, "rev-parse", "refs/heads/other").strip()
    store = datastore.prepare(repo, "main")
    _store_changes(repo)

    assert ci.land(store, repo, "origin").status == "landed"
    assert (
        _git(store.worktree, "log", "-1", "--format=%an|%ae|%s").strip()
        == "coding-stats[bot]|coding-stats[bot]@users.noreply.github.com"
        "|coding-stats artifact update (auto)"
    )
    head = _git(store.worktree, "rev-parse", "HEAD").strip()
    assert _branch_tip(bare, "coding-stats/main") == head  # pushed to the Data branch
    with pytest.raises(subprocess.CalledProcessError):
        _git(store.worktree, "rev-parse", "HEAD^")  # first commit: no parent (orphan root)
    assert _git(bare, "rev-parse", "refs/heads/main").strip() == target_before  # Target never moved
    assert _git(bare, "rev-parse", "refs/heads/other").strip() == other_before


def test_ci_land_abandons_when_newer_data_push_landed(repo, tmp_path, capsys):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
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
    log = _git(store.worktree, "log", "--format=%s")
    assert log.count("coding-stats artifact update (auto)") == 1


# ------------------------------------------- push race + single reporting ---


def test_ci_land_push_race_abandons_neutrally(repo, tmp_path, monkeypatch):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
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
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
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
    orphan_sha = _git(orphan, "rev-parse", "HEAD").strip()

    def racy_push(repo_, remote, branch, head):
        # force-move the remote Data tip to the unrelated (diverged) orphan root
        run(orphan, "push", "-q", "--force", bare, f"HEAD:refs/heads/{branch}")
        raise subprocess.CalledProcessError(1, ["git", "push"], stderr="non-fast-forward")

    monkeypatch.setattr(ci, "_push", racy_push)
    result = ci.land(store, repo, "origin")
    assert result.status == "abandoned"  # divergence also ends neutrally (never force)
    assert _git(bare, "rev-parse", "refs/heads/coding-stats/main").strip() == orphan_sha


def test_ci_land_unrelated_push_failure_raises(repo, tmp_path, monkeypatch):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    store = datastore.prepare(repo, "main")
    _store_changes(repo)
    stored_head_before = _git(repo, "rev-parse", "refs/heads/main").strip()

    def auth_fail(repo_, remote, branch, head):
        raise subprocess.CalledProcessError(128, ["git", "push"], stderr="Authentication failed")

    monkeypatch.setattr(ci, "_push", auth_fail)
    with pytest.raises(subprocess.CalledProcessError):
        ci.land(store, repo, "origin")  # real errors still raise
    assert _git(repo, "rev-parse", "refs/heads/main").strip() == stored_head_before


def test_ci_land_local_without_remote(repo):
    """Local landing without any origin: the orphan Data-branch commit is
    created (the local branch becomes the record), nothing is pushed."""
    commit_files(repo, "base", {"a.txt": "a\n"})
    store = datastore.prepare(repo, "main")
    _store_changes(repo)
    result = ci.land(store, repo, "origin")
    assert result.status == "landed" and "no remote" in result.detail
    head = _git(store.worktree, "rev-parse", "HEAD").strip()
    assert _branch_tip(repo, "coding-stats/main") == head  # branch exists locally
    # the landing never touched the Target branch
    assert store.worktree == store_worktree(repo)


def test_ci_land_cli_prints_status_once(tmp_path, repo, capsys, monkeypatch):
    from coding_stats.cli import main

    commit_files(repo, "base", {"a.txt": "a\n"})
    _with_remote(repo, _bare(tmp_path))
    datastore.prepare(repo, "main")
    _store_changes(repo)
    monkeypatch.chdir(repo)
    main(["ci", "land", "--branch", "main"])
    out = capsys.readouterr().out
    assert out.count("coding-stats ci land: landed") == 1  # printed once, by the CLI
    assert out.count("landed") == 1


# ------------------------ parameter hygiene, tool install, init hardening ---


def _pyproject_version() -> str:
    import tomllib

    data = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text("utf-8"))
    return str(data["project"]["version"])


def _pyproject_version() -> str:
    import tomllib

    data = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text("utf-8"))
    return str(data["project"]["version"])


def test_render_workflow_is_injection_safe_for_hostile_values():
    import re

    import yaml

    for branch in ("feature;id", "true", "café branch", "1.0"):
        text = ci.render_workflow(branch, db=".coding-stats/my db.sql")
        doc = yaml.safe_load(text)
        on = doc.get("on", doc.get(True))  # PyYAML 1.1 reads bare `on` as bool
        assert on["push"]["branches"] == [branch]  # clean strings: 'true' is not a bool
        assert doc["env"]["CODING_STATS_BRANCH"] == branch
        assert doc["env"]["CODING_STATS_DB"] == ".coding-stats/my db.sql"
        for step in doc["jobs"]["coding-stats"]["steps"]:
            run_line = step.get("run", "")
            # parameters travel via env only: no user value ever lands in a
            # run line (word-boundary check; '1.0' inside an unrelated
            # constant like ==0.1.0 is not interpolation)
            assert not re.search(rf"(?<![\w.-]){re.escape(branch)}(?![\w.-])", run_line)
            assert ";id" not in run_line  # no injection surface in run: lines
        assert '"$CODING_STATS_BRANCH"' in text and '"$CODING_STATS_DB"' in text


def test_ci_init_validates_branch_ref_name(repo):
    import yaml

    with pytest.raises(ValueError, match="café branch"):
        ci.init(repo, "café branch")  # spaces are not valid ref characters
    ci.init(repo, "feature;id")  # valid ref, hostile shell-wise: renders safely
    doc = yaml.safe_load((repo / ".github" / "workflows" / "coding-stats.yml").read_text("utf-8"))
    on = doc.get("on", doc.get(True))
    assert on["push"]["branches"] == ["feature;id"]


def test_ci_workflow_installs_tool_standalone(repo):
    ci.init(repo, "main")
    text = (repo / ".github" / "workflows" / "coding-stats.yml").read_text("utf-8")
    sha = ci.own_commit()
    spec = ci.install_spec(tool_ref=sha)
    # the target repo does not depend on coding-stats: install the tool
    # standalone, pinned to the rendering checkout's commit (not PyPI — the
    # package is not published; git install keeps upgrades re-render
    # testable) — from bigger repos, --tool-ref moves the pin explicitly
    assert f"uv tool install '{spec}'" in text
    if sha:
        assert sha in text
    assert "uv sync" not in text  # no target-dependency assumption
    assert "uv run coding-stats" not in text
    assert "coding-stats update --db" in text  # direct invocation
    # installer is best-effort: a failed download must not abort the run
    assert "continue-on-error: true" in text


def test_ci_init_drift_preflight_is_all_or_nothing(repo):
    gi = repo / ".coding-stats" / ".gitignore"
    gi.parent.mkdir(parents=True, exist_ok=True)
    gi.write_text("# hand edited\n", encoding="utf-8")  # drifted store gitignore
    with pytest.raises(ci.DriftError, match="--upgrade"):
        ci.init(repo, "main")  # workflow is missing but must NOT be half-written
    assert not (repo / ".github" / "workflows" / "coding-stats.yml").exists()
    assert gi.read_text(encoding="utf-8") == "# hand edited\n"


def test_ci_init_custom_db_gitignore_covers_sidecars(repo):
    ci.init(repo, "main", db=".coding-stats/ci state.db")
    gi = (repo / ".coding-stats" / ".gitignore").read_text(encoding="utf-8")
    for entry in (
        "ci state.db\n",
        "ci state.db-wal\n",
        "ci state.db-shm\n",
        "ci state.db-journal\n",
    ):
        assert entry in gi  # computed from the configured db, not hardcoded
    # and the gitignore can never swallow the artifact files themselves
    with pytest.raises(ValueError, match="coding-stats.sql"):
        ci.init(repo, "main", db=".coding-stats/coding-stats.sql", upgrade=True)
