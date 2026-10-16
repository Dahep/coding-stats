---
name: migrate-skill-stats
description: Migrate a repository off the legacy skill-stats layout onto coding-stats (ADR-0005) — committed .skill-stats/ artifact, .github/workflows/skill-stats.yml CI workflow, ~/.skill-stats machine-local DB. Use when a repo contains .skill-stats/ or skill-stats.yml, or when the user asks to migrate to coding-stats.
---

# Migrating a legacy skill-stats repo

`coding-stats migrate` automates the whole migration (added with the rename,
ADR-0005). It is idempotent — the second run is a no-op cleanup — and never
removes the legacy `.skill-stats/` directory until the regenerated
`.coding-stats/coding-stats.sql` verifies.

## Procedure

1. Inventory first, mutate nothing:

   ```
   uv run coding-stats migrate <repo> --dry-run
   ```

   The plan names the seed (one of `already`, `reuse-db`, `rebuild-artifact`,
   `fresh`), the relabel (when the GitHub rename changed the Repository key),
   and the cleanup list. Reading the plan beats guessing.

2. Seed semantics (why each is right):
   - **reuse-db** — a machine-local legacy DB corresponds to the legacy
     artifact (same repository key). Verdicts and the classify session
     survive; classification cost is zero. Destination defaults to
     `~/.coding-stats/coding-stats.db` when the legacy DB is at
     `~/.skill-stats/skill-stats.db`, else `coding-stats.db` — pass `--db`
     to choose.
   - **rebuild-artifact** — no usable legacy DB; a fresh DB is seeded from
     the legacy artifact bytes, whose verdict rows travel inside it.
     (Zero re-classification cost too.)
   - **fresh** — the legacy artifact fails verification or is missing:
     full recompute, re-classification included (LLM cost, one-time). This
     is the same fallback recovery uses; never block the migration on it.
   - **rename relabel** — when the GitHub repo was renamed, identity rules
     refuse key changes on DBs holding data. Migration is the only
     sanctioned path around that: it verifies the legacy artifact first
     (proving the DB belongs to this repository), then relabels loudly.
     A canonical → `local:`-shim resolution instead KEEPS the stored key
     with a notice (that is register_repository's documented behavior).
     A shim → canonical move upgrades during the migration update. A
     shim → different-shim move is refused; use `--fresh`.

3. Run it for real:

   ```
   uv run coding-stats migrate <repo> --db <db> [--legacy-db <db>] [--ci]
   ```

   `--ci` additionally removes any remnants of the legacy scaffold and writes
   the new workflow via `ci init` (the legacy `skill-stats.yml` is removed
   during cleanup in every run — a zombie workflow would keep running the
   old tool forever, since old pinned `uv tool install` URLs still resolve
   through GitHub's rename redirect).

4. Verify and commit:
   - `.coding-stats/coding-stats.sql` verifies, `report.html` exists,
     `.skill-stats/` and `skill-stats.yml` are gone.
   - Commit the new store (and scaffold) as a normal user commit — this is
     a migration commit, not a bot artifact-update commit.
   - Machine-local leftovers (`~/.skill-stats/*` reports, DB copies) can be
     deleted once the new DB path works.

## Ordering with the GitHub rename

- Migration relabels to the RESOLVED identity, so run it AFTER the GitHub
  rename (or after setting the remote URL) — otherwise the renamed repo's
  next update would refuse the relabel the normal way.
- Target repos whose CI upgrades later must land the migration (new store
  committed) BEFORE their CI runs, so CI seeds statelessly from the new
  artifact instead of full-recomputing. Existing CI with the legacy
  workflow keeps working through the redirect until then — but delete it
  the moment the new scaffold lands.
