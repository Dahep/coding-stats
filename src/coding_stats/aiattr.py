"""Git-only AI attribution detection (ticket #34, the ADR-0007 baseline tier).

Per commit, from git data alone: a 3-state verdict plus auditable evidence
rows. The ranked rule table is normative in issue #34:

1. ``co-authored-by`` — Co-authored-by trailer, EMAIL-keyed (a display name
   never matches): a known agent email or GitHub bot noreply. ``yes``.
2. ``agent-email``  — author/committer email: known agent domain or GitHub
   bot noreply (numeric-prefix aware). ``yes``.
3. ``name-suffix``  — author/committer name patterns ((aider), [bot],
   Cursor Agent), name-only. ``yes``.
4. ``body-signature`` — signature lines in the message body. ``yes``.
5. ``tool-trailer`` — Assisted-by / Generated-by / Agent-Logs-Url /
   Claude-Session trailers. ``yes``.
6. ``branch-prefix`` — the source branch named in a merge subject carries an
   agent prefix (claude/, codex/). ``possible`` at most — a weak signal.

Everything matched is scanned over the WHOLE message text: GitHub squash
framing (trailer lines inside the ``*``-prefixed squash body block and the
trailing summary Co-authored-by line) is commit-object text and matches the
same rule table as raw trailers. Identity matching runs on the RAW strings,
before any mailmap resolution (git's lowercase ``%an``/``%ae``-class
placeholders — this module never sees a resolved identity).

The verdict is ``yes`` (rules 1-5 matched), ``possible`` (rule 6 only) or
``no-evidence`` (nothing matched). ``no-evidence`` is exactly that — absence
of a signature — and is never a claim about authorship. Detection is a pure
function of the commit's git content: same commit, same rows.

Model names are parsed only where the harness puts one in the trailer:
Claude (name minus its harness word: ``Claude Opus 4.8 (1M context)`` ->
``Opus 4.8 (1M context)``) and Aider (``aider (claude-opus-4-8)`` ->
``claude-opus-4-8``); everything else stores the model as unknown rather
than inventing one. The rule engine is pure stdlib+sqlite: no git, no
network — the walk feeds it facts read straight from the commit objects.

Ticket #35's PR tier reuses this same table — never a second engine: it
feeds head commits the ordinary ``CommitFacts`` and PR-level signals (body
footers, PR author identity, head branch name) through ``pr_matches``.
"""

import re
import sqlite3
from dataclasses import dataclass

VERDICT_YES = "yes"
VERDICT_POSSIBLE = "possible"
VERDICT_NO_EVIDENCE = "no-evidence"

RULE_CO_AUTHORED = "co-authored-by"
RULE_AGENT_EMAIL = "agent-email"
RULE_NAME_SUFFIX = "name-suffix"
RULE_BODY_SIGNATURE = "body-signature"
RULE_TOOL_TRAILER = "tool-trailer"
RULE_BRANCH_PREFIX = "branch-prefix"

# ranked rule ids (issue #34's rule table order): the rank orders evidence
# for harness/model attribution and the report; the verdict needs only the
# tier (rules 1-5 yes, rule 6 possible)
RULE_RANK: dict[str, int] = {
    RULE_CO_AUTHORED: 1,
    RULE_AGENT_EMAIL: 2,
    RULE_NAME_SUFFIX: 3,
    RULE_BODY_SIGNATURE: 4,
    RULE_TOOL_TRAILER: 5,
    RULE_BRANCH_PREFIX: 6,
}

_RULE_TIER: dict[str, str] = {
    rule: VERDICT_YES if rank < 6 else VERDICT_POSSIBLE for rule, rank in RULE_RANK.items()
}


@dataclass(frozen=True)
class CommitFacts:
    """The raw (pre-mailmap) identity strings plus message text of one
    commit — everything the rule table reads."""

    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    title: str
    message: str


@dataclass(frozen=True)
class Evidence:
    """One auditable match: rule id + the raw matched string + its tier."""

    rule: str
    raw: str
    verdict: str  # 'yes' | 'possible'


@dataclass(frozen=True)
class Match:
    """One rule-table match with the identity (harness/model) its raw string
    names — the per-row view attribute() summarizes per commit. The PR tier
    (ticket #35) records rows at exactly this granularity."""

    rule: str
    raw: str
    verdict: str  # 'yes' | 'possible'
    harness: str | None
    model: str | None


@dataclass(frozen=True)
class Attribution:
    """The per-commit verdict with the harness/model the evidence names
    (both None when no evidence names one) and the evidence rows."""

    verdict: str  # 'yes' | 'possible' | 'no-evidence'
    harness: str | None
    model: str | None
    evidence: tuple[Evidence, ...]  # sorted by (rule rank, raw)


@dataclass(frozen=True)
class _Hit:
    evidence: Evidence
    harness: str | None
    model: str | None


# known agent identities, matched on the EMAIL (rule 1) or author/committer
# email (rule 2), never on display names
_AGENT_DOMAINS: dict[str, str] = {
    "anthropic.com": "claude",
    "openai.com": "codex",
    "opencode.ai": "opencode",
    "aider.chat": "aider",
    "cursor.com": "cursor",
}

# canonical harness names for agent tokens found in tool trailers / names
_HARNESS_ALIASES: dict[str, str] = {
    "claude-code": "claude",
    "claude code": "claude",
    "gemini-cli": "gemini",
    "gemini cli": "gemini",
    "cursor-agent": "cursor",
    "cursor agent": "cursor",
}

_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("generated with [claude code]", "claude"),
    ("generated with codex", "codex"),
    ("gemini cli", "gemini"),
    ("made with cursor", "cursor"),
)

_TOOL_TRAILER_KEYS = frozenset({"assisted-by", "generated-by", "agent-logs-url", "claude-session"})

_BRANCH_PREFIXES: tuple[tuple[str, str], ...] = (("claude/", "claude"), ("codex/", "codex"))

_TRAILER_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)\s*:\s*(.*?)\s*$")
_ANGLE_EMAIL_RE = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")
_PR_MERGE_RE = re.compile(r"^merge pull request #\d+ from\s+(\S+)", re.I)
_BRANCH_MERGE_RE = re.compile(r"^merge (remote-tracking )?branch '([^']+)'", re.I)
_TOOL_VALUE_RE = re.compile(r"^([^:\s]+)\s*:\s*(.+)$")
_KERNEL_LLM_RE = re.compile(r"^llm\s*\[[^\]]*\]$", re.I)
_AIDER_NAME_RE = re.compile(r"^aider\s*\(([^)]+)\)", re.I)


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _email_harness(email: str) -> str | None:
    """The harness one email keys on (rules 1 and 2 share the matcher).
    GitHub bot noreply is numeric-prefix aware; a plain user noreply is a
    human GitHub address and never matches."""
    e = email.strip().lower()
    local, at, domain = e.rpartition("@")
    if not at:
        return None
    if domain == "users.noreply.github.com":
        login = re.sub(r"^\d+\+", "", local)
        if login.endswith("[bot]"):
            return login[: -len("[bot]")].strip() or "bot"
        return "copilot" if login == "copilot" else None
    return _AGENT_DOMAINS.get(domain)


def _name_harness(name: str) -> str | None:
    """Rule 3's name-only patterns: [bot] and (aider) suffixes, Cursor Agent."""
    n = name.strip()
    low = n.lower()
    if low.endswith("[bot]"):
        return n[: -len("[bot]")].strip().lower() or "bot"
    if low.endswith("(aider)"):
        return "aider"
    if low == "cursor agent":
        return "cursor"
    return None


def _harness_alias(token: str) -> str | None:
    t = _collapse(token).lower()
    return _HARNESS_ALIASES.get(t, t) or None


def _model_from_name(harness: str | None, name: str) -> str | None:
    """Rule 1's model parse, only for the harnesses that put one in the
    name: Claude (the name minus its harness word) and Aider (the parenthetical)."""
    n = _collapse(name)
    if harness == "claude":
        low = n.lower()
        for prefix in ("claude code", "claude"):
            if low.startswith(prefix):
                n = n[len(prefix) :].strip()
                break
        return n or None
    if harness == "aider":
        m = _AIDER_NAME_RE.match(n)
        return _collapse(m.group(1)) if m else None
    return None


def _coauthored_email(value: str) -> str | None:
    """The trailer's email (rule 1 is email-keyed); None without one — a
    display name never matches."""
    m = _ANGLE_EMAIL_RE.search(value)
    if m:
        return m.group(1)
    v = value.strip()
    if "@" in v and " " not in v and "<" not in v:
        return v
    return None


def _tool_value_harness(value: str) -> tuple[str | None, str | None]:
    """Assisted-by/Generated-by value shapes (issue #34): agent:model,
    AGENT:MODEL, the kernel's 'LLM [tools]', or a bare agent."""
    v = value.strip()
    if _KERNEL_LLM_RE.match(v):
        return "llm", None
    m = _TOOL_VALUE_RE.match(v)
    if m:
        return _harness_alias(m.group(1)), _collapse(m.group(2)) or None
    return _harness_alias(v), None


def _branch_hit(branch: str) -> tuple[str, str] | None:
    """(harness, raw branch) when a source branch name carries an agent
    prefix (rule 6's matcher, shared by commit subjects and PR head refs)."""
    for prefix, harness in _BRANCH_PREFIXES:
        if branch.lower().startswith(prefix):
            return harness, branch
    return None


def _branch_prefix_hit(title: str) -> tuple[str, str] | None:
    """(harness, raw branch) when the subject names a source branch with an
    agent prefix. GitHub PR merges carry 'from <owner>/<branch>' (the owner
    is stripped); git's own merge subjects carry the branch verbatim, with a
    remote component stripped for remote-tracking merges. A commit that
    names no branch yields nothing — never inferred from anything else."""
    t = title.strip()
    m = _PR_MERGE_RE.match(t)
    if m:
        branch = m.group(1)
        branch = branch.split("/", 1)[1] if "/" in branch else branch
    else:
        m = _BRANCH_MERGE_RE.match(t)
        if not m:
            return None
        branch = m.group(2)
        if m.group(1):  # remote-tracking spelling: first component is the remote
            branch = branch.split("/", 1)[1] if "/" in branch else branch
    return _branch_hit(branch)


def _hit(rule: str, raw: str, harness: str | None, model: str | None = None) -> _Hit:
    return _Hit(Evidence(rule, raw, _RULE_TIER[rule]), harness, model)


def _hits(facts: CommitFacts) -> list[_Hit]:
    hits: list[_Hit] = []
    # rules 1 + 5: trailer-shaped lines anywhere in the message — GitHub
    # squash framing (trailers inside the squash body block, the trailing
    # summary line) is commit-object text and matches like raw trailers
    for line in facts.message.splitlines():
        m = _TRAILER_RE.match(line)
        if m is None:
            continue
        key, value = m.group(1).lower(), m.group(2)
        if key == "co-authored-by":
            email = _coauthored_email(value)
            if email is None:
                continue
            harness = _email_harness(email)
            if harness is None:
                continue
            name = _ANGLE_EMAIL_RE.sub("", value).strip()
            hits.append(_hit(RULE_CO_AUTHORED, value, harness, _model_from_name(harness, name)))
        elif key in _TOOL_TRAILER_KEYS:
            raw = value
            if key == "claude-session":
                harness, model = "claude", None
            elif key == "agent-logs-url":
                harness, model = None, None
            else:
                harness, model = _tool_value_harness(value)
            hits.append(_hit(RULE_TOOL_TRAILER, raw, harness, model))
    # rule 4: body signature lines
    for line in facts.message.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        for needle, harness in _SIGNATURES:
            if needle in low:
                hits.append(_hit(RULE_BODY_SIGNATURE, stripped, harness))
    # rule 2: author/committer emails (raw strings)
    for email in (facts.author_email, facts.committer_email):
        harness = _email_harness(email)
        if harness is not None:
            hits.append(_hit(RULE_AGENT_EMAIL, email.strip(), harness))
    # rule 3: author/committer names (raw strings)
    for name in (facts.author_name, facts.committer_name):
        harness = _name_harness(name)
        if harness is not None:
            hits.append(_hit(RULE_NAME_SUFFIX, name.strip(), harness))
    # rule 6: branch prefix named in the merge subject (possible tier)
    hit = _branch_prefix_hit(facts.title)
    if hit is not None:
        harness, raw = hit
        hits.append(_hit(RULE_BRANCH_PREFIX, raw, harness))
    return hits


def _ranked(hits: list[_Hit]) -> list[_Hit]:
    deduped: dict[tuple[str, str], _Hit] = {}
    for h in hits:
        deduped.setdefault((h.evidence.rule, h.evidence.raw), h)
    return sorted(deduped.values(), key=lambda h: (RULE_RANK[h.evidence.rule], h.evidence.raw))


def _finalize(hits: list[_Hit]) -> Attribution:
    ranked = _ranked(hits)
    if any(h.evidence.verdict == VERDICT_YES for h in ranked):
        verdict = VERDICT_YES
    elif ranked:
        verdict = VERDICT_POSSIBLE
    else:
        verdict = VERDICT_NO_EVIDENCE
    harness = next((h.harness for h in ranked if h.harness is not None), None)
    model = next((h.model for h in ranked if h.model is not None), None)
    return Attribution(verdict, harness, model, tuple(h.evidence for h in ranked))


def matches(facts: CommitFacts) -> tuple[Match, ...]:
    """Every rule-table match of one commit's facts, deduped and ranked,
    each carrying the identity its raw string names. The per-row view behind
    attribute()'s per-commit summary."""
    return tuple(
        Match(h.evidence.rule, h.evidence.raw, h.evidence.verdict, h.harness, h.model)
        for h in _ranked(_hits(facts))
    )


def attribute(facts: CommitFacts) -> Attribution:
    """The verdict + evidence rows of one commit. Pure and deterministic:
    the same facts always produce the same Attribution."""
    return _finalize(_hits(facts))


def pr_matches(
    body: str,
    author_login: str | None,
    author_is_bot: bool,
    head_branch: str | None,
) -> tuple[Match, ...]:
    """PR-level signals (ticket #35) through the SAME rule table — never a
    second engine. Three sources, all PR-list derived (zero extra API calls):

    * body footers — trailer and signature rules over the PR body text
      (rules 1/4/5), the raw matched line recorded verbatim;
    * PR author identity — rule 3's name patterns over the author login
      ([bot] and (aider) suffixes, Cursor Agent); a GitHub bot login without
      a pattern still names its own harness;
    * head branch name — rule 6's prefix matcher, ``possible`` tier at most.
    """
    hits: list[_Hit] = []
    facts = CommitFacts(
        author_name="",
        author_email="",
        committer_name="",
        committer_email="",
        title="",
        message=body or "",
    )
    hits.extend(_hits(facts))
    if head_branch:
        hit = _branch_hit(head_branch)
        if hit is not None:
            harness, raw = hit
            hits.append(_hit(RULE_BRANCH_PREFIX, raw, harness))
    if author_login:
        author_harness = _name_harness(author_login)
        if author_harness is None and author_is_bot:
            author_harness = _collapse(author_login).lower() or "bot"
        if author_harness is not None:
            hits.append(_hit(RULE_NAME_SUFFIX, author_login, author_harness))
    return tuple(
        Match(h.evidence.rule, h.evidence.raw, h.evidence.verdict, h.harness, h.model)
        for h in _ranked(hits)
    )


def pr_attribution(
    body: str,
    author_login: str | None,
    author_is_bot: bool,
    head_branch: str | None,
) -> Attribution:
    """The PR-level signals' combined verdict + evidence, summarized exactly
    like a commit's attribution (same tiers, same ranking)."""
    hits = [
        _Hit(Evidence(m.rule, m.raw, m.verdict), m.harness, m.model)
        for m in pr_matches(body, author_login, author_is_bot, head_branch)
    ]
    return _finalize(hits)


def record(conn: sqlite3.Connection, commit_id: int, facts: CommitFacts) -> Attribution:
    """Store one commit's attribution verdict + evidence rows (idempotent:
    re-recording replaces the commit's rows). Returns the Attribution."""
    attr = attribute(facts)
    conn.execute(
        "INSERT OR REPLACE INTO ai_attribution (commit_id, verdict, harness, model)"
        " VALUES (?, ?, ?, ?)",
        (commit_id, attr.verdict, attr.harness, attr.model),
    )
    conn.execute("DELETE FROM ai_evidence WHERE commit_id = ?", (commit_id,))
    for e in attr.evidence:
        conn.execute(
            "INSERT INTO ai_evidence (commit_id, rule, raw, verdict) VALUES (?, ?, ?, ?)",
            (commit_id, e.rule, e.raw, e.verdict),
        )
    return attr
