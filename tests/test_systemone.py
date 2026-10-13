"""System One backend tests (ADR-0004): verdict choice + Feature citation,
route resolution, and the shared classify_pending wiring — no network; the
poster is a scripted fake."""

import json

import pytest

from conftest import make_db
from skill_stats.classify import classify_pending
from skill_stats.gitwalk import walk
from skill_stats.systemone import (
    Route,
    SystemOneBackend,
    normalize_model,
    resolve_route,
)


def _response(verdict, probabilities, feature=None, confidence=0.9):
    answers = {"verdict": {"choice": verdict, "probabilities": probabilities}}
    if feature is not None:
        answers["feature"] = {"choice": feature, "probabilities": probabilities}
    answers["verdict"]["confidence"] = confidence
    return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 300}}


def _backend(scripted, route=None):
    b = SystemOneBackend("jev-1.13", route=route or Route("zen", "http://x", "k"))

    def poster(payload):
        # scripted replies consumed per commit; past the script, stock 'new'
        return scripted.pop(0) if scripted else _response("new", {"new": 1}, confidence=1)

    b._poster = poster
    return b


@pytest.fixture()
def walked(tmp_path, repo, shas):
    conn = make_db(tmp_path / "s1.db", repo)
    walk(repo, conn, "main")
    return conn


def _verdicts(conn):
    return [
        r["verdict"]
        for r in conn.execute(
            "SELECT v.verdict FROM commit_verdicts v"
            " JOIN commits c ON c.id = v.commit_id ORDER BY c.walk_index"
        ).fetchall()
    ]


def test_resolve_route_single_key(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "zen-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    route = resolve_route()
    assert route.vendor == "zen" and route.endpoint.endswith("zen/v1/systemone")


def test_resolve_route_requires_explicit_choice_when_several(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "k1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k2")
    with pytest.raises(RuntimeError, match="SKILL_STATS_SYSTEMONE_PROVIDER"):
        resolve_route()
    monkeypatch.setenv("SKILL_STATS_SYSTEMONE_PROVIDER", "openrouter")
    assert resolve_route().vendor == "openrouter"


def test_resolve_route_no_credentials(monkeypatch):
    for key in ("OPENCODE_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="no System One credentials"):
        resolve_route()


def test_normalize_model_per_vendor():
    assert normalize_model("jev-latest", "zen") == "jev-1.13"
    assert normalize_model("jev-1.13.0", "zen") == "jev-1.13"
    assert normalize_model("jev-1.13", "zen") == "jev-1.13"
    assert normalize_model("jev-latest", "openrouter") == "typesafe/jev-latest"
    assert normalize_model("typesafe/jev-1.13", "openrouter") == "typesafe/jev-1.13"
    assert normalize_model("jev-latest", "typesafe") == "jev-latest"
    # typesafe direct takes the model field as-is
    assert normalize_model("jev-1.13.0", "typesafe") == "jev-1.13.0"


def test_classify_end_to_end(walked):
    # chronologically: new, new, fix, refactor, fix, new, new, fix, cleanup
    scripted = [
        _response("new", {"new": 0.91}, confidence=0.91),
        _response("new", {"new": 0.85}, confidence=0.85),
        _response("fix", {"fix": 0.99}, confidence=0.99),
        _response("refactor", {"refactor": 0.93}, confidence=0.93),
        _response("fix", {"fix": 0.97}, confidence=0.97),
        _response("new", {"new": 0.8}, confidence=0.8),
        _response("new", {"new": 0.8}, confidence=0.8),
        _response("fix", {"fix": 0.95}, confidence=0.95),
        _response("cleanup", {"cleanup": 1}, confidence=1),
    ]
    done = classify_pending(walked, "jev-1.13", _backend(scripted))
    assert len(done) == 9
    verdicts = _verdicts(walked)
    assert verdicts == [
        "feature",
        "feature",
        "fix",
        "refactor",
        "fix",
        "feature",
        "feature",
        "fix",
        "cleanup",
    ]
    feature_ids = [r[0] for r in walked.execute("SELECT id FROM features ORDER BY rowid")]
    assert len(feature_ids) == 4  # "new" verdicts mint Features like the LLM path
    row = walked.execute(
        "SELECT raw_llm_output, rationale FROM commit_verdicts WHERE verdict = 'fix'"
        " ORDER BY classified_at LIMIT 1"
    ).fetchone()
    assert json.loads(row["raw_llm_output"])["answers"]["verdict"]["choice"] == "fix"
    assert "confidence=" in row["rationale"] and "p=fix:0.99" in row["rationale"]


def test_feature_question_cites_registry_ids(walked):
    c1 = walked.execute("SELECT id FROM commits WHERE walk_index = 1").fetchone()
    from conftest import add_feature

    fid = add_feature(walked, "alpha module")
    from skill_stats.classify import Reply, apply_reply

    apply_reply(
        walked, c1["id"], "2026", Reply("new", {"k": "new", "title": "alpha module"}), "{}", "m"
    )

    # walk_index 2 continues the Feature: packet carries a feature question
    b = SystemOneBackend("jev-1.13", route=Route("zen", "http://x", "k"))
    payloads = []

    def fake_post(payload):
        payloads.append(payload)
        return _response("new", {"new": 1}, confidence=1)

    b._poster = fake_post
    b.classify(walked, 2)
    p = payloads[0]
    q = p["questions"]
    assert set(q) == {"verdict", "feature"}
    assert fid in q["feature"]["criteria"]
    assert q["verdict"]["type"] == "choice"
    assert p["model"] == "jev-1.13"
    # and an existing verdict applies through apply_reply
    scripted = [_response("existing", {"existing": 0.9}, feature=fid)]
    done = classify_pending(walked, "jev-1.13", _backend(scripted), limit=3)
    assert 2 in done  # the pending set: walk_index 1 is already verdict-stamped
    roles = walked.execute(
        "SELECT role FROM commits_features WHERE commit_id = (SELECT id FROM commits"
        " WHERE walk_index = 2)"
    ).fetchall()
    assert [r["role"] for r in roles] == ["touches"]


def test_existing_with_unmatchable_feature_is_unknown(walked):
    scripted = [_response("existing", {"existing": 0.7}, feature="nope-999")]
    classify_pending(walked, "jev-1.13", _backend(scripted))
    assert walked.execute(
        "SELECT COUNT(*) c FROM commit_verdicts WHERE verdict = 'unknown'"
    ).fetchone()["c"] == 1


def test_bad_choice_is_unknown_not_crash(walked):
    scripted = [
        {"model": "jev-1.13.0", "answers": {"verdict": {"choice": "tweak"}}},
        _response("new", {"new": 1}, confidence=1),
    ]
    done = classify_pending(walked, "jev-1.13", _backend(scripted))
    assert len(done) == 9
    verdicts = _verdicts(walked)
    assert verdicts[0] == "unknown"
    assert verdicts[1] == "feature"


def test_state_carries_title_diff_and_registry(walked):
    from conftest import add_feature

    add_feature(walked, "alpha module", "first feature")
    b = SystemOneBackend("jev-1.13", route=Route("zen", "http://x", "k"))
    payloads = []
    b._poster = lambda p: (payloads.append(p), _response("new", {"new": 1}, confidence=1))[1]
    b.classify(walked, 1)
    state = payloads[0]["state"]
    assert "TITLE: feature one" in state
    assert "DIFF:" in state and "alpha" in state
    assert "alpha module" in state  # registry embedded so "existing" is decidable
