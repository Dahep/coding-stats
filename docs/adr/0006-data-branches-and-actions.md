# ADR 0006: Data branches, multi-branch tracking, and GitHub Action distribution

## Status: accepted — supersedes these parts of ADR-0003: clause 1's committed `report.html`; clause 3's "walk the artifact file's git history" (now the Data branch's history); clause 5's id shape; clause 6's landing location and staleness test (its no-op, never-force and neutral-abandon rules stand); clause 7's git-pinned install and Pages refinements; and the "report.html is committed" and "analyzing one repo on two branches is not supported" consequences. ADR-0003 clause 4 (the `.coding-stats/**` path exclusion) stands — legacy history and migration still need it.

## Context & decision

ADR-0003 landed each Artifact update as a bot commit on the Target branch
itself. That pollutes the analyzed history and fails on work repos whose
rulesets require PRs into `main` (`GITHUB_TOKEN` cannot push there). Repos
also need more than one tracked branch (e.g. `main` and `release/2.x`)
analyzed independently, and installation via a rendered workflow
(`ci init --upgrade`) is heavier than a versioned action.

1. **One Data branch per Target branch.** The Artifact lives on an orphan
   branch `coding-stats/<target-branch>` holding only `.coding-stats/`; the
   Target branch never receives bot commits. Per-target branches (rather than
   one shared data branch with a directory per target) keep stale-run
   abandonment scoped: a run only ever competes with runs of the *same*
   target. The orphan Data branch shares no ancestry with its Target, so
   staleness is judged on the Data branch alone: the push is fast-forward
   only, and a run whose Data tip moved since it was read abandons (neutral
   notice, never force). A run whose Target moved on still lands — its
   Artifact is cumulative and correct through the commit it analyzed, and the
   next run continues from it, so paid classification is never discarded.
   The identity branch (Repository key) stays the Target branch; the store is
   a separate worktree of the Data branch. A Target branch under
   `coding-stats/`, or one whose Data branch name would clash with another's
   as a ref path (`release` vs `release/2.x`), is skipped with a neutral
   notice and a clear message. Partial recompute searches the Data branch's
   history for the newest verifying version. A ruleset covering every branch
   must exclude `coding-stats/**` or let the Actions bot bypass — a one-time,
   per-repo admin step.
2. **Feature-id prefix carries the branch of origin.** Ids become
   `{repo-slug}@{branch-slug}-{n}` on every branch, the default branch
   included (one id shape; existing artifacts are re-prefixed). `@` is
   outside the slug alphabet, so the repo/branch split is unambiguous; two
   branch names that normalize to one slug (`release/2.x`, `release-2.x`) are
   a hard error. The branch is the one that *first classified* the feature,
   not the artifact's own branch.
3. **Shared history is classified once (Seeding).** When an update runs on a
   Target branch, walked commits that another Data branch of the same repo
   has already classified are seeded from that artifact instead of sent to
   the LLM. Only sibling artifacts whose digest verifies are sources, and only
   shas in this Target's own walk are seeded. Seeded verdicts keep their
   original `classified_at` and model; seeded features keep their ids.
   Features a seeded commit is attributed to come along even when their
   defining commit is outside this walk. Detect, lineage and live-lines still
   run on the seeded branch, so its fix attribution and line counts are its
   own. LLM cost is paid once and shared
   commits agree across branches.
4. **Gather dedups by sha; earliest classification wins.** Where artifacts of
   one repo still disagree about a Shared commit (tracked before seeding
   existed, or concurrent first runs), the verdict with the earliest
   `classified_at` wins, ties broken by the lexicographic order of the
   Repository key strings. The loser's dependents are re-attributed through
   the winner: rows that pointed at a feature the losing verdict defined are
   re-pointed to whatever the winner attributes that sha to, and dropped when
   the winner attributes it to nothing. Feature rows stay per Repository key
   (each branch keeps its own live lines); a feature id present under several
   keys of a repo is one feature for org-wide counts. Ids stay globally
   unique, so the union still needs no remapping for *distinct* features. An
   id whose defining sha differs between artifacts (ids drifted after a full
   recompute) is a gather error, never a silent merge. Each commit counts
   once in org-wide metrics; per-branch reports stay per-branch. "Earliest
   classification" is a reading of the user's "earliest commit wins"; if it
   should mean "the artifact whose coverage first reached the sha", only this
   tie rule and Seeding's source choice change.
5. **Distribution: two composite actions in this public repo.**
   - `Dahep/coding-stats@v1` (update): verify → walk → seed → classify →
     … → serialize + digest → land on the Data branch. It installs the tool
     from `$GITHUB_ACTION_PATH`, so the engine version is the action ref the
     caller pinned — replacing the git-pin rendering of `ci init`. Target
     branch defaults to `github.ref_name`, overridable via a `target-branch`
     input; tracked branches are the caller's `on.push.branches` filter, with
     a per-branch, non-cancelling `concurrency` group. Inputs reach shell
     steps only through `env:`, never `${{ }}` inside `run:`.
   - `Dahep/coding-stats/report@v1` (report; its action path is the
     `report/` subdirectory, so it installs from the repo root above it):
     reads every `coding-stats/*` branch, gathers and dedups them, renders one HTML report (and optionally
     the union DB) to an output path, and uploads it as an Actions artifact.
     Pages deployment is an opt-in input — repos may want the data without a
     report, or use Pages for something else, or not have Pages at all. The
     caller triggers it by `workflow_run`, schedule, or manual dispatch: a
     `push` trigger on the Data branches never fires, because
     `GITHUB_TOKEN`-made pushes do not start workflows. Pages permissions
     belong to the caller's report job, never the update job.
   Data branches hold only the SQL artifact; rendering is the report action's
   job alone. `ci init` shrinks to writing the short caller workflow that uses
   the actions.
6. **Migration from the ADR-0003 layout.** A migration step verifies the
   artifact committed on the Target branch (refusing one that fails), re-
   prefixes its ids, and pushes it as the first commit of
   `coding-stats/<branch>`, so classified history is kept, not re-paid.
   Removing `.coding-stats/` from the Target branch is left to a normal PR;
   migration never pushes to the Target branch and is idempotent. An update
   that finds a legacy `.coding-stats/` on its Target branch but no Data
   branch warns to migrate first instead of silently re-classifying.

## Considered Options

- **Commit on the Target branch** (ADR-0003): rejected — history noise and
  incompatible with PR-only rulesets.
- **One shared data branch, a directory per target**: rejected — runs for
  different targets would supersede each other's landings.
- **git notes**: fit per-commit data but not repo-level state (features,
  lineage, samples); invisible on GitHub and not fetched by default.
- **Separate stats repo / central DB**: deferred, not rejected — likely needed
  once machine-local opencode data (agents, skills, cost) joins in, since CI
  never sees it.
- **Primary-branch-wins dedup**: rejected in favour of earliest
  classification, which needs no per-repo config.
- **Whole-artifact dedup** (one branch's rows win for all shared history):
  rejected for per-sha dedup with re-attribution.
- **Bare prefix on the default branch**: rejected — two id shapes, and a
  default-branch change would make ids ambiguous.
- **`.` as the repo/branch separator**: rejected — `.` is in the slug
  alphabet, so prefixes could parse two ways.
- **Report inside the update action**: rejected — couples the store to Pages.

## Consequences

- Orgs restricting third-party actions must allowlist `Dahep/coding-stats@*`;
  the action needs a tag/release process (`v1.x.y` plus a moving `v1`). The
  update action runs with `contents: write` and LLM keys, so work repos
  should pin a commit sha rather than the moving tag.
- Seeding makes a branch's artifact depend on another Data branch being
  fetchable at update time; when it is missing or fails to verify, the branch
  classifies on its own and gather's dedup rule reconciles later.
- `gather` no longer refuses two Target branches of one repo; the slug
  collision check moves to the `{repo}@{branch}` slug.
- Reports and union DBs carry commit subjects, author names and LLM output;
  on private repos they default to Actions artifacts, and Pages stays a
  per-repo opt-in.
