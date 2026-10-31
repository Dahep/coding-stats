# Commit-attribution research: the graded signature-rule table

The research basis for ADR-0007's detection engine. The table below is the
normative rule set tickets #34/#35 implement — once, in
`src/coding_stats/aiattr.py` — and both evidence channels run through it:
the git-only walk (ticket #34, rows re-derived as pure functions of the
Target branch) and the PR channel (ticket #35, rows recorded per PR).

## Why signature rules

Attribution here is a *detection* problem, not a classification one: a
commit carries machine-written signatures — trailers, known bot emails,
body boilerplate — that are cheap, deterministic and auditable to match.
The rules are graded so a report never overclaims: a strong signature
supports a `yes`, a weak convention only a `possible`, and silence is
exactly that (`no-evidence`, never "human"). Everything is matched on the
**raw** identity strings, before any mailmap/alias resolution — the raw
string is what the agent wrote, and it is what the evidence row stores.
No LLM is involved anywhere in detection.

## The graded rule table

| # | rule id | source | matched on | tier |
|---|---------|--------|------------|------|
| 1 | `co-authored-by` | `Co-authored-by`/`Co-Authored-By` trailer (case-insensitive) | the **email**, never the display name | **yes** |
| 2 | `agent-email` | author/committer email: known agent domain or GitHub bot noreply (numeric-prefix aware, e.g. `198982749+Copilot@users.noreply.github.com`) | the email | **yes** |
| 3 | `name-suffix` | author/committer name patterns: `(aider)`, `[bot]`, `Cursor Agent` | name only | **yes** |
| 4 | `body-signature` | signature lines in the message body: `🤖 Generated with [Claude Code]`, `Generated with Codex.`, `Gemini CLI`, `Made with Cursor` | the line | **yes** |
| 5 | `tool-trailer` | tool trailers: `Assisted-by:` (accepts `agent:model`, `AGENT:MODEL`, kernel `LLM [tools]`), `Generated-by:`, `Agent-Logs-Url:`, `Claude-Session:` | the trailer value | **yes** |
| 6 | `branch-prefix` | the source branch named in a merge subject carries an agent prefix (`claude/`, `codex/`) | the branch name | **possible** |

**Yes-tier: rules 1–5. Possible-tier: rule 6 only.** Repo-level config
artifacts (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`, …) are likewise
**possible**-tier signals at most — never per-commit `yes`.

Verdict per commit (and per PR, summarized the same way):

- `yes` — a rule 1–5 signature matched;
- `possible` — only weak signals (rule 6, config artifacts) matched;
- `no-evidence` — nothing matched. Absence of a signature, never a claim
  about authorship.

## Matching semantics

- **Email-keyed trailers (rule 1).** Display names vary wildly (`Claude`,
  `claude`, `Anthropic Claude`, …); the email is the identity. Known agent
  domains: `noreply@anthropic.com` (claude), `noreply@openai.com` (codex),
  `noreply@opencode.ai` (opencode), `aider@aider.chat` (aider),
  cursor domains. GitHub bot noreplies (`slug[bot]@users.noreply.github.com`,
  numeric-prefix aware) name their own harness.
- **Model parsing, only where a model is actually named.** Claude names put
  the model in the display name (`Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
  → harness `claude`, model `Opus 4.8 (1M context)`); Aider puts it in the
  trailer value (`aider (claude-opus-4-8)` → model `claude-opus-4-8`).
  Everything else stores the model as unknown rather than inventing one.
- **GitHub squash framing is commit-object text.** Per-commit trailers
  inside the `*`-prefixed squash body block and the trailing summary
  `Co-authored-by:` line match identically to raw trailers — same table, no
  special case.
- **Tool trailers (rule 5)** accept the several spellings the harnesses
  emit: `Assisted-by: agent:model`, `AGENT:MODEL`, kernel-style
  `LLM [tools]`, plus `Generated-by:`, `Agent-Logs-Url:`, `Claude-Session:`.
- **Deterministic.** Same commit, same rows — no network, no token, no
  clock. The rule engine is pure stdlib+sqlite.

## PR-level signals (ticket #35) — the same table

The PR tier feeds PR-list derived signals through the identical rule table
(`aiattr.pr_matches`), never a second engine:

| PR-list source | rules applied | tier |
|----------------|---------------|------|
| PR body footers/signatures | 1 / 4 / 5 (the raw matched line recorded verbatim) | yes-tier as above |
| PR author identity | 3 over the author login (`[bot]`, `(aider)` suffixes, `Cursor Agent`); a GitHub bot login without a pattern still names its own harness | yes-tier as above |
| head branch name | 6's prefix matcher | **possible** at most |

Head-commit messages read via `refs/pull/N/head` run through the ordinary
commit rules. Suspicious rows (a head ref that failed its parentage check)
are recorded for audit and never contribute to any verdict or identity.

## Implementation map

- `aiattr.matches(CommitFacts)` / `aiattr.attribute(...)` — git channel
  (rules over author/committer names and emails plus message text);
  rows land in `ai_evidence` + `ai_attribution`, re-derived on recompute.
- `aiattr.pr_matches(...)` / `pr_attribution(...)` — PR channel; rows land
  in `pr_evidence` (channel `pr`, recorded once per (PR, commit, rule),
  replaced per PR on re-runs).
- `prsweep.effective_attribution(...)` — channel-precedence resolution:
  git-first, strongest tier wins for the verdict, suspicious rows excluded.
- Ranking (`aiattr.RULE_RANK`) orders evidence 1–6 for harness/model
  attribution and report display; the verdict needs only the tier.

## Limits (accepted)

- Rule 3's name patterns can fire on a human who happens to style
  themselves `Something [bot]`; rule 6 is never more than `possible` for
  exactly this reason. The evidence row keeps the raw string so a reviewer
  can audit any attribution.
- A human committing with an agent's configured email is indistinguishable
  from the agent at this tier — the rows record *signatures*, not authors.
