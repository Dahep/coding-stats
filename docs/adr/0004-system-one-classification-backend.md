# ADR 0004: System One (decision-model) classification — rejected

## Status: rejected (kept as a measured record; full experiment in git history, commit `d97e23d`)

## Context

Commit classification is a narrow six-verdict choice — exactly the primitive
Jev (TypeSafe AI's System One model, Sep 2026) was built for: a typed choice
with calibrated per-option probabilities, one non-autoregressive pass, no
generated text. A prototype backend was implemented and A/B-tested on this
repository against the ADR-0001 rolling LLM session.

## Measured (19 first-parent commits, Zen route, 81,273 input tokens ≈ $0.0034)

- 95% verdict-level agreement with the committed LLM session (18/19),
  89% at the k-featured level; zero unparsable replies; 17 s wall-clock.
- Both disagreements arrived at confidence < 0.7 (0.40 / 0.51); 12/19 rows
  reached ≥ 0.7 — a hybrid gate would catch them at ~37% escalation.
- Blocking finding: a decision model cannot generate text, so Feature
  canonicalization ("2–6 word title + one sentence about", the human-facing
  identity ADR-0001 commits to) degrades to verbatim commit subjects with an
  empty `about` column — see `apply_reply`'s title/about fallback. Fixing it
  needs an LLM in the write path anyway (a micro-summarizer per `new`
  verdict), which reintroduces the session machinery this backend existed to
  avoid.

## Decision

Dropped. `skill-stats` classifies commits through the opencode CLI only
(ADR-0001); the backend protocol, System One client, and A/B harness were
removed. Practical acceptance criteria that this run surfaced for the record:

- Jev is not reachable through the opencode CLI's model surface, so any
  System One future would be a direct-HTTP client with its own auth story.
- The Zen gateway alias is pinned (`jev-1.13`; `jev-latest` rejected) and was
  live Oct 2026, priced at $0.042/MTok input (output free).
- If the tool ever outgrows per-commit LLM cost, the measured numbers above
  — plus a confidence-gated hybrid — are the return path.
