# ADR 0004: System One (decision-model) classification as an alternative backend

## Status: accepted (as an alternative backend; measured against ADR-0001 on this repo)

## Context & decision

Commit classification is a narrow, already-structured decision: pick one of
six verdicts. That is exactly the primitive System One models (Jev, Sep 2026,
the first of the class) were built for — a typed choice with a calibrated
probability per option, one non-autoregressive pass, no generated text. The
ADR-0001 rolling LLM session instead pays per commit for an LLM turn and a
brittle JSON-reply parse.

Decision: make the classifier a *protocol* (`Backend.classify(conn,
walk_index) -> (Reply, raw)`) with two implementations:

- `OpencodeBackend` (ADR-0001, unchanged semantics): rolling opencode CLI
  session, JSON reply per turn, one repair turn, conversation carries the
  Feature registry.
- `SystemOneBackend` (this ADR): one POST per commit to a System One API.
  State = title/body/diff (same caps) + the compact Feature registry. Two
  Choice questions: `verdict` (the six rubric options from PROMPT_HEADER)
  and, when Features exist, `feature` (options = Feature ids, criteria null)
  so `existing` verdicts can cite like the LLM path does. Rationale slot
  stores `confidence=… p=…` — a decision model cannot produce prose, and the
  probability distribution is arguably the better audit trail. Invalid or
  uncited answers become `unknown` rows, identical to unparsable LLM replies.

Routes (same request/response shape, key from the environment; exactly one,
else `SKILL_STATS_SYSTEMONE_PROVIDER` must name the vendor so no silent
account-billing precedence):

| route | endpoint | key | model id |
| --- | --- | --- | --- |
| zen | `https://opencode.ai/zen/v1/systemone` | `OPENCODE_API_KEY` | `jev-1.13` (pinned; `jev-latest` rejected upstream — verified live) |
| openrouter | `https://openrouter.ai/api/v1/systemone` | `OPENROUTER_API_KEY` | `typesafe/…`/pass-through |
| typesafe | `https://api.typesafe.ai/v1/systemone` | `TYPESAFE_API_KEY` | native alias |

Backend selection: `init --backend systemone|opencode` forces it, otherwise
the stored model id decides (`jev` in the id → System One). The CI workflow
now passes both `OPENCODE_API_KEY` and `OPENROUTER_API_KEY` secrets, so
company repos (OpenRouter) and personal repos (Go/Zen subscription) work
per-machine without per-repo code.

## Measured: A/B on this repository (19 shared first-parent commits, Zen route)

| | LLM session (ADR-0001) | System One jev-1.13 |
| --- | --- | --- |
| verdict-level agreement (metric-driving) | — | 18/19 (95%) |
| k-level agreement (new vs existing etc.) | — | 17/19 (89%) |
| unparsable / unknown | occasional (JSON parse) | 0 |
| wall-clock | minutes (session turns) | 17.1 s total |
| cost | per-commit LLM turn | 81,273 input tokens ≈ $0.0034 at list price |

Both disagreements arrived with low confidence (0.40 and 0.51) while 12/19
rows reached confidence ≥ 0.7: a hybrid gate (System One first, escalate
confidence < 0.7 to the LLM backend) would have caught both disputes and cut
LLM turns by ~63% on this sample. The two disputed commits:

- `8b9154cc` ("fixes: blame … evidence") — LLM `new`, Jev `fix` (0.50 vs
  0.42): literal reading of the `fixes:` prefix, a documented Jev-1.13
  jaggedness; the roll-up (`fix` vs `feature`) still diverged, not just the
  citation.
- `870a7439` ("feat: … pages deploy opt-in") — LLM `existing`, Jev `new`
  (0.59 vs 0.41): near coin-flip; only k-level diverged.

## Consequences

- Rationale is no longer prose under this backend; consumers of
  `commit_verdicts.rationale` must expect `confidence=… p=…`.
- The rolling session id (`classify_session_id`) is untouched for the LLM
  backend and simply not written for System One.
- The Feature registry now depends on the backend's own verdicts when a run
  starts from scratch — same property as the LLM path; artifacts freeze the
  outcome per run.
- OpenRouter and TypeSafe-direct routes are implemented but were exercised
  only through the Zen route here; the first company-repo run should confirm
  the OpenRouter surface.
