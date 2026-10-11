# ADR 0003: In-repo committed artifact, integrity policy, and per-push landing

## Status: accepted

## Context & decision

The git-analysis half of skill-stats was single-repo and machine-local: one DB
per run, stored outside the analyzed repo (`settings.repo` = absolute path),
results never left the machine. The tool's goal moved to running on many repos
with results owned by each repo and combinable later for organization-wide
analysis. A committed, repo-carried artifact makes that possible: the artifact
travels with its repo, survives machine switches, and any clone can read it.

Decision, per grill rounds recorded in `docs/decisions-trail.tsv`:

1. **Artifact = per-repo analysis store, not a metrics snapshot.** Each Target
   repo commits a `.skill-stats/` directory holding the complete analysis state
   of that repo (verdicts, features, fix attribution, lineage, computed line
   columns) plus a regenerated standard HTML report. Org-wide stats are later
   built by gathering artifacts; nothing in this ADR depends on the gather
   design beyond the store being self-contained.
2. **Format: whole-snapshot SQL text** — one canonical `INSERT` per row,
   deterministic ordering, written fresh on every update. Every artifact
   version is complete: no history traversal or delta merging is ever needed to
   read the latest one. Raw per-commit diff text is NOT committed (it re-derives
   from the repo's own git history); instead the walk stores per-commit
   `added_lines`/`deleted_lines`/`churn_lines`. Machine-local settings
   (`repo` path, `classify_session_id`) and the `runs` table stay out; the
   artifact is a curated projection, not a byte copy of the working DB.
3. **Integrity: `Artifact digest` + `Partial recompute`.** The artifact
   carries a digest computed over its content with the digest value itself
   excluded (the digest is written last into the file; verification recomputes
   over the content with the digest field blanked). Deterministic ordering
   keeps the digest stable across machines. Honest limits of an unkeyed
   checksum: it detects accidental or manual edits, not adversarial forgery
   (an editor can also rewrite the digest; the anchor is that staying
   consistent costs as much as the recompute it is hiding). The artifact also
   carries a `covered_through` marker (last commit sha it is trusted through).
   Every update and future gather verifies first. On mismatch: walk the
   artifact file's git history, restore the newest prior version whose digest
   verifies, then re-walk and re-classify only commits newer than its coverage
   horizon, re-run detect/lineage/report, and write a fresh version. Full
   recompute (everything re-classified) is the fallback when the history
   itself was rewritten so no verifiable version remains.
4. **Exclusion: `.skill-stats/**` only.** Walk skips commits whose stripped
   diff is empty; mixed commits lose the artifact paths before
   classification/churn; blame parsing ignores store paths. The rule is a path
   pattern, not a commit list, so re-inits, fork history, and CI-created
   commits are all covered. No blanket hidden-directory rule: in many repos
   (the pilot among them — `.config/`, `.ssh/`) hidden directories are the
   actual product content. Submodules need no special handling: the parent
   repo's diffs and blame show gitlink lines only and never descend into
   submodule content; a submodule can later be analyzed as its own Target repo.
5. **Repo-scoped ids, prefix not remap.** Commit identity is also
   repository-scoped: migration step 1 keys commits by `(repo, sha)` (and
   `walk_index` unique per repo), with attribution tables' references carried
   along. Feature ids are repo-prefixed strings
   (`{repo-slug}-{local number}`, e.g. `dahep-dotfiles-57`) so a gathered
   union DB needs no id remapping — it is structurally identical to a
   single-repo DB, report code reads either unchanged. UUID-style opaque ids
   were rejected: the rolling classifier cites short feature ids in prompts
   and stays reliable with local numbers plus a prefix.
6. **Landing: a distinct artifact-update commit per push, parented on the
   triggering commit.** Amending the pushed commit (force-push to add data
   onto it) was rejected outright. Because artifact paths are excluded from
   stats, an artifact-only commit has nothing for the next run to analyze —
   the recursion ring closes by exclusion, so the workflow does not need a
   PAT, and GitHub's rule that `GITHUB_TOKEN`-pushed events don't re-trigger
   workflows (verified in GitHub's trigger docs) just saves the no-op run. A
   no-op discipline applies regardless: an update whose data is unchanged
   pushes nothing at all. Stale-run races resolve by abandonment, not
   retry: if a push lands while the previous run was still working, the
   newer push triggers its own run anyway, whose artifact is cumulative
   over the branch walk — the older run's landing is simply dropped (it
   would fail the fast-forward check). Such superseded runs end with a
   visible neutral status, never a red failure, and never force-push; they
   must not block the code pushes they trailed.
7. **CI shape:** `skill-stats update` (one orchestrating verb: verify → walk →
   classify → detect → lineage → live-lines (backfill + tip) → report →
   serialize + digest), with flags (`--no-classify` etc.) trimming stages for
   dev/debug; stage subcommands remain plumbing. The job runs on every push
   to the Target branch; "cadence configurable" means the trigger set is
   repo/config-chosen (every push, scheduled, or selected pushes) — trigger
   choice never forces a landing (no-op updates push nothing). The
   deterministic stages always run; classification is best-effort — when
   its configuration is missing (no opencode auth, rate limit) the walked
   backlog is marked visible and reprocessed by a later successful run, never
   silently skipped.

   Two tested refinements (2026-10-11, dogfooding on Dahep/skill-stats):

   - **Tool install is git-pinned, not version-pinned.** The rendered
     workflow installs the tool standalone
     (`uv tool install 'skill-stats @ git+https://github.com/Dahep/skill-stats.git@<ref>'`),
     where `<ref>` is the commit of the skill-stats checkout that rendered
     the file (installed copies default to `main`; `--tool-ref` moves the
     pin explicitly). A version pin was the original intent but the package
     is not on PyPI, and a `uv tool install skill-stats==0.1.0` line fails
     the run at the scaffold step. The git pin preserves the invariant
     "install the engine that rendered the rules" and keeps `--upgrade`
     meaningful (a re-render moves the commit pin).
   - **Pages publishing is a `ci init --pages` opt-in** (the gate on
     public/paid repos below is the user's choice of target; private
     non-paid repos cannot enable Pages at all). The opt-in adds a
     `deploy-pages` job (`pages: write`, `id-token: write`, scoped to that
     job only; `needs: skill-stats`, `concurrency: pages`), which downloads
     the run's `skill-stats-report` artifact, renames `report.html` to
     `index.html`, and deploys via `actions/deploy-pages` - a separate
     deployment, not a landing commit; history is never rewritten for Pages.
   - **SSH-alias hosts are machine-local names, never identity.** An origin
     spelled `git@github-personal:Dahep/dotfiles.git` names a port/key
     mapping in the machine's ~/.ssh/config, not a host: normalize_clone_url
     consults `ssh -G` (a pure config read) and, when the alias resolves to
     a different HostName, returns None so the key falls back to the
     documented local: shim — the same machine-local rule as local paths.
     Real-host spellings (git@github.com:..., IP literals, resolved-equal
     hosts) keep canonicalizing. Found in dogfooding: an alias-baked key
     inside a committed artifact blocked CI's seeded working DB on the
     identity refusal rule (run 38157341104) — genuine data under a
     machine-local name can never relabel cleanly, which is exactly the
     gate doing its job.

## Considered Options

- **Metrics-only snapshot in `.skill-stats/`**: small and simple, but org
  analysis can never re-ask per-commit questions; rejected to keep artifacts
  complete stores.
- **Binary SQLite as the committed artifact**: natively openable from any
  clone, measured ~83 KB compressed whole-file at pilot scale with ~2 KB
  per-update growth — but growth re-stores the full compressed blob whenever
  SQLite page churn breaks delta locality, diffs are unreadable, and a stable
  digest would need a digest table plus canonical row serialization. Rejected.
- **JSON export**: same stable-small growth as SQL text, but rebuilding a DB
  needs a custom loader; SQL text rebuilds with `sqlite3 db < file` and diffs
  line by line. Rejected as the runner-up.
- **Central org store instead of in-repo artifacts**: rejected — it moves the
  source of truth off the repo and makes freshness network/infra dependent.
- **Full recompute on tamper**: maximum integrity, but re-classification is
  LLM-based and non-repeating (verdicts/ids drift), and tamper is a rare event;
  partial recompute anchored at the last trusted version is the chosen
  middle, with full recompute as fallback.

## Consequences

- Every clone carries the repo's analysis; two clones of the same repo
  must agree on data (same shas) — the artifact's `Repository key`
  (canonical clone URL + Target branch) makes identity machine-independent.
  URL normalization (ssh and https spellings of one repo canonicalize to one
  key) is implementation detail, recorded in DESIGN.md at build time.
- If the same repository URL is ever analyzed on two Target branches (two
  checkouts), feature prefixes derived from the URL slug collide; recorded
  rule (user): gather fails with a clear error — analyzing one repo twice
  is not supported, not auto-renamed.
- Repos pay a small store tax per history growth; the SQL text format keeps
  per-update diffs proportional to changed rows only.
- The store must stay deterministic: any wall-clock or accidental
  nondeterminism in serialization becomes false tamper signals.
- Publishing (Pages, org landing) is gated on the repo being public or paid
  plan; report.html is committed and CI-uploaded as an Actions artifact
  regardless.
- Submodule-safe by diff mechanics: gitlink lines in parent diffs count as
  diff lines toward churn; blame never descends into submodule content. A
  submodule can later be analyzed as its own Target repo.
