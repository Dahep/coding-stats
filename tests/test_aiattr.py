"""Pure git-only AI attribution detector (ticket #34, ADR-0007 baseline tier).

The rule table is normative in issue #34: rules 1-5 are ``yes``-tier, rule 6
(branch prefixes) is ``possible``-tier; nothing matched is ``no-evidence``,
which is never a claim about authorship. Matching runs on the RAW identity
strings (pre-mailmap). These tests exercise the detector as a pure function
over synthetic CommitFacts — no git, no network.
"""

import pytest

from coding_stats.aiattr import (
    VERDICT_NO_EVIDENCE,
    VERDICT_POSSIBLE,
    VERDICT_YES,
    CommitFacts,
    attribute,
)


def facts(
    author_name="Tester",
    author_email="t@t.io",
    committer_name="Tester",
    committer_email="t@t.io",
    title="feat: a thing",
    message="feat: a thing",
):
    return CommitFacts(
        author_name=author_name,
        author_email=author_email,
        committer_name=committer_name,
        committer_email=committer_email,
        title=title,
        message=message,
    )


def evidence(attr):
    return [(e.rule, e.raw, e.verdict) for e in attr.evidence]


# ------------------------------------------------------- rule 1: trailers --

ANTHROPIC_TRAILER = "Claude Opus 4.8 (1M context) <noreply@anthropic.com>"


def test_co_authored_by_known_agent_email():
    attr = attribute(facts(message=f"feat: x\n\nCo-authored-by: {ANTHROPIC_TRAILER}\n"))
    assert attr.verdict == VERDICT_YES
    assert attr.harness == "claude"
    assert attr.model == "Opus 4.8 (1M context)"
    assert evidence(attr) == [("co-authored-by", ANTHROPIC_TRAILER, "yes")]


def test_co_authored_by_key_is_case_insensitive():
    attr = attribute(facts(message=f"x\n\nco-Authored-By: {ANTHROPIC_TRAILER}\n"))
    assert attr.verdict == VERDICT_YES
    assert attr.harness == "claude"


def test_model_normalization_collapses_whitespace():
    raw = "Claude  Opus   4.8 (1M context) <noreply@anthropic.com>"
    attr = attribute(facts(message=f"x\n\nCo-authored-by: {raw}\n"))
    assert attr.model == "Opus 4.8 (1M context)"


def test_co_authored_by_aider_model_parsed_from_name():
    raw = "Aider (claude-opus-4-8) <aider@aider.chat>"
    attr = attribute(facts(message=f"x\n\nCo-authored-by: {raw}\n"))
    assert attr.verdict == VERDICT_YES
    assert attr.harness == "aider"
    assert attr.model == "claude-opus-4-8"
    assert evidence(attr) == [("co-authored-by", raw, "yes")]


def test_co_authored_by_opencode_names_no_model():
    raw = "opencode <noreply@opencode.ai>"
    attr = attribute(facts(message=f"x\n\nCo-authored-by: {raw}\n"))
    assert attr.harness == "opencode"
    assert attr.model is None


def test_co_authored_by_matches_email_never_display_name():
    # a human email under an agent-looking display name matches nothing
    attr = attribute(facts(message="x\n\nCo-authored-by: Claude Opus <daniel@example.org>\n"))
    assert attr.verdict == VERDICT_NO_EVIDENCE
    # a name-only trailer carries no email to key on
    attr = attribute(facts(message="x\n\nCo-authored-by: Claude Opus\n"))
    assert attr.verdict == VERDICT_NO_EVIDENCE


# ------------------------------------------------- rule 2: identity e-mail --


def test_author_email_github_bot_noreply_numeric_prefix():
    attr = attribute(facts(author_email="198982749+Copilot@users.noreply.github.com"))
    assert attr.verdict == VERDICT_YES
    assert attr.harness == "copilot"
    assert evidence(attr) == [
        ("agent-email", "198982749+Copilot@users.noreply.github.com", "yes")
    ]


def test_committer_email_bot_slug_noreply():
    attr = attribute(facts(committer_email="dependabot[bot]@users.noreply.github.com"))
    assert attr.verdict == VERDICT_YES
    assert attr.harness == "dependabot"


def test_human_github_noreply_is_never_agent_evidence():
    attr = attribute(facts(author_email="12345+daniel@users.noreply.github.com"))
    assert attr.verdict == VERDICT_NO_EVIDENCE


def test_known_agent_domains():
    for email, harness in (
        ("noreply@anthropic.com", "claude"),
        ("noreply@openai.com", "codex"),
        ("noreply@opencode.ai", "opencode"),
        ("someone@aider.chat", "aider"),
        ("cursor@cursor.com", "cursor"),
    ):
        attr = attribute(facts(author_email=email))
        assert (attr.verdict, attr.harness) == (VERDICT_YES, harness), email


# --------------------------------------------------- rule 3: name suffixes --


def test_name_suffix_aider():
    attr = attribute(facts(author_name="Daniel Herrmann (aider)"))
    assert attr.verdict == VERDICT_YES
    assert attr.harness == "aider"
    assert evidence(attr) == [("name-suffix", "Daniel Herrmann (aider)", "yes")]


def test_name_suffix_bot():
    attr = attribute(facts(committer_name="dependabot[bot]"))
    assert attr.verdict == VERDICT_YES
    assert attr.harness == "dependabot"


def test_name_cursor_agent():
    attr = attribute(facts(author_name="Cursor Agent"))
    assert attr.verdict == VERDICT_YES
    assert attr.harness == "cursor"


# ------------------------------------------------ rule 4: body signatures --


@pytest.mark.parametrize(
    ("line", "harness"),
    [
        ("🤖 Generated with [Claude Code](https://claude.com/claude-code)", "claude"),
        ("Generated with Codex.", "codex"),
        ("✨ Generated with [Gemini CLI](https://github.com/google-gemini/gemini-cli)", "gemini"),
        ("Made with Cursor", "cursor"),
    ],
)
def test_body_signatures(line, harness):
    attr = attribute(facts(message=f"feat: x\n\n{line}\n"))
    assert attr.verdict == VERDICT_YES
    assert attr.harness == harness
    assert evidence(attr) == [("body-signature", line, "yes")]


# --------------------------------------------------- rule 5: tool trailers --


def test_assisted_by_agent_colon_model():
    attr = attribute(facts(message="x\n\nAssisted-by: claude-code:Opus 4.8 (1M context)\n"))
    assert attr.verdict == VERDICT_YES
    assert attr.harness == "claude"
    assert attr.model == "Opus 4.8 (1M context)"
    assert evidence(attr) == [
        ("tool-trailer", "claude-code:Opus 4.8 (1M context)", "yes")
    ]


def test_assisted_by_uppercase_agent_colon_model():
    attr = attribute(facts(message="x\n\nAssisted-by: GENIE:Some Model 2\n"))
    assert attr.harness == "genie"
    assert attr.model == "Some Model 2"


def test_assisted_by_kernel_llm_tools_form():
    attr = attribute(facts(message="x\n\nAssisted-by: LLM [Aider]\n"))
    assert attr.verdict == VERDICT_YES
    assert attr.harness == "llm"
    assert attr.model is None
    assert evidence(attr) == [("tool-trailer", "LLM [Aider]", "yes")]


def test_generated_by_bare_agent():
    attr = attribute(facts(message="x\n\nGenerated-by: codex\n"))
    assert attr.harness == "codex"
    assert attr.model is None


def test_agent_logs_url_is_evidence_without_harness():
    attr = attribute(facts(message="x\n\nAgent-Logs-Url: https://logs.example/1\n"))
    assert attr.verdict == VERDICT_YES
    assert attr.harness is None
    assert attr.model is None


def test_claude_session_trailer():
    attr = attribute(facts(message="x\n\nClaude-Session: 0123-abcd\n"))
    assert attr.harness == "claude"


# ------------------------------------------ rule 6: branch prefixes (weak) --


def test_branch_prefix_github_merge_subject():
    subject = "Merge pull request #5 from daniel/claude/feature-x"
    attr = attribute(facts(title=subject, message=subject))
    assert attr.verdict == VERDICT_POSSIBLE
    assert attr.harness == "claude"
    assert evidence(attr) == [("branch-prefix", "claude/feature-x", "possible")]


def test_branch_prefix_git_merge_subject():
    subject = "Merge branch 'codex/thing'"
    attr = attribute(facts(title=subject, message=subject))
    assert attr.verdict == VERDICT_POSSIBLE
    assert attr.harness == "codex"
    assert evidence(attr) == [("branch-prefix", "codex/thing", "possible")]


def test_branch_prefix_remote_tracking_subject_strips_remote():
    attr = attribute(
        facts(
            title="Merge remote-tracking branch 'origin/claude/t2'",
            message="Merge remote-tracking branch 'origin/claude/t2'",
        )
    )
    assert attr.verdict == VERDICT_POSSIBLE
    assert evidence(attr) == [("branch-prefix", "claude/t2", "possible")]


def test_plain_merge_subject_is_no_evidence():
    subject = "Merge branch 'feature/stuff'"
    attr = attribute(facts(title=subject, message=subject))
    assert attr.verdict == VERDICT_NO_EVIDENCE


# ------------------------------------------------------------- precedence --


def test_yes_outranks_possible():
    msg = (
        "Merge pull request #5 from daniel/claude/feature-x\n\n"
        f"Co-authored-by: {ANTHROPIC_TRAILER}"
    )
    attr = attribute(
        facts(
            title="Merge pull request #5 from daniel/claude/feature-x",
            message=msg,
        )
    )
    assert attr.verdict == VERDICT_YES
    assert attr.harness == "claude"
    assert attr.model == "Opus 4.8 (1M context)"
    rules = [r for r, _raw, _v in evidence(attr)]
    assert rules == ["co-authored-by", "branch-prefix"]  # rank order


def test_harness_comes_from_lowest_ranked_hit():
    # rule 2 (agent-email) outranks rule 3 (name-suffix): harness opencode
    attr = attribute(facts(author_name="Cursor Agent", author_email="noreply@opencode.ai"))
    assert attr.verdict == VERDICT_YES
    assert attr.harness == "opencode"


# ------------------------------------------------- GitHub squash framing --


SQUASH_MESSAGE = """feat: add the thing (#123)

* feat: add core

  Assisted-by: claude-code:Opus 4.8 (1M context)

* fix typo

Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
"""


def test_squash_framing_trailers_match_like_raw_trailers():
    attr = attribute(facts(title="feat: add the thing (#123)", message=SQUASH_MESSAGE))
    assert attr.verdict == VERDICT_YES
    assert evidence(attr) == [
        ("co-authored-by", ANTHROPIC_TRAILER, "yes"),
        ("tool-trailer", "claude-code:Opus 4.8 (1M context)", "yes"),
    ]


def test_squash_summary_line_matches_raw_trailer_identically():
    raw = attribute(facts(message=f"x\n\nCo-authored-by: {ANTHROPIC_TRAILER}\n"))
    squash = attribute(facts(message=SQUASH_MESSAGE))
    raw_rows = [e for e in evidence(raw) if e[0] == "co-authored-by"]
    squash_rows = [e for e in evidence(squash) if e[0] == "co-authored-by"]
    assert raw_rows == squash_rows


# --------------------------------------------------------- no-evidence --


def test_plain_commit_is_no_evidence():
    attr = attribute(facts(title="fix: correct rounding", message="fix: correct rounding\n"))
    assert attr.verdict == VERDICT_NO_EVIDENCE
    assert attr.harness is None
    assert attr.model is None
    assert attr.evidence == ()


def test_attribute_is_deterministic():
    f = facts(message=SQUASH_MESSAGE)
    assert attribute(f) == attribute(f)
