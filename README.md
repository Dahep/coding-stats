# skill-stats

Mine git history to measure feature-level metrics: **features/year**,
**fixes/feature** (overall and per feature), and **code churn per feature**.
See `DESIGN.md` (decisions) and `CONTEXT.md` (domain language).

## Git pipeline (built)

1. `walk` — first-parent chronological walk of the Target branch (`main`).
   Squash-merged work appears once as its main-branch commit; `git patch-id`
   is stored per commit so patch-equivalent commits can be reconciled.
2. `classify` — one verdict per commit from an interchangeable backend:
   the ADR-0001 rolling LLM session via the opencode CLI (one commit per
   turn, one JSON reply per turn: `new | existing | fix | refactor |
   revert | cleanup`), or ADR-0004's System One decision call (one POST per
   commit: `jev` choice over the same six verdicts + citation of the
   Feature it continues). Checkpointed into SQLite per turn so runs are
   resumable. Raw backend output is stored per commit for auditability;
   the single verdict drives what metrics count (blame evidence never
   does). Backend selection: `init --backend` forces it, otherwise the
   model id decides (`jev*` = System One). Routes/keyring: Zen
   (`OPENCODE_API_KEY`), OpenRouter (`OPENROUTER_API_KEY`), TypeSafe direct
   (`TYPESAFE_API_KEY`) — exactly one, or `SKILL_STATS_SYSTEMONE_PROVIDER`
   must name the vendor.
3. `detect` — for `fix`-verdict commits only: blame deleted/rewritten lines
   at the first parent (`git blame -w -C --first-parent --porcelain`) and
   record `fix_touches` (raw hits) and `fix_targets` (hits ≥
   `min_target_lines`).
4. `lineage` — `fixes_features` transitive closure: a fix of a fix is
   attributed to every unit in its chain, each fix counted once per feature
   it reaches (`via_fix_commit_id` marks the first intermediate fix).
5. `report` — features/year, fixes/feature, churn per feature; text, JSON
   (`--json`), matplotlib charts (`--charts`), and a self-contained HTML page
   (`--html [path]`: fix ranking, features-over-time, 1-month rolling
   fixes/feature, verdict mix, commit volume, churn).
6. `update` — one orchestrating run: verify the artifact digest first (on
   mismatch, partial recompute anchored at the newest self-verifying
   committed artifact version, full recompute when none verifies), then
   `walk → classify → detect → lineage → live-lines → report → serialize` +
   digest + verify. Default `--db skill-stats.db` on a machine; CI runs pass
   `--db .skill-stats/working.db` — the CI working DB is throwaway (runs are
   stateless and the committed artifact is the store), so a fresh DB adopts
   the checkout's git repo and seeds itself from the artifact. Classification
   is best-effort: a backlog stays visible in the final line and reprocesses
   on a later run.
7. `ci` — `skill-stats ci init` writes `.github/workflows/skill-stats.yml`
   (plus `.skill-stats/.gitignore` for the throwaway DB) idempotently,
   refusing drifted files unless `--upgrade`; `ci init --pages` adds a
   deploy-pages job that publishes the run's report.html as the site root
   (opt-in: Pages only works on public or paid repos); `skill-stats ci land`
   (called by the workflow, usable locally) lands the artifact-update
   commit: no-op updates push nothing, superseded runs abandon neutrally,
   everything else commits as `skill-stats[bot]` and pushes with
   `GITHUB_TOKEN`.
8. `gather` — `skill-stats gather --db union.db <repo|artifact>...` ingests
   N artifacts into a union DB keyed by Repository key. Ingest verifies each
   artifact first, is idempotent per key (identical artifact = no-op, a newer
   one last-wins), and refuses when distinct keys share a URL-derived slug
   (one repo on two Target branches — never renames ids). The union is
   structurally single-repo: `report --db union.db` reads it unchanged, with
   REPO columns appearing only when it covers more than one repository.

## Live report (dogfooding)

The tool analyzes this repository on every push to `main`; the report publishes
to <https://dahep.github.io/skill-stats/> and the analysis artifact
(`.skill-stats/`) commits back to `main` as `skill-stats[bot]`.

## Usage

```sh
uv sync
uv run skill-stats init ~/dotfiles --db ~/.skill-stats/skill-stats.db
uv run skill-stats walk    --db ~/.skill-stats/skill-stats.db
uv run skill-stats classify --db ~/.skill-stats/skill-stats.db [--limit N] [--fresh]
uv run skill-stats detect  --db ~/.skill-stats/skill-stats.db
uv run skill-stats lineage --db ~/.skill-stats/skill-stats.db
uv run skill-stats report  --db ~/.skill-stats/skill-stats.db --json --charts
uv run skill-stats update  --db ~/.skill-stats/skill-stats.db   # verify->walk->...->serialize
uv run skill-stats ci init [--branch main] [--no-push] [--upgrade]   # in the target repo
uv run skill-stats ci land --branch main                             # landing verb
uv run skill-stats gather --db union.db ~/dotfiles ~/.config-repo     # union of artifacts
```

`classify` shells out to the `opencode` CLI (`opencode run --format json`,
model configurable at `init` with `--model`, default `opencode/glm-5.3-flash`)
in its own scratch session dir so it never touches other sessions. If a reply
is not one JSON object line, it asks the model to repair once; unrecoverable
replies are stored as verdict `unknown` and excluded from metrics.

## Not built yet (per DESIGN.md)

- opencode-history import (schema is settled, built later).
- Plugin-logged skill metrics (ADR-0002: going-forward only).

## Development

```sh
uv run pytest            # full suite
uv run pytest tests/test_targets_lineage.py   # single file
uv run mypy src          # strict
uv run ruff check src tests
```
