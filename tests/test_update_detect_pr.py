"""update-verb PR-tier wiring (ticket #37, ADR-0007 clause 1).

The opt-in `--detect-pr` face runs #36's watermark-driven increment as a
staged pipeline step — after the deterministic stages, before the artifact
is serialized — and the OFF path never enters the PR tier at all (byte-
identical pipeline, no API call, no disclosure). `--pr-backfill full` is
the workflow_dispatch passthrough: the same watermark machinery walked to
the beginning of the PR numbering. Detection-only: no LLM calls. The
disclosure line prints whenever the mode is on, and the serialized artifact
carries the evidence rows and the watermark."""

import sqlite3
import subprocess

import pytest

from coding_stats import artifact, cli, prsweep
from conftest import store_dir
from test_prsweep import FakeTransport, start_remote
from test_prsweep_watermark import make_squash_prs, pulls

DISCLOSURE = "GitHub-sourced (PR-channel) evidence present"
GITHUB_ID = (
    "https://github.com/dahep/coding-stats",
    "dahep-coding-stats",
    "https://github.com/dahep/coding-stats",
)


def _built(tmp_path, repo, ks, monkeypatch):
    """A squash-PR repo with its CLI working DB, identified as a GitHub
    repo (the PR tier derives owner/name from the repositories row)."""
    start_remote(tmp_path, repo)
    shas = make_squash_prs(repo, ks)
    monkeypatch.setattr(
        "coding_stats.identity.resolved_identity", lambda _repo: GITHUB_ID
    )
    db_path = str(tmp_path / "w.db")
    cli.main(["init", str(repo), "--db", db_path])
    return db_path, shas


def _serving(monkeypatch, ks):
    """Serve the list endpoint for the given PR numbers; every request is
    answered locally (zero network in tests)."""
    served = FakeTransport(pulls(*ks))
    monkeypatch.setattr("coding_stats.prsweep.default_transport", served)
    return served


def _rows_per_pr(db_path):
    conn = sqlite3.connect(db_path)
    out = {
        int(r[0]): int(r[1])
        for r in conn.execute(
            "SELECT pr_number, COUNT(*) FROM pr_evidence GROUP BY pr_number"
        )
    }
    conn.close()
    return out


def _watermark(db_path):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT last_pr_number FROM pr_watermark").fetchone()
    conn.close()
    return int(row[0]) if row else 0


def _materialized_artifact(repo):
    mat = sqlite3.connect(":memory:")
    mat.executescript((store_dir(repo) / "coding-stats.sql").read_text(encoding="utf-8"))
    return mat


# ------------------------------------------------------- the off path -----


def test_off_path_never_enters_the_pr_tier(tmp_path, repo, monkeypatch, capsys):
    db_path, _ = _built(tmp_path, repo, [1, 2], monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        "coding_stats.prsweep.sweep_increment",
        lambda *a, **k: calls.append("increment"),
    )
    monkeypatch.setattr(
        "coding_stats.prsweep.sweep_full", lambda *a, **k: calls.append("full")
    )
    monkeypatch.setattr(
        "coding_stats.prsweep.default_transport",
        lambda url: pytest.fail("the off path must make no API call"),
    )

    cli.main(["update", "--db", db_path, "--no-classify"])
    out = capsys.readouterr().out
    assert calls == []  # no sweep face ever ran
    assert "pr-sweep" not in out
    assert DISCLOSURE not in out  # no mode, no disclosure
    assert artifact.verify(store_dir(repo) / "coding-stats.sql") is None
    mat = _materialized_artifact(repo)
    assert mat.execute("SELECT COUNT(*) FROM pr_evidence").fetchone()[0] == 0
    assert mat.execute("SELECT COUNT(*) FROM pr_watermark").fetchone()[0] == 0
    mat.close()


# -------------------------------------------------- the detect-pr face ---


def test_detect_pr_runs_the_increment_before_serialize_and_records_evidence(
    tmp_path, repo, monkeypatch, capsys
):
    db_path, shas = _built(tmp_path, repo, [1, 2, 3], monkeypatch)
    _serving(monkeypatch, [1, 2, 3])

    cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])
    out = capsys.readouterr().out
    # the stage runs after the deterministic stages and before serialization
    assert out.index("live lines at") < out.index("pr-sweep:") < out.index("wrote ")
    assert "pr-sweep: 3 PRs recorded" in out
    assert "watermark 3" in out
    assert DISCLOSURE in out  # on-mode notice
    assert _watermark(db_path) == 3
    assert set(_rows_per_pr(db_path)) == {1, 2, 3}
    # the artifact side carries the rows and the watermark
    assert artifact.verify(store_dir(repo) / "coding-stats.sql") is None
    mat = _materialized_artifact(repo)
    assert mat.execute("SELECT COUNT(*) FROM pr_evidence").fetchone()[0] > 0
    assert mat.execute("SELECT last_pr_number FROM pr_watermark").fetchone()[0] == 3
    mat.close()


def test_increment_resumes_above_the_watermark_across_push_runs(
    tmp_path, repo, monkeypatch, capsys
):
    db_path, _ = _built(tmp_path, repo, [1, 2, 3], monkeypatch)
    _serving(monkeypatch, [1, 2, 3])
    cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])
    capsys.readouterr()

    # second push run: nothing sits above the watermark
    cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])
    out = capsys.readouterr().out
    assert "pr-sweep: 0 PRs recorded" in out
    assert "watermark 3" in out

    # a fourth PR lands; the next run records exactly it (PRs > watermark only)
    make_squash_prs(repo, [4])
    _serving(monkeypatch, [1, 2, 3, 4])
    cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])
    out = capsys.readouterr().out
    assert "pr-sweep: 1 PRs recorded" in out
    assert "watermark 4" in out
    assert _watermark(db_path) == 4
    assert set(_rows_per_pr(db_path)) == {1, 2, 3, 4}


# ------------------------------------------ the pr-backfill: full face ----


def test_pr_backfill_full_resweeps_the_whole_numbering_sharing_the_watermark(
    tmp_path, repo, monkeypatch, capsys
):
    db_path, _ = _built(tmp_path, repo, [1, 2, 3], monkeypatch)
    _serving(monkeypatch, [1, 2, 3])
    cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])
    capsys.readouterr()
    before = _rows_per_pr(db_path)

    cli.main(["update", "--db", db_path, "--no-classify", "--pr-backfill", "full"])
    out = capsys.readouterr().out
    assert "pr-sweep: 3 PRs recorded" in out  # walked below the watermark too
    assert "watermark 3" in out  # the shared watermark never regresses
    assert DISCLOSURE in out
    assert _rows_per_pr(db_path) == before  # replace-not-duplicate: no growth
    assert _watermark(db_path) == 3


def test_pr_backfill_rejects_shapes_other_than_full():
    with pytest.raises(SystemExit):
        cli.main(["update", "--db", "unused.db", "--pr-backfill", "half"])


@pytest.mark.parametrize(
    "boom",
    [
        ValueError("bad API body"),
        KeyError("number"),
        subprocess.SubprocessError("git died"),
        OSError("network gone"),
    ],
)
def test_sweep_tier_never_fails_the_deterministic_update(
    tmp_path, repo, monkeypatch, capsys, boom
):
    # best-effort like classification (Q13): bad API bodies, git failures and
    # OS errors degrade to the WARNING path — the deterministic pipeline and
    # the artifact landing are never failed by the PR tier
    db_path, _ = _built(tmp_path, repo, [1], monkeypatch)

    def raise_boom(*_a, **_k):
        raise boom

    monkeypatch.setattr("coding_stats.prsweep.sweep_increment", raise_boom)
    cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])
    out = capsys.readouterr().out
    assert "WARNING: pr-sweep unavailable" in out
    assert "artifact updated" in out
    assert artifact.verify(store_dir(repo) / "coding-stats.sql") is None


def test_sweep_tier_never_swallows_an_interrupt(tmp_path, repo, monkeypatch):
    # KeyboardInterrupt is the sweep's existing STOP condition — and when it
    # escapes the sweep the best-effort catch must NOT swallow it
    db_path, _ = _built(tmp_path, repo, [1], monkeypatch)

    def raise_boom(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr("coding_stats.prsweep.sweep_increment", raise_boom)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])


# ------------------------------------------------ best-effort degrade ----


def test_sweep_unavailable_degrades_to_a_warning_and_keeps_the_pipeline(
    tmp_path, repo, monkeypatch, capsys
):
    db_path, _ = _built(tmp_path, repo, [1], monkeypatch)

    def boom(*_a, **_k):
        raise prsweep.SweepError("no GitHub origin")

    monkeypatch.setattr("coding_stats.prsweep.sweep_increment", boom)
    cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])
    out = capsys.readouterr().out
    assert "WARNING: pr-sweep unavailable" in out
    assert "artifact updated" in out  # the deterministic pipeline still landed
    assert artifact.verify(store_dir(repo) / "coding-stats.sql") is None
    assert DISCLOSURE in out  # the mode is on regardless
