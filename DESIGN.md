# coding-stats — design (post-grill)

Tool that mines git history (+ later opencode history) to measure feature-level
metrics: features/year, fixes/feature (overall, per-feature), code churn per
feature; later cost/feature/model, cost/skill, fixes/feature/skill,
fixes/feature/model, skill quality over time.

Pilot repo: ~/dotfiles (183 commits since 2025-09 main; 290 opencode sessions —
richest pairing). Store: imported SQLite copy. Explore via python (uv) +
matplotlib scripts over SQL queries. Build git-only phase first.

## Git pipeline

- **Walk scope**: `main` only (or the Target branch). Squash-merged branch
  commits appear once, as the main-branch squash commit. Patch-equivalent
  commits on main are reconciled via patch-id.
- **Commit verdict**: each commit is classified once, by the LLM rolling walk:
  `feature | fix | refactor | revert | cleanup` (+ `merge` kind for merges).
  Mechanical blame detection of line-deletion is the *evidence*, not the
  verdict; formatting-only changes classify as `cleanup`, reverts as `revert`
  (never as fix). Feature metrics (features/year, fixes/feature) count only
  `feature`-verdict commits in the numerator of features/year; fixes are
  `fix`-verdict commits.
- **Feature**: coherent unit of change; LLM-classified. Rolling job via opencode
  CLI: walk commits chronologically on main, ask "new feature or existing one?",
  checkpoint/resumable; only new commits classified. Most dotfiles features = 1
  commit; schema is many-to-many anyway (a commit may define multiple features;
  a commit may both define a feature and be a fix's target).
- **Fix**: a `fix`-verdict commit that modifies lines belonging to existing
  commits (of a feature or another fix). Detection: deletion/rewrite lines vs
  parent (merge commits use first parent on the Target branch), blamed
  (`blame -w -C`) at the parent to a commit that defines a feature or a fix.
- **Targets**: a fix may target several commits. Tables:
  `fix_touches(fix_commit_id, source_commit_id)` raw blame hits (commit
  granularity, no line ranges), and
  `fix_targets(fix_commit_id, target_commit_id)` many-to-many.
- **Lineage**: a fix that targets a fix is attributed transitively: to each fix
  it targets and up-chain to the original feature(s).
  `fixes_features` precomputed transitive closure so queries avoid recursive
  CTEs. Metrics count a fix once per feature it reaches.
- **Identity**: feature id assigned once at creation (keyed to defining
  commits); canonical title locked, later titles kept as history. Incremental
  runs only classify new commits. Raw LLM verdicts stored per commit for
  auditability.
- **User metrics**: dropped for now (single human, 5 author-strings). Author
- normalization is out of scope.

## AI attribution (ADR-0007 baseline tier, ticket #34)

- **Detector**: git-only, per walked commit, on the RAW identity strings
  (before mailmap resolution). Ranked rule table from #34: (1) Co-authored-by
  trailer, email-keyed known agent identity; (2) author/committer email —
  known agent domain or GitHub bot noreply (numeric-prefix aware); (3) name
  suffixes ((aider), [bot], Cursor Agent); (4) body signatures; (5) tool
  trailers (Assisted-by / Generated-by / Agent-Logs-Url / Claude-Session);
  (6) merge-subject branch prefixes (claude/, codex/) — `possible` tier at
  most. Repo-level config artifacts (CLAUDE.md …) are repo-level facts and
  stay out of the per-commit walk (no working-tree checks inside it).
  GitHub squash framing matches the raw-trailer rules: trailer lines inside
  the squash body block and the trailing summary Co-authored-by are commit
  object text.
- **Output**: per-commit verdict `yes` / `possible` / `no-evidence` (never
  "human": absence of a signal is not a statement about authorship) plus
  evidence rows (rule id + raw matched string) for audit. Harness is
  normalized from the matched identity; the model is parsed only where the
  harness names one (Claude, Aider; `Claude Opus 4.8 (1M context)` → `Opus
  4.8 (1M context)`).
- **Storage**: `ai_attribution` + `ai_evidence` ride the Artifact like all
  recorded state (serializer + verify chain); they are pure functions of the
  Target branch, so the walk re-derives any missing rows (restored
  pre-feature artifacts carry none) deterministically.

### PR-channel tier (ADR-0007 clauses 3-5, ticket #35)

- **Sweep**: `pr-sweep` (watermark-driven increment; `--after-pr <n>` is
  the bounded adoption face, `--full` the operator's full-history sweep)
  reconstructs PR-side attribution. Cost discipline: one list
  GET per 100 closed PRs (`GET /repos/{owner}/{repo}/pulls`), bodies/titles/
  authors straight from the list response — ZERO per-PR API calls and no
  commit-list endpoints; `refs/pull/N/head` head refs come over the git
  protocol (zero API budget), fetched lazily only for squash/rebase PRs.
  Closed-unmerged PRs are listed and skipped; PRs merged into another base
  branch never belong to this Target. Processing is oldest-first by merge
  date (ties by PR number); the resume marker is the last incorporated PR's
  merge date (`pr_watermark.last_merged_at` + `last_pr_number`, schema step
  7) — keyed by merge date, not PR number, so out-of-order merges are never
  lost (2026-10-30 ruling; the PR number stays the per-row key and the
  `--after-pr` bound). No LLM calls anywhere — the PR tier
  is detection-only.
- **Merge strategies** (identified from the walk, no API): squash → the
  `(#N)` subject landing; plain merge → the MERGE_HEAD-shaped `Merge pull
  request #N` commit, never fetched — its commits are already identical in
  the repo, so evidence is recorded once from them (merge^1..merge^2,
  locally), never twice; rebase → patch-id twins of the fetched head
  commits, each head commit mapped onto its landed copy, PR-level rows
  anchored at the head tip's twin.
- **Detection**: head commits run through #34's rule table unchanged; the
  PR-level signals (PR body footers, PR author bot identity, head branch
  name prefixes) go through the same table (`aiattr.pr_matches`) — never a
  second rule engine. Rows carry the rule id, raw matched string, tier, the
  identity the raw names, `observed_at`, channel `pr`, and the PR number.
- **Head-parentage check**: a fetched ref is trusted only when it plausibly
  is the PR's merge head — the squash landing carries the head range's
  aggregate patch-id, and every content-carrying rebase head commit maps to
  a walked twin. A force-pushed, vanished or unmappable head records
  SUSPICIOUS rows (no commit attached) — audit records, never attribution.
- **Storage**: `pr_evidence` (schema step 5) rides the Artifact like every
  recorded state and survives gather/org projection with commit-id remap.
  Rows are RECORDED, never re-derived by recompute (they are not pure
  functions of the Target branch). Replace-not-duplicate: a re-run replaces
  that PR's rows wholesale, keyed by (repo_id, pr_number). `repo_id` scopes
  suspicious rows (which carry no commit) in multi-repo unions.
- **Channel precedence**: git-only evidence on the same commit outranks
  inherited PR evidence. Stored: where the walk already recorded the
  identical match at the same commit, the PR row is not stored — recorded
  once, not twice. Consumed (`aiattr.effective_evidence` /
  `effective_attribution` — the resolvers live in the aiattr leaf, which
  also owns the channel constants, the disclosure line and the one verdict
  ladder; prsweep re-exports them, and metrics resolves through aiattr
  only): the verdict is the strongest tier of the contributing rows; the
  identity claims (harness/model) resolve git-first, PR rows fill only what
  the git evidence leaves silent; suspicious rows never contribute. The
  `channel` column is the provenance the gather/report layer (ticket #38)
  acts on.

## Artifact & multi-repo (settled grill 2, ADR-0003)

- **Artifact**: each Target repo commits `.coding-stats/` — the whole
  analysis state as a deterministic SQL text snapshot (one INSERT per row,
  whole DB every version) + digest (sha256 over the file) + `covered_through`
  marker + regenerated standard HTML report. Raw diff text, `runs`, and
  machine-local settings stay out of the artifact; the walk instead stores
  per-commit `added_lines`/`deleted_lines`/`churn_lines`.
- **Exclusion**: `.coding-stats/**` paths only (no blanket hidden-dir rule).
  Walk skips commits whose stripped diff is empty; mixed commits get
  artifact paths stripped; blame parsing ignores store paths. Submodules
  need no handling today: parent diffs show gitlink lines (they count
  toward churn as one-line diff entries) and blame never descends into
  submodule content; a submodule can later be analyzed as its own Target
  repo.
- **Identity**: `Repository key` = clone URL + Target branch — machine-local
  paths are never identity. Feature ids become repo-prefixed strings
  (`{repo-slug}-{fid}`), so gathered unions need no id remapping.
- **Feature size**: per-feature `live_lines` = blame at the Target-branch
  tip, every line owned by exactly one commit; fix-owned lines accrue to
  the features the fix targets (via fix lineage); a commit claimed by two
  features counts its lines in both. History: full-sweep backfill of the
  curve at adoption, sampled past a size cap.
- **Update verb**: `coding-stats update` = verify digest → walk → classify →
  detect → lineage → live-lines (backfill + tip) → report → serialize +
  digest, with flags (`--no-classify` etc.) trimming stages for dev/debug.
  Stage subcommands remain plumbing. Lineage runs before live-lines: fix
  lines accrue to features through the closure, which must exist first.
- **Feature size**: per-feature `live_lines` = blame at the Target-branch
  tip, every line owned by exactly one commit; fix-owned lines accrue to
  the features the fix targets (via fix lineage); a commit claimed by two
  features counts its lines in both. Samples store
  (feature_id, at_commit_sha, live_lines) rows — forward samples every
  update, backfill generates the same row family at adoption (full sweep,
  sampled past a size cap); historical samples attribute fix lines with the
  closure data as-of that sample.
- **Identity**: `Repository key` = clone URL + Target branch — machine-local
  paths are never identity. Feature ids become repo-prefixed strings
  (`{repo-slug}-{fid}`), and commits are keyed by `(repo, sha)` (migration
  step 1 carries the attribution tables along), so gathered unions need no
  id remapping. Analyzing one repo on two Target branches is unsupported —
  gather fails with a clear error rather than renaming colliding ids.
- **Integrity policy**: verify on every update/gather; tamper → partial
  recompute anchored at the newest digest-verifying version in the
  artifact's own git history (reclassify commits after its coverage
  horizon); full recompute only when history was rewritten so no trusted
  version survives.
- **CI**: runs update on every push to the Target branch; the trigger set is
  configurable (every push, scheduled, or selected pushes) and never forces
  a landing — no-op updates push nothing. Stale runs: a newer push triggers
  its own run, whose artifact is cumulative, so a lagging run's landing is
  dropped with a visible neutral status (excluding by fast-forward check);
  superseded runs never block the pushes they trail and never rewrite
  history. Deterministic stages always run; classification best-effort with
  the backlog marked and reprocessed by a later run; artifact lands as a
  distinct commit parented on the push, pushed with `GITHUB_TOKEN` (ring
  closed by exclusion); report also uploaded as an Actions artifact; Pages
  publishing gated on public/paid repos.
- **Gather** (built: `src/coding_stats/gather.py`): ingests N artifacts into a
  union DB keyed by Repository key; verify-first ingest, idempotent per key
  (identical artifact = no-op, a newer one last-wins), slug collisions refuse
  (Q18.2). Union DB is structurally identical to any single-repo DB, so the
  report code path reads either unchanged (REPO columns appear only when it
  holds >1 key); commit ids are union-local, feature ids need no remapping.

## opencode pipeline (schema settled now, built later)

- New threads only: analysis covers sessions created after the plugin exists;
  every session predating the plugin within the db span (which begins
  2026-03-24) is historical and excluded from skill metrics.
- Plugin (future work): V2 plugin API only — V1 plugin implementations do
  not run in V2 (`Plugin.define` + `setup(ctx)`, hook via
  `ctx.tool.hook("execute.before"/"after")` or `ctx.event.subscribe()`).
  Log at skill load — skill dir content hash, session and message ids,
  source (user-invoked vs auto), context size. This is the authoritative
  capture; without it, skill metrics by build only explicit `skill` tool
  calls. Verify at build: `ctx.skill` may expose skill identity/paths
  (possibly replacing manual dir hashing), and how to observe
  user-invoked vs auto source — in V2 recorded data the skill part carries
  only `{name}`, so source detection needs the prompt/session hook and its
  mechanism is not yet proven; fall back to context-size heuristics if
  source detection fails.
- Built-in skills (e.g. customize-opencode) out of scope — we don't own or
  improve them. Skills with no local dir (5 of 47 names seen) are 'external/
  unknown-version' and excluded from version-tracking metrics.
- Import (normalized copy) from the db resolved via `opencode debug paths db`
  (path respects release channel and `OPENCODE_DB`; never hardcode). Keep
  source ids. opencode ≥ V2 (v2.0.x, event-sourced): `session_v2` is the
  authoritative session table (strict superset of legacy `session`); message
  and part rows store JSON blobs — assistant cost/tokens/modelID/providerID
  live in `message.data`, so the importer extracts via `json_extract` rather
  than flat columns. The V1-era 97.6% message population figure must be
  re-measured against the V2 blobs before the importer is built.
- **Skill invocation**: explicit `skill` tool call. Unit of skill metrics.
  In V2 parts, the skill name is `state.input.name` (not `skill`); the blob
  shape may change again — treat `part.data` as data to json_extract, never
  string-match. Recorded subagent tool calls are still named `task`
  (460 parts present); parent_id session linking holds in `session_v2`.
- **Skill cost**: cost from the message that invokes the skill until end of
  session (approximation), including costs from subagents the skill spawned
  (task tool → subagent sessions, with parent_id linking; errored subagents
  flagged, their partial cost excluded).

## Adjudicated answers during grill

- fixes/feature/specific-fix: fixes counted once each, attributed to every unit
  in lineage: target_size matches fixes/feature of fix-F when requested.
- Cost: per-message rollup; session totals convenience only.
- ADR-0001: LLM feature/classification via opencode CLI.
- ADR-0002: skill metrics require plugin-logged data; no backfill attempt.
- Steers: classify steers per skill directly by the LLM (not by interval rule).
