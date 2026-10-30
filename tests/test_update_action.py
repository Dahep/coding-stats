"""The update composite action (`action.yml` at the repo root): the adoption
surface a caller's workflow sees (ADR-0006 clause 5, ticket #24).

A pytest parsing action.yml keeps that surface pinned the way the engine's
CLI-seam tests pin behaviour: declared inputs and outputs, the engine running
from the action's own checkout (so the tool version is exactly the ref the
caller pinned), env-only parameter flow (the injection-safety rule of
ci.render_workflow), fetch coverage of the Data branches, the full-history
requirement, the best-effort opencode install, and the missing-LLM-key
backlog that never fails a run."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ACTION_TEXT = (ROOT / "action.yml").read_text(encoding="utf-8")
ACTION = yaml.safe_load(ACTION_TEXT)

INPUTS = {
    "target-branch",
    "opencode-api-key",
    "openrouter-api-key",
    "no-classify",
    "no-detect",
    "no-lineage",
    "detect-pr",
    "pr-backfill",
}


def _steps() -> list[dict]:
    return ACTION["runs"]["steps"]


def _runs() -> list[str]:
    return [s.get("run", "") for s in _steps()]


def _runs_matching(needle: str) -> list[str]:
    return [r for r in _runs() if needle in r]


# ---------------------------------------------- declared action contract ---


def test_declared_inputs_outputs_and_composite_shape():
    assert ACTION["runs"]["using"] == "composite"
    inputs = ACTION["inputs"]
    assert set(inputs) == INPUTS
    assert inputs["target-branch"]["default"] == "${{ github.ref_name }}"
    for name in sorted(INPUTS):
        assert not inputs[name].get("required", False), name
    for flag in ("no-classify", "no-detect", "no-lineage"):
        assert inputs[flag]["default"] == "false"  # boolean pass-through flags
    for key in ("opencode-api-key", "openrouter-api-key"):
        assert inputs[key]["default"] == ""  # optional: no key is not an error
    # the PR tier is opt-in: detect-pr ships OFF (the git-only default) and
    # the backfill passthrough ships empty (no sweep)
    assert inputs["detect-pr"]["default"] == "false"
    assert inputs["pr-backfill"]["default"] == ""
    assert set(ACTION["outputs"]) == {"data-branch"}
    assert ACTION["outputs"]["data-branch"]["value"] == "${{ steps.land.outputs.data-branch }}"


def test_trim_flags_pass_through_to_update():
    (update,) = _runs_matching("coding-stats update --db")
    for flag in ("--no-classify", "--no-detect", "--no-lineage"):
        assert flag in update
    assert "flags" in update  # flags travel as words, built from env only


def test_detect_pr_and_pr_backfill_pass_through_to_update():
    """The PR tier reaches the engine exactly like the trim flags: words
    built from env, gated on the env value — `detect-pr: "true"` and a
    non-empty `pr-backfill` are the only triggers."""
    (update,) = _runs_matching("coding-stats update --db")
    assert 'if [ "$CODING_STATS_DETECT_PR" = "true" ]; then flags+=(--detect-pr); fi' in update
    assert 'if [ -n "$CODING_STATS_PR_BACKFILL" ]; then' in update
    assert '--pr-backfill "$CODING_STATS_PR_BACKFILL"' in update
    assert "--detect-pr" in update


def test_pr_tier_env_reaches_the_update_step():
    (step,) = [s for s in _steps() if "coding-stats update --db" in s.get("run", "")]
    env = step.get("env", {})
    assert env["CODING_STATS_DETECT_PR"] == "${{ inputs.detect-pr }}"
    assert env["CODING_STATS_PR_BACKFILL"] == "${{ inputs.pr-backfill }}"
    # the PR-tier list API authenticates with the run's own token (env
    # mapping position — never interpolated into a run line, never a PAT)
    assert env["GITHUB_TOKEN"] == "${{ github.token }}"


# ---------------------------------------- engine from the action checkout ---


def test_engine_runs_from_the_actions_own_checkout():
    # the action path IS this repo's checkout: pyproject.toml sits next to
    # action.yml, so `uv run --project $GITHUB_ACTION_PATH` runs exactly the
    # engine version at the ref the caller pinned
    assert (ROOT / "pyproject.toml").exists()
    invocations = _runs_matching("uv run")
    assert invocations
    for run in invocations:
        assert 'uv run --project "$GITHUB_ACTION_PATH"' in run
    joined = "\n".join(invocations)
    assert "coding-stats update --db" in joined
    assert "coding-stats ci land --branch" in joined
    assert "uv tool install" not in joined  # never installs a different engine


def test_update_then_land_run_in_order_against_one_working_db():
    joined = "\n".join(_runs())
    assert joined.index("coding-stats update --db") < joined.index("coding-stats ci land")
    assert "$RUNNER_TEMP/coding-stats/working.db" in joined  # throwaway working DB


# ------------------------------------------------- env-only parameters -----


def test_parameters_reach_shell_steps_through_env_only():
    for run in _runs():
        assert "${{ inputs." not in run  # never interpolated into a run line
        assert "${{ secrets." not in run  # keys arrive as inputs, not secret refs
    # every input is consumed somewhere — via env: mappings — never pasted in
    for name in sorted(INPUTS):
        assert f"${{{{ inputs.{name} }}}}" in ACTION_TEXT, name
    for step in _steps():
        if "run" in step:
            assert step.get("shell") == "bash"  # composite steps name a shell


def test_caller_needs_only_contents_write_unless_the_pr_tier_is_on():
    assert set(ACTION) <= {"name", "description", "inputs", "outputs", "runs", "branding"}
    assert "contents: write" in ACTION_TEXT  # documented: the default's only permission
    for extra in ("pages: write", "id-token: write", "packages:"):
        assert extra not in ACTION_TEXT
    # `pull-requests: read` exists ONLY as the detect-pr opt-in scope: every
    # line naming it also names detect-pr (the off default is git-only)
    assert "pull-requests: read" in ACTION_TEXT
    for line in ACTION_TEXT.splitlines():
        if "pull-requests" in line:
            assert "detect-pr" in line, line
    # evaluated lines reference no secrets: the two LLM keys arrive as inputs
    # (a header comment may show the caller mapping one — comments evaluate
    # to nothing)
    evaluated = "\n".join(
        line for line in ACTION_TEXT.splitlines() if not line.lstrip().startswith("#")
    )
    assert "${{ secrets." not in evaluated


# ---------------------------------------------------------- git plumbing ---


def test_fetch_covers_data_branches_and_the_target():
    (fetch,) = _runs_matching("git fetch")
    assert "--prune" in fetch
    assert "+refs/heads/coding-stats/*:refs/remotes/origin/coding-stats/*" in fetch
    assert "refs/heads/$CODING_STATS_TARGET" in fetch  # Target full history too
    assert " origin " in f" {fetch} " or fetch.rstrip().endswith("origin")


def test_landing_targets_only_the_data_branch():
    joined = "\n".join(_runs())
    assert "git push" not in joined  # only `ci land` pushes (refs/heads/coding-stats/*)
    assert "--force" not in joined  # never force, per the landing rules
    (land,) = _runs_matching("coding-stats ci land")
    assert 'coding-stats ci land --branch "$CODING_STATS_TARGET"' in land
    assert '"$GITHUB_OUTPUT"' in land  # exposes the landed data branch


def test_shallow_checkout_fails_with_a_clear_message():
    (check,) = _runs_matching("is-shallow-repository")
    assert "fetch-depth: 0" in check  # tells the caller exactly what to set
    assert "exit 1" in check
    assert "::error::" in check


# --------------------------------------------------- best-effort degrade ---


def test_opencode_install_is_best_effort():
    step = next(s for s in _steps() if "opencode.ai/install" in s.get("run", ""))
    assert step.get("continue-on-error") is True


def test_no_step_fails_on_a_missing_llm_key():
    # no grep/test gate on the keys: the engine runs the deterministic stages
    # and records the backlog; the shallow check is the only failing step
    failing = [r for r in _runs() if "exit 1" in r]
    assert len(failing) == 1
    assert "is-shallow-repository" in failing[0]
    for run in _runs():
        assert "grep" not in run
        assert "OPENCODE_API_KEY" not in run  # keys pass through env, unread here
        assert "OPENROUTER_API_KEY" not in run
