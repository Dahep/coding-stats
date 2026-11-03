# ADR 0008: Commit verdict taxonomy and fix attribution

## Status: accepted — the implementation specs are #48 (the deterministic
derive stage: fix attribution, areas, embedded fixes, live-lines accounting)
and #49 (the verdict vocabulary's `enhancement` and `chore`, the revert
rules); the audit surface over the recorded provenance is #51. The metrics
consumption is #50.

## Context & decision

The verdict vocabulary (`feature | fix | refactor | revert | cleanup`) and
the fix-attribution rules were sized for a single-human pilot repo. The
extraction-logic review of 2026-11-01 found them systematically misdirecting
repair credit: Enhancements sat invisibly inside `feature` verdicts; fix
lineage stopped at Enhancement commits and died at Refactor/Cleanup commits;
insert-only fixes had no targets; reverts — the loudest quality signal in
the data — counted for nothing because CONTEXT.md said a Revert is never a
Fix; and repair work hidden inside feature PRs (the squash-merge norm) was
invisible. The decisions below restate the taxonomy and fix attribution;
they are recorded before the implementation specs so their implementers work
against settled vocabulary.

The verdicts are **`feature`, `enhancement`, `fix` (subtype `repair` or
`revert`), `refactor`, `cleanup` and `chore` (subtype `docs`, `build`,
`test`, `deps` or `other`)**, plus the system verdicts **`merge`** and
**`unknown`**. Blame evidence is a hint supplied to the model, never the
verdict; and every verdict records how it was obtained (ADR-0009's verdict
source).

1. **Chore is upkeep that changes no product behaviour** — docs,
   build/CI config, tests, dependencies (`chore` subtypes `docs`, `build`,
   `test`, `deps`, `other`). It counts as neither a Feature nor a Fix, is
   never blamed as a fixing commit, and is never skipped by blame (unlike
   Refactor and Cleanup, blame passes through it), and its lines are
   unowned. A commit that mixes upkeep with functional change takes the
   functional verdict: `chore` never hides a Feature, Enhancement or Fix.
   Adding or maintaining upkeep is `chore`; repairing a defect in it is a
   `fix` — an Upkeep fix by Area (decision 6), never left inside `chore`.
2. **An Enhancement extends an existing Feature and links to that Feature's
   root Feature** — never to another Enhancement, so Features never form
   trees. It is not counted in features/year; fixes to an Enhancement's
   lines count against the root Feature it links to.
3. **Reverts are Fixes** (subtype `revert`). This supersedes CONTEXT.md's
   "a Revert is never counted as a Fix" and DESIGN.md's "reverts as
   `revert` (never as fix)": a Revert usually means the reverted change was
   defective — exactly the quality signal the tool exists to measure. The
   target comes from the `This reverts commit <sha>` line when present
   (blame cannot find it when the reverted change only deleted lines),
   falling back to blame for hand-written reverts. A revert of a Revert is
   a **Re-land**: it takes on the original commit's verdict and links, and
   the re-landing itself is never counted as a fix — the counts it rejoins
   are the original commit's, so re-landing does not inflate fix counts.
   Reverts made
   for non-quality reasons still count — the `revert` subtype keeps them
   separable, and #51 lists them for review.
4. **Fixes after Refactor or Cleanup count against the original Feature.**
   Blame skips Refactor and Cleanup commits; where git still credits a line
   to one (moved files, split helpers, rewritten blocks), the Refactor or
   Cleanup commit takes on the Features of the lines it replaced
   (**inherited resolution**), and a fix to those lines attributes through
   it.
5. **Embedded fixes.** A Feature or Enhancement commit that rewrites at
   least a threshold number of another Feature's lines counts as a fix of
   that Feature — the squash-merge norm hides repair work inside feature
   PRs, and a minimum threshold keeps incidental contact with shared files
   (imports, registries, configs) from creating fake fixes. A commit's
   edits to its own Feature's lines are ordinary feature evolution, never
   a fix.
6. **The verdict states intent and never location: any repair is a `fix`.**
   Each changed file belongs to one **Area** — `product`, `test`, `build`
   or `docs` — from ordered path rules: repo rules first, then built-in
   defaults, with unmatched paths counted as `product`. Only product-area
   fix hits reach Features; hits in `test`, `build` or `docs` files are
   **Upkeep fixes** of their Area, whichever commit wrote the lines.
   Upkeep fixes
   are reported as their own rates, never in fixes/feature and never as
   uncovered — a broken test or workflow is not a defect of the Feature,
   and this keeps tests written in separate commits counting the same way
   as tests written with the Feature. The effective rules ride the
   Artifact.
7. **Every resolution is recorded with its provenance** — direct,
   adjacency (insert-only fixes, matched through the lines next to the
   insertion), inherited (through a named Refactor/Cleanup commit),
   revert-message — plus the Area of each hit and the hit-line count, so
   every attribution is auditable (#51).

## Considered Options

- **Keeping Reverts outside fix counts** (the prior ruling): threw away the
  loudest quality signal — reverting a change is the response to its
  defect.
- **Folding docs/build/CI/test commits into `cleanup` or `refactor`**:
  rejected because those commits are new content, not cosmetic rewrites of
  existing lines — and Cleanup/Refactor are skipped by blame, which would
  make upkeep invisible exactly where the chain needs to pass through.
- **Counting test, CI and docs fixes against the Feature that owned the
  lines**: a broken test or workflow is not a defect of the Feature, and
  tests written in separate commits would count differently from tests
  written with the Feature. Areas keep the signal without misdirecting it.
- **Letting the model decide a fix's Area**: path rules are deterministic,
  cheap, auditable and configurable per repo; a model guess is none of
  those.
- **Treating enhancements as plain `feature` commits linked `touches`**:
  verdict counts and Feature counts disagree without explanation (18 vs 16
  on the dogfood artifact), and features/year inflates by growth.
- **Leaving lineage restricted to `defines`**: a fix repairing lines
  written by an Enhancement is never credited to the Feature, though the
  same Enhancement's lines count toward the Feature's live lines.

## Consequences

- The verdict vocabulary, subtypes and fix attribution rules are the
  contract for #48 (deterministic derive) and #49 (classification); #50
  consumes them (fix events, landing dates, upkeep rates); #51 audits the
  provenance.
- CONTEXT.md's Revert entry changes meaning; downstream text and metrics
  that treated Reverts as non-fixes are updated with this ADR.
- Blame skipping Refactor/Cleanup plus inherited resolution makes derived
  attribution deterministic and run-history-independent: the derive stage
  (#48) rebuilds it from scratch every run.
