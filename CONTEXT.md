# coding-stats

A tool that mines git history (and later opencode history) to measure feature-level metrics like features/year, fixes/feature, and later cost and skill usage per feature.

## Language

### Git domain

**Repository key**:
A Target repo's identity across machines and clones: clone URL plus the repository's Target branch. Machine-local paths are never identity; SSH config alias hosts are machine-local names and never identity either. Gather keys on a Repository key, two clones of the same repository on the same Target branch are one; one repository on two Target branches is two keys.

**Feature id**:
A Feature's identifier, globally unique: it names its repository and the Target branch that first classified it. A Feature reached by several Target branches through Shared commits keeps one id.

**Enhancement**:
A Commit verdict for a commit that extends an existing Feature, linked to that Feature's root Feature — never to another Enhancement. Not counted in features/year; fixes to an Enhancement's lines count against the root Feature.
_Avoid_: improvement, feature (the old catch-all verdict)

**Fix**:
A Commit verdict for a commit that repairs (subtype `repair`) or reverts (subtype `revert`) lines belonging to existing commits of a Feature or another Fix, without adding a Feature.
_Avoid_: bugfix, patch

**Fix lineage**:
The relation formed by fixes that target other commits. A Fix is attributed to every Feature its chain reaches: targets resolve through any Feature link (defining, enhancement or fix commits), blame skips Refactor and Cleanup commits (a link through one is resolved by inheritance), and a fix may reach several Features. Metrics count each Fix once per Feature it reaches.

**Embedded fix**:
Repair work inside a Feature or Enhancement commit that rewrites at least a threshold number of another Feature's lines: it counts as a Fix of that Feature.
_Avoid_: hidden fix, drive-by fix

**Area**:
One of `product`, `test`, `build` or `docs` — the classification of each changed file, from ordered path rules: repo rules first, then built-in defaults, with unmatched paths counted as `product`. The Commit verdict states intent and never location: any repair is a Fix.
_Avoid_: component, zone

**Upkeep fix**:
A Fix hit in a `test`, `build` or `docs` file, recorded per Area whichever commit wrote the lines. Reported as its own rates — never in fixes/feature and never as uncovered.
_Avoid_: maintenance fix

**Fix event**:
A `fix`-verdict commit (either subtype) or an Embedded fix, counted once per Feature it reaches and dated by its Landing date. The unit of fixes/feature.
_Avoid_: fix count

**Landing date**:
When a commit landed on the Target branch (the committer date). All time bucketing — fixes/feature windows, features/year — uses it, never the author date a rebase preserves.
_Avoid_: merge date (a PR-tier term), author date

**Feature attribution**:
The mapping of a commit onto a Feature: directly (the commit defines the feature) or via blame (the commit touches the feature's lines).

**Commit verdict**:
The single class a commit is classified into: `feature`, `enhancement`, `fix` (subtype `repair` or `revert`), `refactor`, `cleanup` or `chore` (subtype `docs`, `build`, `test`, `deps` or `other`), plus the system verdicts `merge` and `unknown`. Blame evidence is a hint supplied to the classifier, never the verdict; every verdict records the source it was obtained from.
_Avoid_: commit type, category

**AI attribution**:
The per-commit attribution state of a Target-branch commit, computed from git data alone: `yes` (a signature rule matched), `possible` (weak signal only), or `no-evidence` (nothing matched — absence of a signal, never a claim about authorship). Matched on the raw identity strings, before any mailmap resolution.
_Avoid_: human commit, AI commit

**Attribution evidence**:
One auditable match behind an AI attribution: rule id plus the raw matched string (and its tier), stored per commit and carried in the Artifact. The git-channel rows are pure functions of the Target branch: re-derived deterministically, never hand-edited.
_Avoid_: signal, hint

**PR-channel evidence**:
Attribution evidence recorded from merged PRs (PR body footers, PR author identity, head branch names, and head-commit messages read via `refs/pull/N/head`) and attached at the landed Target-branch sha. Recorded once per (PR, commit, rule), never re-derived; channel `pr` — inherited, and outranked by git-only evidence on the same commit. Evidence whose head ref failed its parentage check is recorded as suspicious evidence: audit only, never attribution.
_Avoid_: PR signal

**PR-channel disclosure marker**:
The recorded marker — one row per repository — that the PR tier has run against it: recorded before a sweep lists any PRs, never removed. Reports disclose the PR channel whenever the marker exists, even when the run recorded zero non-suspicious evidence rows; Artifacts from before the marker existed disclose on their non-suspicious PR-channel evidence instead.
_Avoid_: enabled flag, disclosure mode

**Refactor**:
A Commit verdict for a change that restructures existing Feature lines without defect repair intent.
_Avoid_: clean-up, rewrite

**Chore**:
A Commit verdict for upkeep that changes no product behaviour: docs, build/CI, tests, dependencies (subtypes `docs`, `build`, `test`, `deps`, `other`). It counts as neither a Feature nor a Fix, is never blamed as a fixing commit, and its lines are unowned; a commit mixing upkeep with functional change takes the functional verdict.
_Avoid_: maintenance, housekeeping

**Revert**:
A Commit verdict for a change that undoes an earlier commit — the `revert` subtype of Fix. Its target comes from the `This reverts commit <sha>` message line when present, falling back to blame.
_Avoid_: rollback, undo

**Re-land**:
A revert of a Revert. It takes on the original commit's verdict and Feature links and is never a Fix.
_Avoid_: reland, reapply

**Cleanup**:
A Commit verdict for formatting/whitespace/non-semantic changes.
_Avoid_: style change, lint commit

**Unowned lines**:
Lines at the tip owned by walked commits that belong to no Feature, broken down by Commit verdict. Distinct from unattributed lines, which are owned by commits outside the walk.
_Avoid_: orphan lines

**Provenance**:
How a decision was obtained, recorded on the decision itself: a verdict's source in its ladder, a fix-to-Feature link's resolution (direct, adjacency, inherited, revert-message) with the Area of each hit. The audit reads nothing else.
_Avoid_: metadata, source info

**Edge case**:
A non-standard decision the pipeline records for audit: commits `unknown` after every ladder step, heuristic or fallback-model verdicts, fixes reaching no Feature, non-standard fix resolutions, Reverts, unowned tip lines, near-threshold Embedded fixes. Listed by the audit verb with counts and per-case detail.
_Avoid_: anomaly, outlier

### opencode domain

**Skill invocation**:
One explicit `skill` tool call recorded in opencode history, whether user-invoked or automatically triggered. The atomic unit of all skill metrics.
_Avoid_: skill use, skill call

**Skill version**:
The content hash of a skill's directory, ideally captured at load time by a plugin. The identity skills carry in metrics like performance-per-version.

**Skill cost**:
The accumulated session cost from the message that loaded the Skill (invoking forward) until the skill's end — currently approximated as end of session — including costs incurred by subagents the skill spawned.

**Steer**:
A user message after a Skill invocation that redirects the agent, as opposed to confirming/approving or continuing. Classified by an LLM from the skill's context plus surrounding messages.

**Historical invocation**:
A Skill invocation from sessions predating the plugin's existence. Most skill attributes cannot be measured for these; Skill-invocation stats cover new Invocations only.

**Target repo**:
The git working copy a Run analyzes. One checkout pairs one Repository key.

**Artifact**:
The `.coding-stats/` store holding one Target branch's complete git-analysis state (verdicts, features, fix attribution, lineage), committed on that branch's Data branch. Source of truth for that branch's numbers; Org-wide metrics are gathered from many Artifacts.
_Avoid_: export, dump

**Artifact digest**:
A value stored inside the Artifact covering its whole content except the digest itself. Verification anchor for Updates and gathers: neither builds on an Artifact whose digest fails to verify.

**Coverage horizon**:
The commit up to which an Artifact version's data is trusted. Stored inside the Artifact; Partial recompute resumes after it.
_Avoid_: watermark, boundary

**Sample**:
One commit at which the live-lines history is recorded for every feature alive at that point. The curve is Samples joined; the latest Sample pins the per-feature summaries.
_Avoid_: checkpoint, snapshot

**Partial recompute**:
What an Artifact digest failure triggers: trust is rebuilt from the newest prior artifact version that still verifies, and work newer than it is re-analyzed. Falls back to a full recompute when no verifiable version survives.

**Target branch**:
The single branch a git analysis run walks (main or an explicitly configured alternative). All metrics are computed on Target-branch commits only; a repo may track several Target branches independently.
_Avoid_: all branches, full history

**Data branch**:
The orphan branch `coding-stats/<Target branch>` that carries one Target branch's Artifact.
_Avoid_: stats branch, gh-pages

**Seeding**:
Reusing the analysis held by another Data branch of the same repository for Shared commits, so shared history is classified once.
_Avoid_: seed (for restoring a branch from its own Artifact — that is Partial recompute or a plain rebuild)

**Shared commit**:
A commit walked by more than one tracked Target branch of the same repository. Counted once in gathered metrics.
