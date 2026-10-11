"""CI scaffold tests: workflow rendering (ci init) and the landing verb
(ci land) — local bare remotes, no GH API."""

import subprocess

import pytest

from conftest import commit_files, run
from skill_stats import ci


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
    (repo / ".skill-stats").mkdir(exist_ok=True)
    (repo / ".skill-stats" / "report.html").write_text("<html>report</html>", encoding="utf-8")
    (repo / ".skill-stats" / "skill-stats.sql").write_text(
        "-- skill-stats artifact\n-- digest:\n", encoding="utf-8"
    )


def test_ci_init_writes_workflow_and_is_idempotent(repo):
    ci.init(repo, "main")
    wf = repo / ".github" / "workflows" / "skill-stats.yml"
    text = wf.read_text(encoding="utf-8")
    for needle in (
        "paths-ignore",
        ".skill-stats/**",
        "fetch-depth: 0",
        "contents: write",
        "astral-sh/setup-uv",
        "uv sync",
        "https://opencode.ai/install",
        "OPENCODE_API_KEY",
        "skill-stats update --db .skill-stats/working.db",
        "skill-stats ci land --branch main",
        "actions/upload-artifact",
        "if: always()",
        "schedule",  # cadence configurable in one obvious place
    ):
        assert needle in text
    assert "glm" not in text  # no model names: settings travel in the artifact
    gi = repo / ".skill-stats" / ".gitignore"
    assert "working.db" in gi.read_text(encoding="utf-8")  # throwaway DB never commits

    msgs = ci.init(repo, "main")  # idempotent assertion
    assert wf.read_text(encoding="utf-8") == text
    assert any("up to date" in m for m in msgs)


def test_ci_init_refuses_drift_unless_upgrade(repo):
    ci.init(repo, "main")
    wf = repo / ".github" / "workflows" / "skill-stats.yml"
    wf.write_text(wf.read_text(encoding="utf-8") + "\n# hand edit\n", encoding="utf-8")
    with pytest.raises(ci.DriftError, match="--upgrade") as excinfo:
        ci.init(repo, "main")
    assert "hand edit" in str(excinfo.value)  # drift surfaced with a diff summary
    assert "# hand edit" in wf.read_text(encoding="utf-8")  # refused: untouched
    ci.init(repo, "main", upgrade=True)
    assert "# hand edit" not in wf.read_text(encoding="utf-8")


def test_ci_init_no_push_omits_landing_step(repo):
    ci.init(repo, "main", push=False)
    text = (repo / ".github" / "workflows" / "skill-stats.yml").read_text(encoding="utf-8")
    assert "ci land" not in text
    assert "contents: write" not in text  # nothing is written without the landing step


def test_ci_land_noop_without_store_changes(repo, tmp_path):
    commit_files(repo, "base", {"a.txt": "a\n"})
    _with_remote(repo, _bare(tmp_path))
    before = _git(repo, "rev-parse", "HEAD").strip()
    assert ci.commit_or_abandon(repo, "main", "origin") == "noop"
    assert _git(repo, "rev-parse", "HEAD").strip() == before  # no commit created


def test_ci_land_commits_and_pushes(repo, tmp_path):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    _git(bare, "update-ref", "refs/heads/other", _git(repo, "rev-parse", "HEAD").strip())
    other_before = _git(bare, "rev-parse", "refs/heads/other").strip()
    _store_changes(repo)

    assert ci.commit_or_abandon(repo, "main", "origin") == "landed"
    assert (
        _git(repo, "log", "-1", "--format=%an|%ae|%s").strip()
        == "skill-stats[bot]|skill-stats[bot]@users.noreply.github.com"
        "|skill-stats artifact update (auto)"
    )
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert _git(bare, "rev-parse", "refs/heads/main").strip() == head  # pushed
    assert _git(bare, "rev-parse", "refs/heads/other").strip() == other_before  # never touched


def test_ci_land_abandons_when_newer_push_landed(repo, tmp_path, capsys):
    commit_files(repo, "base", {"a.txt": "a\n"})
    bare = _bare(tmp_path)
    _with_remote(repo, bare)
    # a newer push advances the remote tip while our run works
    other = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", bare, str(other)], check=True)
    run(other, "config", "user.name", "T")
    run(other, "config", "user.email", "t@t.io")
    (other / "x.txt").write_text("x\n", encoding="utf-8")
    run(other, "add", "-A")
    run(other, "commit", "-m", "newer push")
    run(other, "push", "-q", "origin", "HEAD:main")
    tip = _git(bare, "rev-parse", "refs/heads/main").strip()

    _store_changes(repo)
    assert ci.commit_or_abandon(repo, "main", "origin") == "abandoned"
    out = capsys.readouterr().out
    assert "::notice::" in out  # neutral annotation: never a red run (Q18)
    assert _git(bare, "rev-parse", "refs/heads/main").strip() == tip  # nothing pushed
    assert "skill-stats artifact update (auto)" not in _git(repo, "log", "--format=%s")
