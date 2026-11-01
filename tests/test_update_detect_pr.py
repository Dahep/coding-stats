"""update-verb PR-tier wiring (ticket #37, ADR-0007 clause 1).

The opt-in `--detect-pr` face runs #36's watermark-driven increment as a
staged pipeline step — after the deterministic stages, before the artifact
is serialized — and the OFF path never enters the PR tier at all (byte-
identical pipeline, no API call, no disclosure). `--pr-backfill full` is
the workflow_dispatch passthrough: the BATCHED history backfill — oldest-
unincorporated merged PRs past the shared watermark, 250 per invocation,
each dispatch landing a disjoint chunk and the next continuing past it
(never a restart at PR 1, never a re-sweep below the marker — that is the
direct `pr-sweep --full` face). Detection-only: no LLM calls. The
disclosure line prints whenever the mode is on, and the serialized artifact
carries the evidence rows and the watermark."""

import sqlite3
import subprocess
import urllib.error

import pytest

from coding_stats import artifact, cli, htmlreport, prsweep
from coding_stats.report import text_report
from conftest import (
    FakeTransport,
    land_squash_prs,
    pr_list_rows,
    start_remote,
    store_dir,
)

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
    shas = land_squash_prs(repo, ks)
    monkeypatch.setattr("coding_stats.identity.resolved_identity", lambda _repo: GITHUB_ID)
    db_path = str(tmp_path / "w.db")
    cli.main(["init", str(repo), "--db", db_path])
    return db_path, shas


def _serving(monkeypatch, ks):
    """Serve the list endpoint for the given PR numbers; every request is
    answered locally (zero network in tests)."""
    served = FakeTransport(pr_list_rows(*ks))
    monkeypatch.setattr("coding_stats.prsource.default_transport", served)
    return served


def _rows_per_pr(db_path):
    conn = sqlite3.connect(db_path)
    out = {
        int(r[0]): int(r[1])
        for r in conn.execute("SELECT pr_number, COUNT(*) FROM pr_evidence GROUP BY pr_number")
    }
    conn.close()
    return out


def _watermark(db_path):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT last_pr_number FROM pr_watermark").fetchone()
    conn.close()
    return int(row[0]) if row else 0


def _marker_count(db_path):
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM pr_disclosure").fetchone()[0]
    conn.close()
    return int(n)


def _report_text(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return text_report(conn)
    finally:
        conn.close()


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
    monkeypatch.setattr("coding_stats.prsweep.sweep_full", lambda *a, **k: calls.append("full"))
    monkeypatch.setattr(
        "coding_stats.prsource.default_transport",
        lambda url: pytest.fail("the off path must make no API call"),
    )

    cli.main(["update", "--db", db_path, "--no-classify"])
    out = capsys.readouterr().out
    assert calls == []  # no sweep face ever ran
    assert "pr-sweep" not in out
    assert DISCLOSURE not in out  # no mode, no disclosure
    assert _marker_count(db_path) == 0  # the off path records nothing
    assert DISCLOSURE not in _report_text(db_path)  # and reports never disclose
    assert artifact.verify(store_dir(repo) / "coding-stats.sql") is None
    mat = _materialized_artifact(repo)
    assert mat.execute("SELECT COUNT(*) FROM pr_evidence").fetchone()[0] == 0
    assert mat.execute("SELECT COUNT(*) FROM pr_watermark").fetchone()[0] == 0
    assert mat.execute("SELECT COUNT(*) FROM pr_disclosure").fetchone()[0] == 0
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


def test_zero_row_sweep_still_discloses_in_text_and_html_reports(
    tmp_path, repo, monkeypatch, capsys
):
    # the DECISION: disclosure is mode state, not row state — a sweep face
    # that finds zero non-suspicious rows still records the marker, the
    # artifact carries it, and both reports disclose on the marker alone
    db_path, _ = _built(tmp_path, repo, [], monkeypatch)
    _serving(monkeypatch, [])  # an empty listing: zero PRs, zero evidence rows

    cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])
    out = capsys.readouterr().out
    assert "pr-sweep: 0 PRs recorded" in out
    assert _rows_per_pr(db_path) == {}  # zero non-suspicious evidence rows
    assert _marker_count(db_path) == 1  # yet the PR tier has run: marker on

    mat = _materialized_artifact(repo)
    assert mat.execute("SELECT COUNT(*) FROM pr_disclosure").fetchone()[0] == 1
    mat.row_factory = sqlite3.Row
    assert DISCLOSURE in text_report(mat)  # text report: marker-only disclosure
    assert DISCLOSURE in htmlreport.build(mat)  # and the HTML report
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
    land_squash_prs(repo, [4])
    _serving(monkeypatch, [1, 2, 3, 4])
    cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])
    out = capsys.readouterr().out
    assert "pr-sweep: 1 PRs recorded" in out
    assert "watermark 4" in out
    assert _watermark(db_path) == 4
    assert set(_rows_per_pr(db_path)) == {1, 2, 3, 4}


# ------------------------------------------ the pr-backfill: full face ----
# the face's batched semantics live in the "bounded CI backfill" section
# below; this section keeps the input-shape and operational-failure wiring.


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
        urllib.error.HTTPError("url", 500, "Server Error", hdrs={}, fp=None),
    ],
)
def test_sweep_operational_failures_become_a_visible_unavailable_stop(
    tmp_path, repo, monkeypatch, capsys, boom
):
    # best-effort like classification (Q13), owned by prsweep's run
    # machinery: the expected operational families convert to a clean
    # STOP_UNAVAILABLE result — the CLI catches no exception families —
    # surfacing as a visible notice that carries the failure's detail; the
    # deterministic pipeline and artifact landing are never failed by the PR tier
    db_path, _ = _built(tmp_path, repo, [1], monkeypatch)

    def raise_boom(*_a, **_k):
        raise boom

    monkeypatch.setattr("coding_stats.prsource.default_transport", raise_boom)
    cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])
    out = capsys.readouterr().out
    assert "::notice::pr-sweep unavailable" in out  # the stop notice, not a cli catch
    assert str(boom) in out  # useful detail
    assert "WARNING: pr-sweep unavailable" not in out  # no family catch left in cli
    assert "pr-sweep: 0 PRs recorded" in out  # listing failed before the prefix
    assert "artifact updated" in out
    assert artifact.verify(store_dir(repo) / "coding-stats.sql") is None
    assert DISCLOSURE in out
    # the marker landed before the listing failed: disclosure persists to
    # the reports even though the run recorded zero evidence rows
    assert _marker_count(db_path) == 1
    assert DISCLOSURE in _report_text(db_path)


@pytest.mark.parametrize("bug_type", [TypeError, RuntimeError])
def test_sweep_tier_never_swallows_arbitrary_bugs(tmp_path, repo, monkeypatch, bug_type):
    # not a blanket catch: TypeError and an invariant RuntimeError both stay
    # explicit — a real bug is never laundered into a best-effort stop
    db_path, _ = _built(tmp_path, repo, [1], monkeypatch)

    def raise_boom(*_a, **_k):
        raise bug_type("a real bug")

    monkeypatch.setattr("coding_stats.prsource.default_transport", raise_boom)
    with pytest.raises(bug_type):
        cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])


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


# ------------------------------- the bounded CI backfill (cap 250/run) -----
# update's --pr-backfill full is the BATCHED backfill face: oldest-
# unincorporated history past the watermark, 250 PRs per invocation — never
# a restart at PR 1, never a re-sweep below the marker (direct
# `pr-sweep --full` owns that explicit re-sweep).


def test_pr_backfill_full_is_batched_history_and_never_restarts_at_pr1(
    tmp_path, repo, monkeypatch, capsys
):
    db_path, _ = _built(tmp_path, repo, [1, 2, 3], monkeypatch)
    _serving(monkeypatch, [1, 2, 3])
    cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])
    capsys.readouterr()
    before = _rows_per_pr(db_path)

    cli.main(["update", "--db", db_path, "--no-classify", "--pr-backfill", "full"])
    out = capsys.readouterr().out
    assert "pr-sweep: 0 PRs recorded" in out  # caught up: no restart at PR 1
    assert "watermark 3" in out  # the shared watermark stands
    assert DISCLOSURE in out
    assert _rows_per_pr(db_path) == before  # nothing below the marker is re-recorded
    assert _watermark(db_path) == 3


def test_update_backfill_passes_the_named_cap_while_direct_full_stays_uncapped(
    tmp_path, repo, monkeypatch
):
    assert prsweep.DEFAULT_BACKFILL_CAP == 250  # the named per-invocation default
    db_path, _ = _built(tmp_path, repo, [1], monkeypatch)
    _serving(monkeypatch, [1])
    seen: list = []
    real_backfill = prsweep.sweep_backfill
    real_full = prsweep.sweep_full

    def spy_backfill(*a, **k):
        seen.append(("backfill", k.get("max_prs")))
        return real_backfill(*a, **k)

    def spy_full(*a, **k):
        seen.append(("full", k.get("max_prs")))
        return real_full(*a, **k)

    monkeypatch.setattr(prsweep, "sweep_backfill", spy_backfill)
    monkeypatch.setattr(prsweep, "sweep_full", spy_full)
    cli.main(["update", "--db", db_path, "--no-classify", "--pr-backfill", "full"])
    assert seen == [("backfill", prsweep.DEFAULT_BACKFILL_CAP)]  # capped per invocation

    seen.clear()
    cli.main(["pr-sweep", "--db", str(db_path), "--full"])
    assert seen == [("full", None)]  # direct full: the explicit re-sweep, uncapped by default


def test_two_consecutive_capped_backfills_process_disjoint_chunks_toward_completion(
    tmp_path, repo, monkeypatch, capsys
):
    db_path, _ = _built(tmp_path, repo, [1, 2, 3], monkeypatch)
    _serving(monkeypatch, [1, 2, 3])
    monkeypatch.setattr(prsweep, "DEFAULT_BACKFILL_CAP", 2)  # shrink for fast chunks

    cli.main(["update", "--db", db_path, "--no-classify", "--pr-backfill", "full"])
    out = capsys.readouterr().out
    assert "pr-sweep: 2 PRs recorded" in out and "watermark 2" in out
    assert set(_rows_per_pr(db_path)) == {1, 2}  # the first chunk landed

    # the identical dispatch continues past the landed chunk — it must not
    # restart at PR 1 (that would re-record 2 PRs and stall at the cap forever)
    cli.main(["update", "--db", db_path, "--no-classify", "--pr-backfill", "full"])
    out = capsys.readouterr().out
    assert "pr-sweep: 1 PRs recorded" in out and "watermark 3" in out
    assert set(_rows_per_pr(db_path)) == {1, 2, 3}  # disjoint chunks, toward completion

    # history is caught up: the next dispatch records nothing
    cli.main(["update", "--db", db_path, "--no-classify", "--pr-backfill", "full"])
    out = capsys.readouterr().out
    assert "pr-sweep: 0 PRs recorded" in out and "watermark 3" in out


def test_capped_backfill_ends_cleanly_at_the_cap_and_lands_the_prefix(
    tmp_path, repo, monkeypatch, capsys
):
    db_path, _ = _built(tmp_path, repo, [1, 2, 3], monkeypatch)
    _serving(monkeypatch, [1, 2, 3])
    monkeypatch.setattr(prsweep, "DEFAULT_BACKFILL_CAP", 2)  # shrink for a fast capped run

    cli.main(["update", "--db", db_path, "--no-classify", "--pr-backfill", "full"])
    out = capsys.readouterr().out
    assert "pr-sweep: 2 PRs recorded" in out  # ended cleanly at the cap
    assert "pr-sweep stopped: processing cap reached" in out
    assert "next run resumes" in out  # the standard cap notice, never a failure
    assert "artifact updated" in out  # the prefix landed with the artifact
    assert artifact.verify(store_dir(repo) / "coding-stats.sql") is None
    assert _watermark(db_path) == 2  # the swept prefix's marker landed too

    # the recorded watermark rides the artifact: the next run resumes past it
    cli.main(["update", "--db", db_path, "--no-classify", "--detect-pr"])
    out = capsys.readouterr().out
    assert "pr-sweep: 1 PRs recorded" in out and "watermark 3" in out
