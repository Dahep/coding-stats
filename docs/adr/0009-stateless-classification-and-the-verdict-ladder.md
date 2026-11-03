# ADR 0009: Stateless classification and the verdict ladder

## Status: accepted — supersedes the rolling-session clause of ADR-0001
(that ADR's status line says so). The implementation spec is #49; #51
audits the recorded sources and failures.

## Context & decision

Classification ran every commit through one rolling opencode session.
Every prompt already carried the full Feature registry, so session memory
added nothing but cost: each turn grew the context by up to 12 KB of diff,
the session id stayed out of the Artifact so CI started fresh every run
while local runs continued an old session — and the same commit could get
a different verdict depending on where it ran. The session also made
failures structural: one malformed reply (`existing` without an `f` field,
or citing an unknown Feature id) crashed the run or stalled it
permanently, because the next run retries the same commit first
(head-of-line blocking); a rejected API key looked like a green run (the
dogfood CI classified 0 commits since #31 unnoticed); and a single
unparsable reply lost a commit's classification for good.

1. **Classification is stateless.** Each commit is one fresh LLM call, in
walk order. The prompt carries the Feature registry (ordered by recent
activity, with Features named by the evidence always included), the blame
evidence, and a diff under a per-file budget (the budget is never spent
entirely on a lockfile or generated file). The rolling session and its
stored id are dropped.

2. **The verdict ladder** drives `unknown` as close to zero as possible
while recording how every verdict was obtained. Steps 1–2 run in the
commit's first run; a commit that exhausts them is stored `unknown` and
retried on a later run by steps 3–4, after which the tail (5–6) applies.
In this order:

   1. **Rules** — revert and re-land from the message (`git revert` writes
   the reverted sha into the message; no LLM call), and the empty-diff
   `merge` system verdict.
   2. **Primary model**, with one corrective re-ask naming the exact
   problem (missing field, unknown id, bad JSON) — a fixable mistake is
   fixed within the same run, and one bad reply never blocks later
   commits.
   3. **Reduced prompt on a later run** — summary, message and evidence,
   no diff body — retrying `unknown` commits.
   4. **Fallback model** — one model's blind spot is not every model's.
   5. **Conventional Commits message-prefix heuristic** — `feat`, `fix`,
   `revert`, `refactor`, `perf`, `style`, and `docs`/`build`/`ci`/`test`/
   `deps`/`chore` → `chore` subtypes.
   6. **`unknown`**, with the failure reason, attempts and last raw reply
   stored for the audit (#51).

Every verdict records its source; the recorded column refines these ladder
steps to #49's source vocabulary (`rule-revert`, `rule-reland`, `llm`,
`llm-reduced`, `llm-fallback`, `heuristic-prefix`, `system`).

An `unknown` verdict is RETRY-PENDING until the ladder is exhausted and
TERMINAL after step 6 — distinguished by the stored attempts and failure
reason. Within a later run the retries run in the listed order (reduced
prompt first, then the fallback model, then the heuristic tail); a commit
never attempted for lack of a key is the exit policy's backlog, not an
`unknown` — it has no failure reason to store.

3. **The exit policy**: only transport and auth failures stop
classification. A key that was provided but rejected fails the CI run
after the Artifact lands — rejected keys are loud (a `::error::` naming
the auth problem), while a missing key stays green with a visible backlog
notice, and transient failures warn. The deterministic stages always
complete and the Artifact always lands before any such failure.

## Considered Options

- **Keeping the rolling session** (ADR-0001's original clause): added cost
  and non-determinism for no benefit — the registry was already in every
  prompt — and made verdicts run-location-dependent.
- **One batch LLM call for many commits**: cheaper, but loses the
  per-commit corrective re-ask and concentrates failure (one bad reply
  poisons the batch).
- **Treating a rejected key like a missing key** (green with backlog):
  this is precisely how the dogfood backlog grew unnoticed since #31.
- **Dropping the message-prefix heuristic**: conventional repositories
  would end with `unknown` verdicts that a free, deterministic rule could
  resolve; the source column keeps its verdicts auditable and separable.

## Consequences

- ADR-0001's resumability and chronological framing stay in force, but
  resumption state is the stored `unknown` backlog plus the walk position,
  not a session id.
- Verdict reproducibility improves (same commit, same prompt shape,
  deterministic ladder) but is still bounded by the models; the recorded
  source and raw replies make every verdict inspectable (#51).
- CI classification failures can no longer pass silently: rejected keys
  fail the run visibly after the Artifact lands; missing keys keep the
  backlog visible.
