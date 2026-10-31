"""Features / churn / fixes per attributed AI model (the by-model table).

Per (harness, model) — the channel-resolved identity of yes-attributed
commits: the features those commits define, their churn, and the fix
commits reaching those features, each once per model row it reaches.
Lower bounds everywhere; a commit without a matched identity contributes
to no row, and ``no-evidence`` never renders as a model."""


from coding_stats import htmlreport
from coding_stats.metrics import attribution_by_model
from conftest import add_feature, commit_files, make_db

CLAUDE = "Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
CODEX = "Assisted-by: codex:gpt-5"


def _built(tmp_path, repo):
    """walked DB over three commits: A fixes f1 (claude), B touches f2
    (codex), C is an unattributed fix of f1."""
    ha = commit_files(
        repo, f"feat: classifier\n\n{CLAUDE}\n", {"a.txt": "x\n" * 3}
    )
    hb = commit_files(
        repo, f"feat: renderer\n\n{CODEX}\n", {"b.txt": "y\n" * 2}
    )
    hc = commit_files(repo, "fix: classifier crash", {"a.txt": "y\n" * 3})
    conn = make_db(tmp_path / "w.db", repo)
    from coding_stats import gitwalk
    gitwalk.walk(repo, conn, "main")
    ids = {str(r["sha"]): int(r["id"]) for r in conn.execute("SELECT id, sha FROM commits")}
    fid_a = add_feature(conn, "classifier")
    fid_b = add_feature(conn, "renderer")
    for cid, fid in ((ids[ha], fid_a), (ids[hb], fid_b)):
        conn.execute(
            "INSERT INTO commits_features (commit_id, feature_id, role) VALUES (?, ?, 'defines')",
            (cid, fid),
        )
        conn.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
            " model, classified_at) VALUES (?, 'feature', '', '{}', 'm', datetime('now'))",
            (cid,),
        )
    conn.execute(
        "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
        " model, classified_at) VALUES (?, 'fix', '', '{}', 'm', datetime('now'))",
        (ids[hc],),
    )
    # fix commit hc reaches feature fid_a
    conn.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role) VALUES (?, ?, 'touches')",
        (ids[hc], fid_a),
    )
    conn.execute(
        "INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id)"
        " VALUES (?, ?, NULL)",
        (ids[hc], fid_a),
    )
    conn.commit()
    return conn, ids


def test_per_model_metrics_are_lower_bounds(tmp_path, repo):
    conn, ids = _built(tmp_path, repo)
    rows = attribution_by_model(conn)
    by_h = {r.harness: r for r in rows}
    assert set(by_h) == {"claude", "codex"}

    claude = by_h["claude"]
    assert (claude.model, claude.commits, claude.features) == (
        "Opus 4.8 (1M context)", 1, 1
    )
    assert claude.churn == 3  # the walked churn_lines (3 added lines in a.txt)
    assert claude.fixes == 1 and claude.fixes_per_feature == 1.0

    codex = by_h["codex"]
    assert (codex.model, codex.commits, codex.features) == ("gpt-5", 1, 1)
    assert codex.churn == 2 and codex.fixes == 0 and codex.fixes_per_feature == 0.0
    conn.close()


def test_the_model_table_renders_and_hides_without_evidence(tmp_path, repo):
    conn, ids = _built(tmp_path, repo)
    html = htmlreport.build(conn)
    assert "per AI model" in html
    assert "Opus 4.8 (1M context)" in html and "gpt-5" in html
    assert "Fixes/feature" in html
    conn.close()

    # zero attribution: no model rows, no table, zero single-repo drift
    empty = make_db(tmp_path / "e.db", repo)
    from coding_stats import gitwalk
    gitwalk.walk(repo, empty, "main")
    empty.execute("DELETE FROM ai_attribution")
    empty.execute("DELETE FROM ai_evidence")
    empty.commit()
    page = htmlreport.build(empty)
    assert "per AI model" not in page
    empty.close()
