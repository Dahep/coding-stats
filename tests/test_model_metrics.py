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
    ha = commit_files(repo, f"feat: classifier\n\n{CLAUDE}\n", {"a.txt": "x\n" * 3})
    hb = commit_files(repo, f"feat: renderer\n\n{CODEX}\n", {"b.txt": "y\n" * 2})
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
    assert (claude.model, claude.commits, claude.features) == ("Opus 4.8 (1M context)", 1, 1)
    assert claude.churn == 3  # the walked churn_lines (3 added lines in a.txt)
    assert claude.fix_events == 1 and claude.fixes_per_feature == 1.0

    codex = by_h["codex"]
    assert (codex.model, codex.commits, codex.features) == ("gpt-5", 1, 1)
    assert codex.churn == 2 and codex.fix_events == 0 and codex.fixes_per_feature == 0.0
    conn.close()


def test_per_model_fix_events_count_per_reach(tmp_path, repo):
    """THE Fix-event semantics for the by-model views: ONE fix commit
    reaching TWO of the model's features counts TWO Fix events (one per
    (fix commit, Feature) reach) — the pre-change count said one (per
    commit), however many features it reached."""
    from coding_stats.metrics import attribution_by_model, attribution_trend

    conn, _ids = _built(tmp_path, repo)
    fix_id = conn.execute(
        "SELECT commit_id FROM commit_verdicts WHERE verdict = 'fix'",
    ).fetchone()[0]
    ha_id, fid_a = conn.execute(
        "SELECT commit_id, feature_id FROM commits_features WHERE role = 'defines'"
        " ORDER BY commit_id LIMIT 1"
    ).fetchone()
    # the same claude commit defines a SECOND feature; the fix reaches it too
    fid_two = add_feature(conn, "classifier-two")
    conn.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role) VALUES (?, ?, 'defines')",
        (ha_id, fid_two),
    )
    conn.execute(
        "INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id)"
        " VALUES (?, ?, NULL)",
        (fix_id, fid_two),
    )
    conn.commit()

    rows = attribution_by_model(conn)
    claude = next(r for r in rows if r.harness == "claude")
    assert claude.features == 2
    assert claude.fix_events == 2  # two reaches of the same fix commit
    assert claude.fixes_per_feature == 1.0

    trend = attribution_trend(conn)
    claude_trend = trend["claude/Opus 4.8 (1M context)"]
    assert sum(p.fix_events for p in claude_trend) == 2
    conn.close()


def test_per_model_trend_buckets_months(tmp_path, repo):
    """The trend splits the by-model metrics by commit month: a commit's
    month carries its churn and its feature discovery; a fix lands in its
    own month and reaches features across months."""
    conn, ids = _built(tmp_path, repo)
    from coding_stats.metrics import attribution_trend

    trend = attribution_trend(conn)
    assert set(trend) == {"claude/Opus 4.8 (1M context)", "codex/gpt-5"}
    claude = trend["claude/Opus 4.8 (1M context)"]
    assert all(p.month for p in claude)
    assert sum(p.commits for p in claude) == 1
    assert sum(p.features for p in claude) == 1 and sum(p.churn for p in claude) == 3
    assert sum(p.fix_events for p in claude) == 1
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


def test_model_grouping_is_yes_only_with_identity(tmp_path, repo):
    """The eligibility both by-model views share: only ``yes``-attributed
    commits carrying a harness or model group — a ``possible`` verdict and
    an identity-less ``yes`` reach neither the table nor the trend. The
    hand-built identities also pin that ``resolved=`` is consumed as-is,
    never re-resolved."""
    conn, ids = _built(tmp_path, repo)
    from coding_stats.metrics import ResolvedCommit, attribution_trend

    (sha_yes, cid_yes), (sha_weak, cid_weak), (sha_plain, cid_plain) = sorted(ids.items())
    resolved = [
        ResolvedCommit(cid_yes, sha_yes, "demo", "yes", "claude", "m1", "git", []),
        ResolvedCommit(cid_weak, sha_weak, "demo", "possible", "codex", "gpt-5", "git", []),
        ResolvedCommit(cid_plain, sha_plain, "demo", "yes", None, None, "git", []),
    ]
    rows = attribution_by_model(conn, resolved=resolved)
    assert [(r.harness, r.model) for r in rows] == [("claude", "m1")]
    trend = attribution_trend(conn, resolved=resolved)
    assert set(trend) == {"claude/m1"}
    assert sum(p.commits for p in trend["claude/m1"]) == 1
    conn.close()


def test_unknown_model_cell_is_a_literal_em_dash_never_an_escaped_entity(tmp_path, repo):
    """A known harness whose evidence names no model: the model cell falls
    back to the literal em dash like the sibling tables — the HTML must
    never carry escaped entity text (``&amp;mdash;``) for that row."""
    commit_files(repo, "feat: tool\n\nGenerated-by: codex\n", {"t.txt": "t\n" * 2})
    conn = make_db(tmp_path / "w.db", repo)
    from coding_stats import gitwalk

    gitwalk.walk(repo, conn, "main")
    assert [(r.harness, r.model) for r in attribution_by_model(conn)] == [("codex", "")]
    html = htmlreport.build(conn)
    assert "<td>codex</td><td>—</td>" in html  # known harness, unknown model
    assert "&amp;mdash;" not in html  # never a doubly-escaped entity
    conn.close()
