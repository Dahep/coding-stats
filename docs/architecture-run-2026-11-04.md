# Architecture run 2026-11-04: seven deepenings, reviewed and shipped

An architecture review of 2026-11-03 surfaced seven deepening candidates. The domain decisions were
settled in a grilling session the same day; each PR was implemented by one thread, reviewed
and shipped by a second, independent thread, and merged sequentially so every PR branched
from the previous merge.

Order of landing (all squash-merged into `main`):

## 1. UpdateOptions + StoreLayout — #57 (merge `38de1fd`)

`cli._run_update` stops consuming a duck-typed `argparse.Namespace`; `migration` no longer
fabricates one by hand (the alien `no_report` field is removed). `legacy_store` became the named
`StoreLayout` enum across its four sites. A bidirectional drift guard pins parser flags ⇔
`UpdateOptions` fields.

## 2. aiattr one-read — #58 (merge `39af683`) · paper trail: [#38 comment](https://github.com/Dahep/coding-stats/issues/38#issuecomment-6623399024)

`aiattr.resolve` keeps its one-evidence-read promise: `effective_evidence` returns
contribution rows with identity (`harness`/`model`), the duplicated `pr_evidence` re-read and
second `suspicious` filter are gone, `metrics.resolve_attribution` shed its de-dup loop. The
ship thread re-ran its own equivalence probe (15 commits, 0 diffs) and strengthened the read
guard to count `resolve`'s total statements per commit. Ticket #38 (gather/report union of
recorded PR evidence) builds on this shape.

## 3. prsweep five faces → three behaviours — #59 (merge `2ac4b37`)

One `_run_face` carries the sweep behaviour (mode matrix documented); the five public names
remain one-line aliases — they carry ADR-0007 clause 2 vocabulary and tests monkeypatch two
of them. Caps stated once. AST-verified per-face behavioural identity.

## 4. tablecatalog — #60 (merge `1d5760c`)

One leaf declaration (`src/coding_stats/tablecatalog.py`, zero internal imports) owns every
table's row shape, with explicit divergence knobs (conflict policy per direction, per-direction
sentinels, conditional inserts, the settings whitelist). Generators produce the artifact DDL,
schema step-0 DDL, union INSERT/purge, ownership predicates and completion maps. The ship
thread verified FK-uniform equivalence (`PRAGMA foreign_key_list`), old↔new artifact
compatibility across both version boundaries, and added import-time guards requiring
gather/orgview plan coverage. SQL delta is formatting-only; the one removed FK clause was an
exact duplicate.

## 5. verdicts owner + Fix events everywhere — #61 (merge `962a641`)

`verdicts.py` owns the recorded verdict vocabulary, `is_fix()`, report order, and the
CHECK builder — generated once through tablecatalog. Every displayed "fixes" number is now
the CONTEXT.md **Fix event** (per-(fix, Feature) reach, Landing-dated): `fix_event_total`,
`fixes_rolling`, the timeline and by-model views; raw fix-commit counts survive only as the
labeled `uncovered_fix_commits` audit view. The donut renders all seven recorded verdicts
(merge included — the slices-sum bug is dead, pinned by a coupling test that introspects the
generated CHECK) and a literal guard bans verdict string literals outside `verdicts.py`.
The ship thread also fixed a pre-existing Landing-date bug: `gitwalk.author_and_time` now
uses `%cI` (committer date), not `%aI`.

## 6. seed_working_db — #62 (merge `75596a7`)

`db.replace_file` is the one atomic swap with one sidecar rule; `artifact.seed_working_db(db_path, bytes, *, register)`
is the single public restorer (`register=False` is migration's named relabel-authority
path). `materialize` went private; all four seed routes go through one entry, so the
recurring unsafe-seed warnings (issue #31's digest/stamp failure mode) disappear with their
subject. Review added a hardening fix: a forged header
stamp now refuses as a typed `MaterializeError` instead of a raw crash mid-seed.

## 7. derive stage — #63 (merge `3485801`) · paper trail: [#48 comment](https://github.com/Dahep/coding-stats/issues/48#issuecomment-6627552231)

`derive.py` replaces `targets.py`, `lineage.py` and `livelines.py`;
`fix_touches`/`fix_targets`/`fixes_features` are rebuilt from scratch every run in one
transaction — the append-only asymmetry and its stale-target retention are gone (regression
test at a raised threshold). The `detect`/`lineage` CLI verbs are replaced by one `derive`
verb; the `--no-detect`/`--no-lineage` update flags stay as debug trims. Spec #48 lands
Areas, Embedded fixes and Provenance columns on this substrate.

## Suite trajectory

415 (baseline) → 416 → 417 → 417 → **432** (catalogue) → **442** (verdicts) → **446**
(seed) → **448** tests, ruff and mypy strict clean at every merge. Zero coverage deleted;
every PR's body carries its merge-danger notes.

## Known follow-ups recorded during the run

- `attribution_trend`'s month normalization and its fix-verdict gate are pre-existing
  divergences, observed during the #61 review and not yet filed as an issue — follow-up
  issue worthy.
- Historical `feature_line_samples` rows keep old attribution after a raised
  `min_target_lines` until the next backfill sweep trigger (noted to #48).
- DESIGN.md still carries stale bullets (two "Feature size"/"Identity" pairs; the
  two-Target-branch claim superseded by ADR-0006's Status line — gather no longer refuses
  two Target branches of one repository) — doc clean-up candidate.
