# ADR 0007: AI attribution and the PR-channel evidence tier

## Status: accepted — the spec tickets #34–#38 implement. Clause 3–5 = the
PR-channel sweep (ticket #35); clause 2 = the watermark run machinery
(ticket #36); clause 1 = the composite-action integration (ticket #37). The
git-only baseline detector is ticket #34; the gather/report union of
recorded evidence is ticket #38. The detection engine's research basis is
`docs/commit-attribution-research.md`.

## Context & decision

The Target-branch walk sees only what landed on the branch. PR body
footers, bot-authored titles, and the head branch names of squash-merged
PRs are gone by the time the walk reads the commit object — squash-heavy
repos with branches deleted at merge lose that evidence entirely, and the
walk undercounts AI-assisted work. The tier below reconstructs it **without
weakening the git-only default**: every claim is an auditable evidence row
(rule id + the raw matched string), `no-evidence` is never rendered as
"human", and no LLM call is ever added — the PR tier is detection-only and
the push-run classification cost is unchanged.

Detection is one engine for both channels: the graded signature-rule table
of `docs/commit-attribution-research.md` (ticket #34's rule table,
implemented once in `aiattr`), matched on the RAW identity strings before
any mailmap resolution. The git channel re-derives its rows as pure
functions of the Target branch; the PR channel's rows are *recorded*
observations and never re-derived.

1. **Opt-in PR tier in CI (the `detect-pr` input).** `Dahep/coding-stats@v1`
   gains `detect-pr` (default `false`). Off is exactly today's behavior: the
   pipeline runs byte-identically to the git-only table and no
   `pull-requests: read` permission is requested — the git-only guarantee
   stays the default. On, the run performs the watermark-driven increment
   (clause 2) after the deterministic stages and before the report step;
   detection-only. The workflow gains `pull-requests: read` **only** when
   the input is on. Private-repo head refs (`refs/pull/N/head`) are fetched
   over the authenticated git transport — the checkout's persisted token
   (`http.https://github.com/.extraheader` config), never the API and never
   a PAT. Artifacts and reports carry the disclosure of clause 5 — the
   recorded marker of clause 5 means the tier has RUN, not merely that rows
   happened to exist. A passthrough input `pr-backfill: full` (the
   `workflow_dispatch` caller shape) backfills the unincorporated history
   in batches — the clause 2 resume machinery at 250 PRs per invocation,
   each dispatch landing a disjoint chunk past the shared watermark and the
   next continuing from there until history is caught up; the explicit
   full-history RE-SWEEP (already-incorporated history included) is the
   direct `pr-sweep --full` operator face.
2. **Watermark-driven per-increment runs.** The resume marker is the
   `(merged_at, number)` tuple of the last *incorporated* PR, recorded in
   the Artifact next to the evidence rows (the `pr_watermark` table:
   `last_merged_at` + `last_pr_number`, schema step 7) and surviving
   partial recompute and gather exactly
   as they do. Runs process merged PRs whose tuple is newer than the
   marker **oldest first by merge date** (ties broken by PR number
   ascending, deterministic — an equal date with a higher number counts as
   newer), and the marker advances **after each PR's evidence is
   recorded** (rows + marker in one per-PR transaction), never per page:
   every interruption leaves a contiguous processed prefix, and resume
   starts at the first PR past the marker — zero skipped PRs, redoing at
   worst the single interrupted PR (its rows are replaced, never
   extended). *Rationale (the 2026-10-30 user ruling, superseding ticket
   #36's letter):* the PR number stays the per-row key and the
   `--after-pr` bounded face's bound, but it cannot be the RESUME point —
   a still-open lower number sat below the number watermark forever,
   losing out-of-order merges. The ruling prefers completeness over the
   number's append-only convenience. Two consequences: the increment's
   listing window is complete (the page walk descends to the end of the
   list — no date-based early stop is sound, since an older-numbered PR
   can merge later), and a migrated number-key marker (a NULL merge date)
   claims no date, so every merged PR is re-examined. Closed-unmerged PRs
   and PRs merged into another base branch are never incorporated and
   never advance the marker. Stop conditions — rate
   limit exhaustion, the per-run processing cap, interruption — end a run
   cleanly with a visible notice, never a failure exit; the next run
   resumes automatically. The **full-history sweep** (explicit operator
   invocation) is the same merge-date walk merely continued past the
   number bound to the beginning of the numbering (or a stop condition),
   sharing the marker: later push runs continue after it.
3. **PR listing and head-ref retrieval.** One `GET
   /repos/{owner}/{repo}/pulls?state=all&per_page=100` per 100 PRs,
   keeping merged ones only — mergedness is the
   wire's `merged_at` timestamp-or-null (`merged: true` in prose is the
   intent, never the wire format) (closed-unmerged and still-open PRs are
   listed and skipped — an open PR that merges later is picked up then,
   its merge date newer than the marker's). Bodies, titles and
   authors come from the LIST response: ZERO per-PR API calls and no
   commit-list endpoints anywhere — a 10k-PR repo costs ~100 requests
   against the `pull-requests: read` budget. `refs/pull/N/head` is fetched
   over the git protocol (zero API budget), **lazily** — only for
   squash/rebase-merged PRs whose shas were rewritten on the Target branch.
   Plain-merge PRs are skipped entirely by the fetch: their commits are in
   the repo at identical shas, so evidence is recorded once from their
   commits, never twice. The fetch runs on the authenticated git transport
   (clause 1) so deleted squashed branches recover in CI without any PAT.
4. **Detection and mapping.** Head commits are detected with ticket #34's
   rule table (`aiattr.matches`) plus PR-level signals (`aiattr.pr_matches`:
   body footers, PR author bot identity, head branch name prefixes) — the
   same ranked table, never a second engine. Evidence maps onto the landed
   Target-branch sha: subject `(#N)` for squash, MERGE_HEAD shape for plain
   merges, patch-id matching for rebase rewrites. A **head-parentage check**
   runs before a fetched ref is trusted: the squash landing must carry the
   head range's aggregate patch-id and every content-carrying rebase head
   commit must map to a walked twin. When the ref is not plausibly the PR's
   merge head (mapping fails, post-merge force-push, or the ref is gone) the
   PR's rows are recorded SUSPICIOUS-MARKED with no commit attached — audit
   only, never attribution.
5. **Recorded evidence, channel precedence, and disclosure.** Rows are
   (PR number, rule id, raw matched string, `observed_at`, channel `pr`)
   plus the tier and the identity the raw string names, attached at the
   mapped sha. They are RECORDED, never re-derived by recompute (they are
   not pure functions of the Target branch); a re-run replaces that PR's
   rows wholesale rather than duplicating them. Channel precedence
   (consumption): git-only evidence on the same commit outranks inherited PR
   evidence — the storage encodes it ("recorded once, not twice": where the
   walk already recorded the identical match at the same commit the PR row
   is not stored again) and resolution is git-first, with suspicious rows
   excluded from every resolution. Disclosure is MODE state, recorded: every
   actual sweep face records a `pr_disclosure` marker row (schema step 8 —
   `repo_id` as its primary key, one row per repository, a row meaning the
   PR tier has RUN, no enabled boolean and no timestamps) before it lists
   any PRs and after config validation, so a run that finds zero
   non-suspicious rows — or dies operationally right after — still leaves
   disclosure on; a config error raises `SweepError` before the marker and
   records nothing. The marker is RECORDED state like the rows and the
   watermark: it rides the Artifact and survives partial recompute and
   gather unchanged, and is never removed. Artifacts minted before the
   marker existed (schema step < 8) complete it empty and fall back to
   their non-suspicious `pr_evidence` rows as the disclosure trigger.
   Reports render the disclosure line `GitHub-sourced (PR-channel) evidence
   present` whenever a marker exists or such legacy rows do; with neither,
   no disclosure. Gathered org metrics (#38) resolve one attribution per
   Shared commit by the stronger channel and render all attribution numbers
   as lower bounds — never normalized upward to PR counts, `no-evidence`
   never rendered as "human".
