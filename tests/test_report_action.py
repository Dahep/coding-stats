"""Report action tests (ADR-0006 clause 5, ticket #25): the composite
action's declared surface (inputs/outputs), env-only parameter hygiene,
Pages gating, and the README's workflow-trigger limitation note."""

from pathlib import Path

import pytest
import yaml

from coding_stats.cli import main
from conftest import commit_files, store_dir

ROOT = Path(__file__).parents[1]  # the workspace root (report/ lives below it)
ACTION = ROOT / "report" / "action.yml"
README = ROOT / "README.md"


@pytest.fixture(scope="module")
def doc():
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def test_declared_inputs_and_outputs(doc):
    assert doc["inputs"]["deploy-pages"]["default"] == "false"
    assert doc["inputs"]["include-db"]["default"] == "false"
    assert "artifact-name" in doc["inputs"]
    assert doc["outputs"]["report-path"]["value"] == "${{ env.CODING_STATS_REPORT_PATH }}"
    # db-path aliases the union var the engine-paths step actually writes
    assert doc["outputs"]["db-path"]["value"] == "${{ env.CODING_STATS_UNION }}"
    evaluated = "\n".join(
        line
        for line in ACTION.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "CODING_STATS_UNION=" in evaluated  # the env var exists, not just the mapping


def test_engine_installed_from_repo_root_above_the_action_path(doc):
    runs = [step["run"] for step in doc["runs"]["steps"] if "run" in step]
    assert any("$GITHUB_ACTION_PATH/.." in run for run in runs)
    # the root above the report action carries the engine's pyproject
    assert (ROOT / "pyproject.toml").exists()


def test_gather_covers_every_coding_stats_ref(doc):
    runs = "\n".join(step["run"] for step in doc["runs"]["steps"] if "run" in step)
    assert "refs/heads/coding-stats/*:refs/remotes/origin/coding-stats/*" in runs
    assert "--prune" in runs
    assert "coding-stats gather" in runs  # one command: combined + deduped


def test_pages_steps_are_opt_in(doc):
    pages_uses = (
        "actions/deploy-pages@",
        "actions/upload-pages-artifact@",
        "actions/configure-pages@",
    )
    for step in doc["runs"]["steps"]:
        if "uses" in step and any(u in step["uses"] for u in pages_uses):
            assert step.get("if") == "${{ inputs.deploy-pages == 'true' }}"


def test_parameters_travel_through_env_only():
    """Injection safety (the repo's rule): no inputs.* or secrets.* inside a
    run: line; those tokens only appear in env/with/if mapping positions."""
    evaluated = "\n".join(
        line
        for line in ACTION.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    doc = yaml.safe_load(evaluated)
    for step in doc["runs"]["steps"]:
        run_line = step.get("run", "")
        assert "${{ inputs." not in run_line
        assert "${{ secrets." not in run_line


def test_readme_documents_push_trigger_limitation():
    """A push trigger on coding-stats/** never fires: GITHUB_TOKEN-made
    pushes do not start workflows — the README says so."""
    text = README.read_text(encoding="utf-8")
    assert "start workflows" in text
    assert "`coding-stats/**`" in text


def test_update_writes_no_report_into_the_store(tmp_path, repo, capsys, monkeypatch):
    """update stops writing report.html: the store holds only the SQL
    Artifact; Data-branch diffs stay small (rendering = report verb/action)."""

    class Scripted:
        session_id = None

        def send(self, message):
            return '{"k":"cleanup","why":"w"}'

    commit_files(repo, "feature", {"a.txt": "a1\n"})
    db_path = str(tmp_path / "u.db")
    main(["init", str(repo), "--db", db_path])
    monkeypatch.setattr("coding_stats.cli.Session", lambda model, sid=None: Scripted())
    monkeypatch.chdir(repo)
    main(["update", "--db", db_path])
    capsys.readouterr()
    assert not (store_dir(repo) / "report.html").exists()
