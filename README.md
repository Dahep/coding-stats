# coding-stats

Mine git history to measure feature-level metrics: **features/year**,
**fixes/feature** (overall and per feature), and **code churn per feature**.
See `DESIGN.md` (decisions) and `CONTEXT.md` (domain language).

## Git pipeline (built)

1. `walk` — first-parent chronological walk of the Target branch (`main`).
   Squash-merged work appears once as its main-branch commit; `git patch-id`
   is stored per commit so patch-equivalent commits can be reconciled.
2. `classify` — rolling LLM session via the opencode CLI (ADR-0001): one
   commit per turn, one JSON reply per turn (`new | existing | fix |
   refactor | revert | cleanup`), checkpointed into SQLite per turn so runs
   are resumable. Raw LLM output is stored per commit for auditability; the
   single LLM verdict drives what metrics count (blame evidence never does).
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
   digest + verify. Default `--db coding-stats.db` on a machine; CI runs pass
   `--db .coding-stats/working.db` — the CI working DB is throwaway (runs are
   stateless and the committed artifact is the store), so a fresh DB adopts
   the checkout's git repo and seeds itself from the artifact. Classification
   is best-effort: a backlog stays visible in the final line and reprocesses
   on a later run.
7. `migrate` — one-time migration from the legacy `skill-stats` layout
   (ADR-0005): verifies the legacy artifact, reuses the machine-local DB
   when it corresponds (verdicts survive), relabels the repository key when
   the GitHub rename changed it, regenerates the store under
   `.coding-stats/`, removes the legacy `skill-stats.yml` workflow (`--ci`
   writes the new scaffold), and only then deletes `.skill-stats/`.
   Idempotent; `--dry-run` prints the plan first.
8. `ci` — `coding-stats ci init` writes `.github/workflows/coding-stats.yml`
   (plus `.coding-stats/.gitignore` for the throwaway DB) idempotently,
   refusing drifted files unless `--upgrade`; `ci init --pages` adds a
   deploy-pages job that publishes the run's report.html as the site root
   (opt-in: Pages only works on public or paid repos); `coding-stats ci land`
   (called by the workflow, usable locally) lands the artifact-update
   commit: no-op updates push nothing, superseded runs abandon neutrally,
   everything else commits as `coding-stats[bot]` and pushes with
   `GITHUB_TOKEN`.
9. `gather` — `coding-stats gather --db union.db <repo|artifact>...` ingests
   N artifacts into a union DB keyed by Repository key. Ingest verifies each
   artifact first, is idempotent per key (identical artifact = no-op, a newer
   one last-wins), and refuses when distinct keys share a URL-derived slug
   (one repo on two Target branches — never renames ids). The union is
   structurally single-repo: `report --db union.db` reads it unchanged, with
   REPO columns appearing only when it covers more than one repository.

## Live report (dogfooding)

The tool analyzes this repository on every push to `main`; the report publishes
to <https://dahep.github.io/coding-stats/> and the analysis artifact
(`.coding-stats/`) commits back to `main` as `coding-stats[bot]`.

## Usage

```sh
uv sync
uv run coding-stats init ~/dotfiles --db ~/.coding-stats/coding-stats.db
uv run coding-stats walk    --db ~/.coding-stats/coding-stats.db
uv run coding-stats classify --db ~/.coding-stats/coding-stats.db [--limit N] [--fresh]
uv run coding-stats detect  --db ~/.coding-stats/coding-stats.db
uv run coding-stats lineage --db ~/.coding-stats/coding-stats.db
uv run coding-stats report  --db ~/.coding-stats/coding-stats.db --json --charts
uv run coding-stats update  --db ~/.coding-stats/coding-stats.db   # verify->walk->...->serialize
uv run coding-stats ci init [--branch main] [--no-push] [--upgrade]   # in the target repo
uv run coding-stats ci land --branch main                             # landing verb
uv run coding-stats gather --db union.db ~/dotfiles ~/.config-repo     # union of artifacts
```

To run the update in CI, adopt the composite action `Dahep/coding-stats@v1`
(see `action.yml`): one `uses:` step after `actions/checkout` with
`fetch-depth: 0`. It installs the engine exactly at the action ref you pin and
lands every update on the Data branch `coding-stats/<branch>` — the analyzed
branch itself never receives bot commits.

For reports there is a second composite action, `Dahep/coding-stats/report@v1`
(see `report/action.yml`): it gathers every `coding-stats/*` Data branch of
the repo with Shared-commit dedup, renders one HTML report, uploads it as an
Actions artifact (and optionally the union DB, `include-db: true`) and deploys
to Pages only when `deploy-pages: true` (with `pages: write` / `id-token:
write` permissions in the report job). Trigger it by `workflow_run` after the
update workflow, a schedule, or manual dispatch — a `push` trigger on
`coding-stats/**` never fires, because pushes made with `GITHUB_TOKEN` do not
start workflows.

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
