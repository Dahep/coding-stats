-- coding-stats artifact
-- format: 1
-- schema-step: 2
-- repository-key: https://github.com/Dahep/coding-stats@main
-- covered-through: 92da29865a51b83ccdc197265743b44e1fc76fb3
-- digest: 32beee3db9a1fba86a5dd63e7370d2bc4a4ea406fe0a79f69d28dc3a3822bc3b
CREATE TABLE repositories (
               id INTEGER PRIMARY KEY,
               repo_key TEXT NOT NULL UNIQUE,
               slug TEXT NOT NULL,
               branch TEXT,
               clone_url TEXT,
               added_at TEXT
           );
INSERT INTO repositories (id, repo_key, slug, branch, clone_url, added_at) VALUES (1, 'https://github.com/Dahep/coding-stats@main', 'dahep-coding-stats', 'main', 'https://github.com/Dahep/coding-stats', '');
CREATE TABLE commits (
               id INTEGER PRIMARY KEY,
               repo_id INTEGER NOT NULL,
               sha TEXT NOT NULL,
               parent_sha TEXT,
               tree_sha TEXT NOT NULL,
               committed_at TEXT NOT NULL,
               author_name TEXT NOT NULL,
               title TEXT NOT NULL,
               message TEXT NOT NULL,
               subject_patch_id TEXT,
               walk_index INTEGER NOT NULL,
               kind TEXT NOT NULL CHECK (kind IN ('normal', 'merge')),
               diff TEXT NOT NULL,
               added_lines INTEGER NOT NULL DEFAULT 0,
               deleted_lines INTEGER NOT NULL DEFAULT 0,
               churn_lines INTEGER NOT NULL DEFAULT 0,
               UNIQUE (repo_id, sha),
               UNIQUE (repo_id, walk_index)
           );
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (1, 1, 'dc519177d9b1e47cc39a386d49d4fc2b9670b918', '76e9c8c0b0e7d9cc91b986501e8eefe14440e781', 'cfc404179cff3000e62d4d583553cfac71525234', '2026-10-02T16:37:03-03:00', 'Daniel Herrmann', 'Merge pull request #1 from Dahep/t3code/grill-idea-with-docs', 'Merge pull request #1 from Dahep/t3code/grill-idea-with-docs

What
Foundational docs for skill-stats, captured from the grill-with-docs session:

CONTEXT.md — domain glossary. Git domain: Feature, Fix, Fix lineage, Feature attribution, Commit verdict (Feature/Fix/Refactor/Revert/Cleanup), Refactor, Revert, Cleanup. opencode domain: Skill invocation, Skill version, Skill cost, Steer, Historical invocation, Target branch.
DESIGN.md — post-grill design: git pipeline (Target-branch walk, LLM commit verdicts, blame-evidence fix detection with fix_touches/fix_targets/fixes_features tables), opencode pipeline (schema settled, build deferred), and the adjudicated answers from the grill.
ADR-0001 (accepted) — LLM-based feature classification via opencode CLI, over heuristics, manual curation, and batch prompts.
ADR-0002 (accepted) — skill metrics require plugin-logged invocations; no backfill of pre-plugin sessions.
Reviewer guidance
Docs-only, no code yet. Worth checking that the glossary terms match the vocabulary you intend to use going forward — they drive all later metric definitions. Known deliberate choices to not "fix": a Revert is never a Fix; single commit verdict per commit; merge is a kind, not a verdict.

Notes
No tests/gates exist yet (no code). Design-doc nits from self-review already applied: patch-equivalent (not duplicate-hash) commit reconciliation, Target-branch terminology alignment, historical-session boundary wording.', '30acb494b840541835bb715f1ddcac61f28a2ac1', 2, 'merge', '', 185, 0, 185);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (2, 1, '96bba5112039499782c5c92cfc68a666e2dca5c9', 'dc519177d9b1e47cc39a386d49d4fc2b9670b918', '25aae5555e5f0a6950a969c24efc2f995c99b07d', '2026-10-05T10:10:03-03:00', 'Dahep', 'scaffold: uv package, sqlite schema (commits/features/fix tables), db migrations', 'scaffold: uv package, sqlite schema (commits/features/fix tables), db migrations', 'af11000059318aa62efbcc23d4a512e3a3a41966', 3, 'normal', '', 1030, 0, 1030);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (3, 1, 'b04223b4dfe071528bc2cce640b5371b3652d965', '96bba5112039499782c5c92cfc68a666e2dca5c9', 'fb0350c55f3482576c86fd08ff4c29c3a6fdd236', '2026-10-05T10:10:03-03:00', 'Dahep', 'walk: first-parent Target-branch import with patch-id evidence', 'walk: first-parent Target-branch import with patch-id evidence', '9ab60452b50bdf5e56a64717dfac9a3d841a25f5', 4, 'normal', '', 243, 0, 243);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (4, 1, '86645df2aee5c8f5fe9a4d0e620e3dc4c69cc3fb', 'b04223b4dfe071528bc2cce640b5371b3652d965', '6c780648be851fe6c41d375b3c04d0eec0830b94', '2026-10-05T10:10:03-03:00', 'Dahep', 'classify: rolling opencode-CLI LLM session, resumable per-commit checkpoint', 'classify: rolling opencode-CLI LLM session, resumable per-commit checkpoint', '44e615029f52220805a49ca5f3eafd7175a6cb68', 5, 'normal', '', 402, 0, 402);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (5, 1, '8b9154ccec75712fe9f67a1fee7ade207519b515', '86645df2aee5c8f5fe9a4d0e620e3dc4c69cc3fb', 'e66be35bfbd528658e616c406e661e489e96b724', '2026-10-05T10:10:03-03:00', 'Dahep', 'fixes: blame -w -C --first-parent evidence, fix_targets, lineage closure', 'fixes: blame -w -C --first-parent evidence, fix_targets, lineage closure', 'b2a58755ed06942b5d893af9e55a11f29a318c0a', 6, 'normal', '', 384, 0, 384);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (6, 1, '7401695136ca5cb12b38de7c84f9d628c276fe5f', '8b9154ccec75712fe9f67a1fee7ade207519b515', 'af7a2feec2ba0d3e074596599fed14c3d3c82511', '2026-10-05T10:10:03-03:00', 'Dahep', 'metrics+cli: features/year, fixes/feature, churn; text/json/charts report', 'metrics+cli: features/year, fixes/feature, churn; text/json/charts report', 'b46f9ee829cbf7ba93260b7f0558e2e9e93d43f7', 7, 'normal', '', 534, 0, 534);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (7, 1, '1071f03aa2742cc7fb662e049eb76ec3dbd024cd', '7401695136ca5cb12b38de7c84f9d628c276fe5f', '3883f3202716d2510ef5a9349b7899948d4ed810', '2026-10-05T10:10:07-03:00', 'Dahep', 'tests: shared git fixture repo builder', 'tests: shared git fixture repo builder', '8474e6b6507fb0e6f30115e641ef8731e1479e63', 8, 'normal', '', 90, 0, 90);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (8, 1, '5286dc4f71aa54bf355d519045e5b86d79d9141f', '1071f03aa2742cc7fb662e049eb76ec3dbd024cd', 'd9350ee9bc1f584b00bff73e4a40b5b659740762', '2026-10-07T10:38:31-03:00', 'Dahep', 'docs: update opencode pipeline spec for V2; classify sessionID from first event', 'docs: update opencode pipeline spec for V2; classify sessionID from first event', 'c48a53c2e76879dc7b1c2e9137da46b5eb01f104', 9, 'normal', '', 53, 8, 61);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (9, 1, '1bbe64a60d3a3ec30fc891a01439c9025edb9b2e', '5286dc4f71aa54bf355d519045e5b86d79d9141f', 'd6383734189be41a64bb3016bcd311753429a664', '2026-10-08T14:03:03-03:00', 'Daniel Herrmann', 'Merge pull request #3 from Dahep/t3code/f4b16bae', 'Merge pull request #3 from Dahep/t3code/f4b16bae

skill-stats: self-contained HTML/PDF report — fix ranking, features and rolling fixes/feature over time', 'ff9c287330987b462b87d643bbc351dd2da40f3c', 10, 'merge', '', 710, 9, 719);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (10, 1, 'c9409dc81199a73fca430c568055340bfadadda1', '1bbe64a60d3a3ec30fc891a01439c9025edb9b2e', 'eba39081eae9d091e4ca1c15a186a99c65b492ee', '2026-10-10T00:16:26-03:00', 'Daniel Herrmann', 'docs: multi-repo artifact spec (ADR-0003) + grill close-out (#4)', 'docs: multi-repo artifact spec (ADR-0003) + grill close-out (#4)

* docs: grill close-out — ADR-0003 (in-repo artifact, integrity policy), glossary entries, DESIGN/README synthesis

* trail: record shipped PR pointer', '6838778005c913deefbb7ec1b1af6b559c49da11', 11, 'normal', '', 244, 4, 248);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (11, 1, '8e02366903207fa1c4eafbed7e646e973d3df525', 'c9409dc81199a73fca430c568055340bfadadda1', 'ea5aabcc68e45b097a74ce1090a13626b028bcc2', '2026-10-10T09:10:17-03:00', 'Daniel Herrmann', 'feat: multi-repo schema, store-path exclusion, churn columns (ADR-0003 wave 1) (#5)', 'feat: multi-repo schema, store-path exclusion, churn columns (ADR-0003 wave 1) (#5)

* feat: wave 1 artifact/multi-repo schema (ADR-0003 clauses 2, 4, 5)

Migration step 1 (runs automatically on connect): repositories table,
commits keyed UNIQUE (repo_id, sha) / (repo_id, walk_index) with
added/deleted/churn line columns, features.id becomes repo-prefixed TEXT
''{slug}-{n}'', commits_features/fixes_features follow the new ids while
attribution keeps commits.id INTEGER. Old DBs get the documented
''local:<basename>@<branch>'' identity shim; old diffs are re-derived with
the walk''s exclusion + churn rules.

Walk: one-pass prepare_diff drops .skill-stats/** sections (any depth)
and counts churn; store-only commits are skipped with walk_index gaps
and nothing stored; commits carry repo_id. Blame ignores store paths.
init resolves and normalizes the origin URL (ssh -> https) into THE
repositories row and settings.repo_id. classify mints/cites prefixed
feature ids; metrics reads the stored churn columns (churn math lives
only in gitwalk.prepare_diff).

* fix: wave 1 review fixes (atomic migration, quoted diff headers, rename blame, re-init safety, stale purge)

1. Migrations are atomic: db._apply_migration wraps each callable step in an
   explicit BEGIN covering all DDL+DML plus the _schema_step_NNNN marker
   (python sqlite3 autocommits DDL outside a real transaction), FK toggling
   outside; mid-migration failure rolls back to the step-0 shape and the DB
   stays reopenable (failure-injection test).
2. Quoted diff headers handled: parse_diff_header decodes git''s C-quoted
   (octal) paths and bare spellings; every diff --git line resets section
   state, so store content can neither leak after a normal section nor
   swallow a later normal one (real git quoted-path tests, both orders).
3. Blame excludes store paths on BOTH header sides, closing the modified
   rename-into-store blindspot; artifact hunks contribute no fix attribution.
4. register_repository never silently relabels: same key is a no-op, key
   change with data is refused (shim -> canonical upgrade allowed), and a
   canonical key is never downgraded to the local: shim when origin
   resolution fails (four-branch tests).
5. Migration purges store-only commits with every dependent row and drops
   orphaned features, so migrated DBs match fresh walks; equivalence test
   compares full metric snapshots and derived columns.

* trail: wave-1 rows + deferred wave-2 obligations', '7d2b987ac1e8b13b4e195dd2e0f32d89e9d38802', 12, 'normal', '', 1952, 225, 2177);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (12, 1, 'ae43736da29ae81139769ab1a8c6335e9d4a0065', '8e02366903207fa1c4eafbed7e646e973d3df525', 'df835074f1165b280ebfaacf2e18903e6bc4a3d6', '2026-10-11T01:05:32-03:00', 'Daniel Herrmann', 'feat: artifact serializer + update verb + live-lines (ADR-0003 wave 2) (#6)', 'feat: artifact serializer + update verb + live-lines (ADR-0003 wave 2) (#6)

* feat: live-lines at tip + Sample backfill (schema step 2, ADR-0003 feature size)

livelines.update_live_lines blames every tracked file at the latest walked
Target-branch commit (store paths and submodule gitlinks skipped) and accrues
each commit''s surviving lines to its features: directly via
commits_features (defines/touches), fix lines through the fixes_features
lineage closure, a commit claimed by two features counted in both. Lines
whose introducing commit is outside the DB are counted as unattributed_lines,
never dropped.

Schema step 2: features.live_lines (NOT NULL DEFAULT 0) and
feature_line_samples (feature_id, at_commit_sha, live_lines) — one Sample
family for forward runs (idempotent per sha) and backfill. Backfill runs once
when the samples table is empty: full sweep per walked commit, ~160 evenly
spaced points past a 400-commit cap (grill-r3-Q9 leaves the cap to
implementation; both constants documented).

* feat: report live-lines column + Feature size over time chart

Ranking table gains a Live lines column (features.live_lines via
FeatureRank.live); a new sample-driven ''Feature size over time'' section
charts the top-8 features by live lines plus a total-live-lines line from
feature_line_samples (inline-SVG polylines, same self-contained style),
omitted gracefully when no samples exist. The unattributed_lines count
(livelines persists it to settings) surfaces in the section note instead of
being dropped.

* feat: artifact serializer — deterministic SQL-text snapshot + digest (ADR-0003)

artifact.py writes the committed .skill-stats/skill-stats.sql: a
self-executing whole-snapshot dump (curated CREATE TABLE + one canonical
INSERT per row, stable per-table ordering) with the header digest line last.
The Artifact digest is sha256 over the content with its own value blanked;
verify()/digest_of() recompute it. Curated projection per ADR-0003 clause 2:
runs out entirely, machine-local settings out (repo, classify_session_id),
wall-clock columns frozen to sentinels (repositories.added_at), commits.diff
payload empty, run provenance NULL; classified_at kept as stable audit data.
write() lands the SQL plus the regenerated report.html from the working DB.

Tests: byte-stable across calls and across identically built DBs (wall-clock
must not leak), tamper detection, standalone materialization via sqlite3 with
a drift guard comparing artifact shape against working tables.

* feat: skill-stats update verb — orchestrating run with backlog visibility

update = verify digest of the existing artifact first (missing = ok first
run; a MISMATCH is logged loudly and superseded by the rewrite — the
partial-recompute recovery is wave 3) -> walk -> classify -> detect ->
lineage -> live-lines -> report -> serialize + digest + verify, with
--no-classify/--no-detect/--no-lineage/--no-report/--no-serialize trimming
stages for dev. Deterministic stages always run; classification is
best-effort (Q13): when the opencode CLI or model fails the walked backlog
stays pending, prints a loud WARNING and the final visibility line
(''N walked, N classified, N pending, artifact updated''), and reprocesses on a
later successful update. Exit code stays 0 with a backlog. Stage
subcommands remain plumbing.

* fix: artifact header injection hardening + byte-exact digest verification

1. Every header value goes through _hdr(): printable-ASCII-minus-backslash
   stays literal, every other byte (newlines, control chars, non-ASCII, the
   escape char itself) becomes \xNN — single-line ASCII always, so a crafted
   repository key can never inject SQL or sqlite3 dot-commands into the
   self-executing dump. The escaping is injective (backslash escaped).
2. verify()/digest_of() operate on raw bytes: the digest line is located with
   an ASCII bytes pattern, only its value is blanked, exact bytes are hashed.
   CRLF-normalization can no longer launder a tampered file through text
   mode, and invalid UTF-8 yields a failure verdict instead of a decode crash
   that aborts update before the artifact is superseded.

* fix: live-lines raw ls-tree names + first-parent blame semantics

3. _files_at lists files with raw NUL-delimited ''ls-tree -r -z'' names and
   decodes any defensively quoted spelling before the store-path check and
   before blaming: text ls-tree output C-quotes non-ASCII names, which both
   broke blame/blob lookups (live lines silently lost) and ran the
   .skill-stats exclusion against the quoted spelling (store paths slipped
   through).
4. Blame runs with --first-parent, matching the first-parent walk and
   blame.py''s detect path: lines brought in by side-branch commits (absent
   from the DB) attribute to the merge commit the walk includes, instead of
   being miscounted unattributed.

* fix: update''s best-effort classify boundary catches ValueError too

5. The Q13 best-effort boundary must cover apply_reply failures as well:
   a well-formed reply citing an unknown feature id (and JSONDecodeError,
   which subclasses ValueError) aborted detect/lineage/live-lines/serialize
   and the backlog line. update now catches ValueError at the classification
   call into the same loud-WARNING+pending path, surfacing what landed before
   the abort (''N classified before the abort'') — the raising commit and the
   remainder stay pending for a later update; nothing downstream is blocked.
   The classify plumbing verb keeps raising.

* fix: backfill trust state + honest incomplete sampling; total renamed

6. backfill_state (settings JSON: status pending|done + the max classified
   walk index at sweep end) replaces ''any sample disables backfill forever''.
   The sweep re-runs when samples are absent, the state is not done (fresh or
   interrupted sweep), or new classification landed after the recorded index
   — so late-created features gain their historical samples and interrupted
   sweeps complete on the next update. With a classification backlog the
   status stays pending (re-sweeps until the backlog clears); a fully
   classified walk marks the sweep done. backfill_state joins the artifact''s
   settings whitelist.
7. The chart total is ''total attributed lines'' with a note that multi-feature
   claims can exceed the repository''s line count; while sampling is
   incomplete the report says so and the curves render gaps (values only
   where samples exist) instead of zero-filled pseudo-measurements. Pinned:
   a known commit without feature links accrues to neither feature sums nor
   unattributed (unattributed = introducing commit unknown only).

* trail: wave-2 rows + wave-3 obligations', 'f6cbe66aac248f3eb341cf0167a0bea1c98890d0', 13, 'normal', '', 1839, 45, 1884);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (13, 1, 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', 'ae43736da29ae81139769ab1a8c6335e9d4a0065', 'c828485d556273c9b7ba972c8b1de6beb8e15e95', '2026-10-11T11:29:49-03:00', 'Daniel Herrmann', 'feat: partial recompute + CI scaffold (ADR-0003 wave 3) (#7)', 'feat: partial recompute + CI scaffold (ADR-0003 wave 3) (#7)

* feat: partial recompute on digest failure (ADR-0003 clause 3)

On artifact digest MISMATCH the tampered content is never trusted: recovery
walks the artifact file''s git history for the newest committed version whose
embedded digest self-verifies (artifact.verify_bytes), materializes it into a
fresh working DB (artifact.materialize — the shared loader, schema markers
included) and the update flow re-walks/re-classifies only commits past its
coverage horizon, logging the anchor version and the new horizon. A tampered
current file and a tampered latest commit resolve through the same path. When
no committed version verifies (history rewritten / none exists) the fallback
is a loud FULL recompute from scratch — distinct from restore-recover.

The same rebuild primitive serves the throwaway-DB decision behind CI: a
fresh working DB (no repo recorded) adopts the git repo at the current
directory and seeds itself from a VERIFIED artifact, so stateless runs
continue from the committed store instead of re-analyzing everything.
Non-derived settings (machine-local + user config) survive a rebuild;
derived measurement state never does. No new user-facing flags: MISMATCH
always attempts anchor restore then falls back to full recompute (recorded
policy). The two wave-2 ''MISMATCH + supersede'' tests now assert the recovery
verdict.

* feat: CI scaffold — workflow rendering + landing verb (ADR-0003 clauses 6-7)

skill-stats ci init writes .github/workflows/skill-stats.yml idempotently
(identical content = assertion; drifted content refuses with a diff summary
unless --upgrade) plus .skill-stats/.gitignore for the throwaway CI working
DB. The workflow: push trigger on the Target branch with
paths-ignore .skill-stats/** (belt-and-braces alongside the exclusion ring,
Q18.1), cadence configurable in the on: block, fetch-depth 0, uv + opencode
CLI install with OPENCODE_API_KEY as best-effort (Q13 — no required secret,
no model names; settings travel in the artifact), update --db
.skill-stats/working.db, report.html upload with if: always(), and a landing
step calling  (permissions: contents: write, GITHUB_TOKEN only —
Q17, no PAT bits).

skill-stats ci land keeps the Q15/Q18 landing rules in Python where tests
run them: noop when .skill-stats/ is unchanged (nothing pushed), abandoned
with a neutral ::notice:: when the checked-out push is behind the branch tip
or diverged (never force, never block), else a ''skill-stats artifact update
(auto)'' commit by skill-stats[bot] pushed to exactly the given branch.
README updated for the shipped update/ci behavior only.

* fix: recovery correctness — diff rehydration, atomic swap, branch-scoped anchor

1. Restored/seeded pending commits regain their real diffs: after
   materialize, commits without a verdict row get commits.diff refilled from
   git through the walk''s own diff+prepare_diff path (store paths stripped,
   merge diffs vs first parent as diff_for does). The artifact format stores
   '''' for the machine-derivable payload; without this the backlog was
   classified against noise and merges silently passed the empty-merge
   shortcut. Classified commits keep '''' — their diffs are already consumed.
2. The DB swap is atomic again: os.replace runs with the original still in
   place (stale -wal/-shm/-journal sidecars removed only after a successful
   swap; a failed swap cleans the temp file and leaves the original intact).
3. find_anchor is scoped to the Target branch ref (git log <branch> -- path):
   artifact versions reachable only from another ref can no longer anchor a
   restore — their rows would make the Target walk look rewritten. An
   unresolvable ref means no anchor (full recompute, the safe side).

* fix: ci land push-race neutrality + single-print landing status

4. The check-to-push race window is closed: a rejected push re-fetches and
   re-verifies ancestry against the original base — a remote tip now past or
   diverged from that base is the Q18 superseded case and ends in the neutral
   ''abandoned'' outcome (never force, never block), while unrelated failures
   (auth, network) still raise. Before, a non-FF rejection escaped as a
   CalledProcessError and reddened the workflow.
7c. commit_or_abandon returns a Landing(status, detail) struct and stays
   silent; report_landing is the single printing point (neutral ::notice:: +
   step summary for abandonment), called once by the CLI.

* fix: workflow parameter hygiene, standalone tool install, init hardening

5. Branch/db travel via env + quoted "$VAR" in run lines (one mechanism,
   consistently) and every YAML scalar is single-quoted (''true'' never a bool,
   '''' doubling for quotes) — hostile values (feature;id, true, café branch,
   db paths with spaces) render YAML that parses with clean strings and no
   injection surface in run: lines (pyyaml added as a DEV dependency for the
   parse test only). ci init validates the branch against git/GitHub ref-name
   rules (repr in the error).
6. The target repo does not depend on skill-stats: the workflow installs the
   tool standalone (uv tool install "skill-stats==<version>", the pin read
   from the package''s own distribution metadata at render time so upgrades
   re-render) and invokes it directly — the target''s uv sync is gone. The
   opencode installer step is continue-on-error: a failed download must never
   abort the deterministic stages.
7a. ci init preflights drift across ALL managed files before writing either:
   a refusal never leaves a half-installed scaffold.
7b. The store gitignore is derived from the configured --db (name + -wal/
   -shm/-journal sidecars, exact entries so the artifact files can never be
   swallowed); a db colliding with skill-stats.sql/report.html is refused.

* trail: wave-3 rows + wave-4 gather obligations', '46f391ab93ad2057799836e90630e65f836d6093', 14, 'normal', '', 1461, 21, 1482);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (14, 1, 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', '242ee6af0aa2e27c267af2bbb1cb69933d75bcc0', '2026-10-11T13:13:42-03:00', 'Daniel Herrmann', 'feat: gather — union DB from committed artifacts (ADR-0003 wave 4, final agreed scope) (#8)', 'feat: gather — union DB from committed artifacts (ADR-0003 wave 4, final agreed scope) (#8)

* feat: gather — artifact ingest into a Repository-key union DB (ADR-0003)

gather.py ingests N artifacts (repo checkouts or explicit file paths) into a
union DB that is a normal skill-stats DB — same tables and joins, so
report/metrics read it unchanged (grill-r4-Q14). Feature ids are already
globally-unique prefixed TEXT and copy verbatim; artifact-local commit ids
are remapped into union-space ints in deterministic ingest order (sources
sorted by Repository key, stable within a key), every attribution edge
rebuilt through (repo, sha). UNIQUE (repo_id, sha) catches surprises; commit
ids are union-local and nothing displays them (reports surface shas).

Ingest rules: verify_bytes first (a non-verifying artifact is refused before
anything is written); same key + identical artifact = idempotent no-op,
same key + different artifact = last-ingest-wins upsert of that repo''s rows;
distinct keys sharing a URL-derived slug (one repo on two Target branches)
refuse with the offending keys named — never rename (grill-r5-Q18.2); the
\xNN header convention is decoded (artifact.parse_header/unhdr) and
cross-checked against the rows. Union-only metadata (digest, coverage
horizon, backfill_state, display fields) lives in union_artifacts — a
separate table, so the repositories shape stays single-repo identical; the
union stores no backfill_state of its own (completeness is derived at
generate time).

* feat: union report surface + least-complete sampling rule

2. metrics.sampling_complete derives completeness at generate time: a union
   DB is only as complete as its LEAST complete constituent (union_artifacts
   carries each artifact''s backfill_state — the union stores none of its
   own), so the chart''s incomplete-mode gap rendering wins over zero-fill
   whenever ANY constituent is incomplete; single-repo DBs keep following
   their own backfill_state.
3. Org report surface, no new chart formats: text_report top lists and the
   html ranking/churn tables gain a REPO column when the DB holds >1
   repository (FeatureRank.repo + metrics.feature_repos); single-repo
   rendering is untouched (guarded by test). Branding: org mode titles
   ''skill-stats report · N repos'', a single-repo union brands by Repository
   key slug, machine single-repo keeps its path basename.

* docs: gather shipped — README pipeline/usage + DESIGN pointer

* fix: never execute artifact SQL as code — materialize authorizer (CRITICAL)

The artifact digest is an unkeyed checksum — any committer can recompute it
— so a checksummed artifact is DATA, never code. artifact.materialize now
replays under an SQLite authorizer with a small complete allowlist: CREATE
TABLE of the known curated tables (column shape verified post-replay),
INSERTs into them, plain reads of them, sqlite''s own schema-table bookkeeping
for those CREATEs, SELECT and transactions. Everything else denies:
ATTACH/DETACH any database, PRAGMAs, DELETE/UPDATE/DROP, triggers, views,
temp objects, explicit indexes, functions. Schema-step bookkeeping moved out
of the replay (stamp_schema_steps, called by recovery after the guarded
replay) — bookkeeping tables are ours, not artifact data.

Both untrusted-bytes callers share the fix: gather ingest (refusal names the
source) and recovery rebuild (anchor bytes from the artifact''s git history).
Tests: 11 crafted correctly-checksummed hostile artifacts (ATTACH, cross-db
CREATE/INSERT, dot-command, PRAGMA write, DROP/DELETE/UPDATE, rogue
CREATE TABLE/VIEW/TRIGGER) refused through materialize; gather and recovery
paths refuse too; a benign artifact still replays with exactly the curated
columns (drift guard).

* fix: artifact ownership preflight + gather input error boundaries

5. Defense in depth beside the authorizer: a replayed artifact must be ONE
   repository''s rows — exactly one repositories row and every row of every
   table (commits, features, verdicts, attribution edges, samples) belonging
   to it. A crafted checksummed artifact can no longer smuggle foreign-repo
   rows that the ingest remap would silently adopt; the refusal names the
   table and row count (runs inside materialize, so gather AND recovery get
   it before any write).
7. Expected input failures become actionable errors at the gather boundary:
   a destination that is not a skill-stats database (other tool''s schema,
   garbage file) names the dest with a ''does it have skill-stats tables?''
   hint; malformed/empty artifacts name the source. Programming errors still
   propagate.

* fix: union chart correctness, per-feature commit links, branch branding

2. The size-over-time shaping moved into _size_series and made
   union-correct: samples join their feature''s OWN repository''s commit
   ((repo_id, sha) — fork-shared shas can never double-count totals), the
   axis is chronological (committed_at, sha; per-repo walk indexes overlap
   across a union), and each feature''s line spans only ITS OWN sample dates
   — staggered sampling renders gaps, never invented zeros. Crafted-union
   tests pin shared-sha totals, staggered lines, and the single-feature case.
3. Ranking commit links resolve per FEATURE via its repositories row
   (clone_url), falling back to the settings.repo resolution for machine DBs
   — gathered reports no longer render bare shas. Test: one report, two
   origins, two correct link domains; machine fallback covered.
4. Branch branding: a one-repo union shows its repositories.branch (never a
   ''main'' default), multi-repo unions make no branch claim anywhere (h1/who/
   methodology foot); machine DBs unchanged.

* fix: canonical backfill of shim-keyed repositories at gather (wave-4 obligation)

6. A shim-keyed artifact (''local:...'') ingested from a checkout whose origin
   resolves is normalized to the CANONICAL identity at the union level:
   repositories row gets the canonical key/slug/clone_url and feature-id
   prefixes follow the canonical slug (local numbers preserved,
   walk-order-stable), so shim- and canonical-produced artifacts of one repo
   alias into ONE key with CONSISTENT ids. The normalization also fires for
   canonical-keyed artifacts that already match the resolved key — seeded
   rows can carry mixed shim-era prefixes (the wave-1 upgrade hazard) and a
   partial rewrite left ''dotfiles-*'' ids beside ''dahep-dotfiles-*'' ones.
   Sources without checkout context (explicit artifact files) and checkouts
   without a resolvable origin stay shim-keyed. The artifact file on disk is
   never touched: union-only normalization. Alias overlap resolves through
   the normal upsert path (last-ingest-wins), tested with a real shim+canonical
   pair sharing two commits.

* test: shared gather factory, ordered determinism projection, API note

8a. The determinism test compares an ORDERED canonical projection (fixed
    ORDER BY — unordered SELECT * only compared as sets) and re-asserts
    determinism across source TYPES (same artifacts gathered once via
    checkout dirs, once via explicit file paths): rows match exactly except
    union_artifacts.source_path, documented as intentional provenance and
    excluded on that basis.
8b. The duplicated scratch-repo setup collapses into one shared factory with
    an optional classifier-behavior parameter (no_classify / fail_after).
8c. FeatureRank carries an ''internal API; callers unpack by name'' note.

* trail: wave-4 rows + surfaced leftovers', '62b17686953cf7ef2df0de1867f9ce9c01767f98', 15, 'normal', '', 1652, 64, 1716);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (15, 1, '870a7439351a086b07a40978ee855c29e50b9930', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 'd69b766a514b5c743b6f3ca3b44360b9e172c157', '2026-10-11T13:32:57-03:00', 'Dahep', 'feat: git-pinned tool install + pages deploy opt-in', 'feat: git-pinned tool install + pages deploy opt-in

- workflow installs skill-stats standalone from the git pin of the
  rendering checkout (package is not on PyPI; version pin would fail CI)
- ci init --pages renders a deploy-pages job that publishes report.html
  as the Pages site root (scoped job permissions, no contents write)
- ADR-0003 amendment + README', 'e2b2408c1a08b6d4472a0fa6e2b326f1516aa1f2', 16, 'normal', '', 189, 18, 207);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (16, 1, 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', '870a7439351a086b07a40978ee855c29e50b9930', '81521b2f1a3e0a1c642851042ca3c8d35f7c9e2b', '2026-10-11T13:38:39-03:00', 'Dahep', 'fix: tolerate non-UTF-8 text files in walk + blame decode', 'fix: tolerate non-UTF-8 text files in walk + blame decode

Vendored text-ish binaries (no NUL byte => git treats them as text) made
strict UTF-8 decoding of git diff/blame output crash update with
UnicodeDecodeError. gitwalk._git now decodes with errors=''replace''
(line structure survives; churn/patch-id/classifier context unaffected)
and livelines treats undecodable blame output as unattributed
(_blob_line_count), matching the binary-file bucket. Test in
test_livelines.py reproduces with a physical 0x89-byte file.', '37fbc412b29a05599d1e96c957c5d25d6bf37b8b', 17, 'normal', '', 60, 7, 67);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (17, 1, 'fb62a1504040479027cb455e4c30f23760e7daff', 'adac4c27f7be6b83738344fcfb0dec87acd1dc4e', '4170c4e04c1da817c5bf3af921636fb382074452', '2026-10-11T13:53:28-03:00', 'Daniel Herrmann', 'ci: stage skill-stats scaffold (workflow + store gitignore) (#11)', 'ci: stage skill-stats scaffold (workflow + store gitignore) (#11)

Rendered by the merged tool at main''s tip with pages opt-in; merge
fires the first dogfood run.', 'd5db86e56bb7054234bcd745df3df7918a97891d', 19, 'normal', '', 98, 0, 98);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (18, 1, '80802547b5142cd82882ad3cca5750a7ce775693', '5bc5c003249103adfa14e01dace2c45f8be5fe24', 'ab1379f559c9cc07197038b441403f1046d55426', '2026-10-11T13:56:39-03:00', 'Daniel Herrmann', 'fix: align live-lines binary accounting with walked churn (#10)', 'fix: align live-lines binary accounting with walked churn (#10)

NUL-containing blobs are binary to git (numstat ''-'': churn never counts
them); live-lines now skips them too — bytes are content, not lines, so
they must never surface as fake unattributed lines (dotfiles pilot: 154k
fake unattributed lines from a vendored .deb + PNGs). Weird-text blobs
(high bytes, no NUL) stay text: porcelain decoded with ''replace'' so
blame attribution keeps working. Binary probe: streaming git cat-file
batch capped at the 8000-byte head (git''s own heuristic window).', 'd99655eff38972b567752437217defb69d39f57b', 21, 'normal', '', 84, 27, 111);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (19, 1, 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 'dea4362e67a5852f839121b621f861b6341ed53d', '2154fbe9452efc1c5e0c89589c3bebd63859969c', '2026-10-11T15:54:23-03:00', 'Daniel Herrmann', 'docs: document the live dogfeed report URL (#13)', 'docs: document the live dogfeed report URL (#13)', 'ad08631666217fa3239726a687d48181427d2010', 23, 'normal', '', 6, 0, 6);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (20, 1, '2c2d504fdd9054ffd72bed97c1157826e90a3797', 'e9fcba6f347c88ef4beab121f5c03a94be9802ed', 'c864d64144f021fe46bb88a9d92f61fce8dbf040', '2026-10-11T16:48:17-03:00', 'Daniel Herrmann', 'fix: ssh-alias hosts are machine-local names, never identity (#14)', 'fix: ssh-alias hosts are machine-local names, never identity (#14)

An origin spelled git@github-personal:... names a port/key mapping in
~/.ssh/config, not a host — the same identity class as a local path.
normalize_clone_url now consults ssh -G (pure config read, no
connection) and refuses alias-resolving hosts (falls back to the
documented local: shim); real-host spellings, IP literals, and
resolved-equal hosts keep canonicalizing. ssh config file injectable
for tests. Found in dogfooding: the alias-baked key in the accidental
artifact landing blocked CI''s seeded working DB on the relabel refusal
gate (run 38157341104). ADR-0003 amendment + CONTEXT.md avoid-rule.', 'e33d4c63058a72fcf7cacff18c106cf3810f746f', 25, 'normal', '', 107, 4, 111);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (21, 1, 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', '03a8a8dddd350477eb23ff5696fb4f9b5ee490f3', '8d3595df3d760725ca9501e62db08ac8b897f073', '2026-10-15T12:29:19-03:00', 'Daniel Herrmann', 'Reject System One (Jev) backend; CI carries the OpenRouter key as well (#15)', 'Reject System One (Jev) backend; CI carries the OpenRouter key as well (#15)

* feat: System One (jev) classification backend + CI key routing (ADR-0004)

classify becomes a backend protocol (Backend.classify -> Reply+raw): the
ADR-0001 opencode rolling session keeps its semantics under
OpencodeBackend, and SystemOneBackend sends one POST per commit — a Choice
question over the six verdicts plus a Feature-citation question, with
confidence+probabilities stored as rationale. Routes: zen
(OPENCODE_API_KEY), openrouter (OPENROUTER_API_KEY), typesafe direct; the
CI workflow passes both secrets so company (OpenRouter) and personal
(Go/Zen) repos need no per-repo code. init grows --backend.

Live A/B on this repo (19 first-parent commits, Zen route): 95%
verdict-level agreement with the committed LLM session, zero unparsable
replies, 81k input tokens (~$0.003), 17s; both disagreements arrived at
confidence < 0.7 — a hybrid gate would catch them at ~37% escalation.

* drop System One backend (ADR-0004 rejected); keep OpenRouter CI secret

Jev''s blocking constraint hit exactly where it hurts: a decision model
cannot generate text, so Feature canonical titles/about degrade to
verbatim commit subjects and the fix needs an LLM in the write path
anyway. Backend protocol, systemone client and tests removed; measured
A/B numbers preserved in the ADR as the recorded rejection.

Stays: the workflow template now passes OPENROUTER_API_KEY alongside
OPENCODE_API_KEY so company (OpenRouter) and personal (Go/Zen) repos
pick per-repo config --model with no code changes.

* fix ADR-0004 comment drift (jev citation) + add Consequences section', 'a2cd402723d3ff174d872e5b038f1f8d438423ed', 27, 'normal', '', 57, 3, 60);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (22, 1, 'a04a9a759ee98f11cbbc2880980473add5e3a49a', '10db353b82185cd96b3d593fc9e6a1b94d6dc992', '559d809997a406bf04d491c44de7960eed83f8e2', '2026-10-16T20:57:32-03:00', 'Daniel Herrmann', 'Rename project to coding-stats (#16)', 'Rename project to coding-stats (#16)

* refactor: rename project to coding-stats

Every identity surface: package+module (skill_stats -> coding_stats),
CLI, store (.skill-stats/ -> .coding-stats/, artifact file and header),
CI (workflow file, env vars CODING_STATS_*, bot identity, install spec),
default DB/report names, gitignore, docs.

- legacy .skill-stats/ paths stay excluded from analysis beside the new
  store dir (ADR-0003 clause 4 covers both names): pre-rename artifacts
  are committed in git history forever
- old artifacts fail the new header check by design and are regenerated
  by a fresh update (derived data; repository key changes with the URL)
- this repo''s dogfood scaffold swapped: old workflow + artifact deleted,
  fresh coding-stats.yml pinned @main (a sha pin rendered in a PR branch
  dangles after squash-merge)
- historical records (ADRs 0001-0004, decisions-trail.tsv) stay verbatim
- ADR-0005 records the rename

Gates: pytest 167 passed, mypy src strict clean, ruff clean

* feat: migrate verb + repo skill for legacy skill-stats repos (ADR-0005)

coding-stats migrate <repo> automates the per-consumer migration:
verify the legacy artifact first, then seed the new working DB
(reuse-db when the legacy machine DB corresponds — verdicts and
classify session survive; rebuild-artifact otherwise — verdicts
travel inside the artifact bytes; fresh on demand or when the legacy
artifact fails verification), relabel the repository key when the
GitHub rename changed it (the one write register_repository refuses,
made safe by the verified correspondence), regenerate the store via
the normal update flow, and remove the legacy .skill-stats/ dir and
skill-stats.yml workflow only after the new store verifies.

- identity.resolved_identity factored out of register_repository
- recovery._rehydrate_diffs -> public rehydrate_diffs (seed path reuse)
- idempotent: second run is a no-op cleanup; --dry-run prints the plan
- .agents/skills/migrate-skill-stats/SKILL.md documents the driven
  procedure (seed semantics, rename ordering, verification, commit)

Gates: pytest 177 passed (10 new), mypy src strict clean, ruff clean

* review fixes: migrate safety hardening (2 critical, 6 warning findings)

- never lose verdicts on reuse: the legacy DB and artifact are never
  silently merged — coverage horizons are compared via git ancestry and
  the side that classified ahead wins (artifact verdict rows travel
  inside it); divergent coverage refuses instead of guessing
- destination protection: a --db holding foreign content is refused,
  not overwritten (verified against a real stray DB found on this
  machine); --fresh refuses to discard data; unknown-schema files
  refuse loudly
- --fresh now precedes the already-migrated shortcut (full recompute
  is its contract) and refuses existing destinations before opening
  them; explicit --legacy-db pointing at a missing file is an error
- atomic destination swaps everywhere: sqlite backup API into a tmp
  file + os.replace, sidecars cleared only after the swap (mirrors
  recovery.rebuild)
- the one-way format break is now real: parse_header demands the
  coding-stats marker line by default (gather refuses legacy
  artifacts); migration reads them past it deliberately
- already-credit checks the new artifact''s repository key matches this
  repo (a valid-looking foreign artifact is not proof of migration)
- home mirror destination expands ~ at use time; rebuild path creates
  the destination parent dir
- ADR-0005: records the user ruling that DB reuse via verified
  correspondence + relabel supersedes the fresh-DB consequence for the
  renamed repo (cheaper than re-classification, semantically the same
  repository); ADR-0003 clause 4 renamed AND updated to cover both
  exclusion paths; ADR-0003/0004 non-store-path wording restored
  verbatim per the decision-trail claim
- tests: +7 covering the guards; fixture isolation via HOME pinning

Gates: pytest 184 passed, mypy src strict, ruff clean', '6b3dba455c8c3a666ae8111005e2f467402a71ec', 29, 'normal', '', 1574, 405, 1979);
INSERT INTO commits (id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title, message, subject_patch_id, walk_index, kind, diff, added_lines, deleted_lines, churn_lines) VALUES (23, 1, '92da29865a51b83ccdc197265743b44e1fc76fb3', '2d53dbd0aacbbd9b267ed0cf853d6091bd0f014b', 'aa15aa90ca79a1e7c013c0ffddfb87b9b49185c7', '2026-10-22T17:17:29-03:00', 'Daniel Herrmann', 'docs: agent-skills setup (issue tracker, triage labels, model pins) (#17)', 'docs: agent-skills setup (issue tracker, triage labels, model pins) (#17)

Adds the per-repo config the engineering skills read:

- AGENTS.md — new root rules file with the Agent skills block
- docs/agents/issue-tracker.md — GitHub Issues via gh; PRs as a
  request surface: no
- docs/agents/triage-labels.md — five canonical triage roles,
  label strings equal to role names

The four missing state labels (needs-triage, needs-info,
ready-for-agent, ready-for-human) were created on the GitHub repo
out-of-band; wontfix already existed.

Setup per the setup-matt-pocock-skills skill.', '83376e856c65e108f275d34a6a0a658309a6c196', 31, 'normal', '', 81, 0, 81);
CREATE TABLE commit_verdicts (
               commit_id INTEGER PRIMARY KEY,
               verdict TEXT NOT NULL CHECK (verdict IN ('feature', 'fix', 'refactor',
                                                        'revert', 'cleanup', 'merge',
                                                        'unknown')),
               rationale TEXT NOT NULL,
               raw_llm_output TEXT NOT NULL,
               model TEXT NOT NULL,
               run_id INTEGER,
               classified_at TEXT NOT NULL
           );
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (1, 'feature', 'First content in the repo: a coherent new unit of foundational documentation, not a repair or restructuring of prior lines.', '{"k":"new","title":"Foundational skill-stats docs","about":"Adds CONTEXT.md glossary, DESIGN.md, and ADR-0001/0002 establishing the domain language and design decisions for the tool.","why":"First content in the repo: a coherent new unit of foundational documentation, not a repair or restructuring of prior lines."}', 'opencode/glm-5.3-flash', NULL, '2026-10-16 23:59:57');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (2, 'feature', 'First code in the repo: introduces new functionality (package skeleton and DB schema), unrelated to repairing or extending the docs feature.', '{"k":"new","title":"SQLite schema scaffold","about":"Initial uv package scaffold with SQLite schema (commits, features, fix tables), migration mechanism, and project tooling config.","why":"First code in the repo: introduces new functionality (package skeleton and DB schema), unrelated to repairing or extending the docs feature."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:00:00');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (3, 'feature', 'Coherent new functionality — the git import pipeline; not a repair or continuation of the schema scaffold.', '{"k":"new","title":"Target-branch commit walk","about":"Adds gitwalk module: first-parent chronological walk of the Target branch importing commits into the DB with patch-id evidence, resumable and history-rewrite detection, plus tests.","why":"Coherent new functionality — the git import pipeline; not a repair or continuation of the schema scaffold."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:00:05');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (4, 'feature', 'Coherent new functionality — the classification pipeline per ADR-0001; neither repairs earlier lines nor extends an existing feature''s scope.', '{"k":"new","title":"LLM commit classification","about":"Adds classify.py: rolling opencode-CLI session that assigns one verdict per commit with resumable per-commit checkpointing, feature registry context, and reply-parsing/apply logic, plus tests.","why":"Coherent new functionality — the classification pipeline per ADR-0001; neither repairs earlier lines nor extends an existing feature''s scope."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:00:10');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (5, 'feature', 'Coherent new functionality — fix detection and lineage attribution; not a repair or continuation of an existing feature.', '{"k":"new","title":"Fix attribution pipeline","about":"Adds blame.py, targets.py, and lineage.py: blame -w -C --first-parent deletion evidence, fix_touches/fix_targets reduction, and transitive fixes_features lineage closure, plus tests.","why":"Coherent new functionality — fix detection and lineage attribution; not a repair or continuation of an existing feature."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:00:14');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (6, 'feature', 'Coherent new functionality — the metrics/reporting layer plus CLI entry point; no repair intent.', '{"k":"new","title":"Metrics and CLI reporting","about":"Adds metrics.py, report.py, and cli.py: features/year, fixes/feature, churn-per-feature computation with text/JSON/chart reporting and the skill-stats CLI wiring all pipeline stages.","why":"Coherent new functionality — the metrics/reporting layer plus CLI entry point; no repair intent."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:00:19');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (7, 'feature', 'New test infrastructure with a distinct purpose (shared fixture builder); not a repair of earlier lines.', '{"k":"new","title":"Shared git test fixture","about":"Adds tests/conftest.py: a reusable fixture repo builder with a scripted feature/fix/refactor/revert history used by the pipeline test suites.","why":"New test infrastructure with a distinct purpose (shared fixture builder); not a repair of earlier lines."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:00:25');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (8, 'fix', 'Repairs classify.py sessionID extraction: takes the first event line carrying sessionID instead of the last, fixing failed session capture when trailing events (step/idle) lack it; the DESIGN/ADR docs edits ride along.', '{"k":"fix","why":"Repairs classify.py sessionID extraction: takes the first event line carrying sessionID instead of the last, fixing failed session capture when trailing events (step/idle) lack it; the DESIGN/ADR docs edits ride along."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:00:32');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (9, 'feature', 'Coherent new functionality — an HTML report renderer extending reporting output formats; not a repair of earlier lines.', '{"k":"new","title":"Self-contained HTML report","about":"Adds htmlreport.py with inline SVG charts/CSS rendering fix ranking, features-over-time, rolling fixes/feature, verdict mix, and churn, plus a --html CLI flag.","why":"Coherent new functionality — an HTML report renderer extending reporting output formats; not a repair of earlier lines."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:00:36');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (10, 'feature', 'Coherent new design unit (artifact/multi-repo spec) beyond the original foundational docs scope; no repair intent.', '{"k":"new","title":"Artifact multi-repo design docs","about":"Adds ADR-0003 (committed .skill-stats artifact, digest integrity, per-push CI landing, gather) and extends CONTEXT/DESIGN/README with the artifact domain vocabulary.","why":"Coherent new design unit (artifact/multi-repo spec) beyond the original foundational docs scope; no repair intent."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:00:42');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (11, 'feature', 'Coherent new functionality realizing the artifact/multi-repo design; the bundled review fixes are polish within the same new unit, not repairs of shipped lines.', '{"k":"new","title":"Multi-repo schema and exclusion","about":"Implements ADR-0003 wave 1: schema migration step 1 (repositories table, repo-scoped keys, churn columns), store-path exclusion in walk/blame, and prefixed feature ids.","why":"Coherent new functionality realizing the artifact/multi-repo design; the bundled review fixes are polish within the same new unit, not repairs of shipped lines."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:00:46');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (12, 'feature', 'Coherent new functionality (artifact store, update verb, live-lines); not a repair of earlier work.', '{"k":"new","title":"Artifact serializer and update","about":"Implements ADR-0003 wave 2: deterministic SQL-text artifact serializer with digest verification, the update verb, live-lines computation with Sample backfill, and report live-lines/chart additions.","why":"Coherent new functionality (artifact store, update verb, live-lines); not a repair of earlier work."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:00:50');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (13, 'feature', 'Coherent new functionality — recovery flow and CI scaffold; no repair intent toward earlier lines.', '{"k":"new","title":"Partial recompute and CI","about":"Implements ADR-0003 wave 3: partial recompute on digest mismatch (anchor restore from artifact git history, full recompute fallback) plus the ci init/ci land verbs and workflow rendering.","why":"Coherent new functionality — recovery flow and CI scaffold; no repair intent toward earlier lines."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:00:54');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (14, 'feature', 'Coherent new functionality — the gather/org-report stage; internal review fixes belong to the same new unit, not repairs of shipped lines.', '{"k":"new","title":"Gather union DB","about":"Implements ADR-0003 wave 4: gather.py ingests verified artifacts into a Repository-key union DB with idempotent/slug-refusal rules, org report surface, and least-complete sampling.","why":"Coherent new functionality — the gather/org-report stage; internal review fixes belong to the same new unit, not repairs of shipped lines."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:00:58');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (15, 'feature', 'Extends the CI scaffold feature''s scope: git-pinned tool install (correcting the version pin that fails CI), a --pages deploy-pages job, --tool-ref flag, and the ADR amendment.', '{"k":"existing","f":"dahep-coding-stats-12","why":"Extends the CI scaffold feature''s scope: git-pinned tool install (correcting the version pin that fails CI), a --pages deploy-pages job, --tool-ref flag, and the ADR amendment."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:01:07');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (16, 'fix', 'Repairs a crash in walk and livelines: non-UTF-8 text-ish files broke strict UTF-8 decode of git diff/blame output; now decodes with errors=''replace'' and routes undecodable blame to the unattributed bucket.', '{"k":"fix","why":"Repairs a crash in walk and livelines: non-UTF-8 text-ish files broke strict UTF-8 decode of git diff/blame output; now decodes with errors=''replace'' and routes undecodable blame to the unattributed bucket."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:01:14');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (17, 'feature', 'New unit for this repo — adopting/generated CI automation; not a repair or continuation of the generator feature''s code.', '{"k":"new","title":"Repo CI scaffold adoption","about":"Stages the generated .github/workflows/skill-stats.yml so the pipeline runs on this repo itself (first dogfood run, with Pages opt-in).","why":"New unit for this repo — adopting/generated CI automation; not a repair or continuation of the generator feature''s code."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:01:22');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (18, 'fix', 'Corrects live-lines accounting: NUL-containing blobs were surfacing as fake unattributed lines (154k in the pilot) — now skipped to match numstat churn semantics, with a streaming binary probe replacing the decode-error path.', '{"k":"fix","why":"Corrects live-lines accounting: NUL-containing blobs were surfacing as fake unattributed lines (154k in the pilot) — now skipped to match numstat churn semantics, with a streaming binary probe replacing the decode-error path."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:01:29');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (19, 'feature', 'Small README addition documenting the live dogfood report URL — extends the foundational docs scope, no repair intent.', '{"k":"existing","f":"dahep-coding-stats-1","why":"Small README addition documenting the live dogfood report URL — extends the foundational docs scope, no repair intent."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:01:36');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (20, 'fix', 'Repairs identity normalization: SSH-alias hosts leaked into Repository keys and blocked CI''s seeded DB (dogfood run 38157341104) — normalize_clone_url now refuses alias-resolving hosts via ssh -G, with ADR/CONTEXT amendments.', '{"k":"fix","why":"Repairs identity normalization: SSH-alias hosts leaked into Repository keys and blocked CI''s seeded DB (dogfood run 38157341104) — normalize_clone_url now refuses alias-resolving hosts via ssh -G, with ADR/CONTEXT amendments."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:01:41');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (21, 'feature', 'Coherent new decision record (ADR-0004) with a companion CI template change; not a repair of earlier lines.', '{"k":"new","title":"System One backend rejection","about":"Adds ADR-0004 recording the measured rejection of the System One decision-model backend, and passes OPENROUTER_API_KEY through the CI workflow template.","why":"Coherent new decision record (ADR-0004) with a companion CI template change; not a repair of earlier lines."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:01:47');
INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at) VALUES (22, 'feature', 'Introduces new functionality (migrate verb + migration procedure) beyond a mechanical rename; not a repair of earlier lines.', '{"k":"new","title":"coding-stats rename and migrate","about":"Renames the project to coding-stats across all identity surfaces, adds ADR-0005, a migrate verb automating legacy skill-stats repo migration, and a driven migration skill.","why":"Introduces new functionality (migrate verb + migration procedure) beyond a mechanical rename; not a repair of earlier lines."}', 'opencode/glm-5.3-flash', NULL, '2026-10-17 00:01:53');
CREATE TABLE features (
               id TEXT PRIMARY KEY,
               title TEXT NOT NULL,
               about TEXT NOT NULL DEFAULT '',
               title_history_json TEXT NOT NULL DEFAULT '[]',
               created_at TEXT NOT NULL,
               created_run_id INTEGER,
               repo_id INTEGER NOT NULL,
               live_lines INTEGER NOT NULL DEFAULT 0
           );
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-1', 'Foundational skill-stats docs', 'Adds CONTEXT.md glossary, DESIGN.md, and ADR-0001/0002 establishing the domain language and design decisions for the tool.', '[]', '2026-10-02T16:37:03-03:00', NULL, 1, 229);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-10', 'Multi-repo schema and exclusion', 'Implements ADR-0003 wave 1: schema migration step 1 (repositories table, repo-scoped keys, churn columns), store-path exclusion in walk/blame, and prefixed feature ids.', '[]', '2026-10-10T09:10:17-03:00', NULL, 1, 1947);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-11', 'Artifact serializer and update', 'Implements ADR-0003 wave 2: deterministic SQL-text artifact serializer with digest verification, the update verb, live-lines computation with Sample backfill, and report live-lines/chart additions.', '[]', '2026-10-11T01:05:32-03:00', NULL, 1, 1817);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-12', 'Partial recompute and CI', 'Implements ADR-0003 wave 3: partial recompute on digest mismatch (anchor restore from artifact git history, full recompute fallback) plus the ci init/ci land verbs and workflow rendering.', '[]', '2026-10-11T11:29:49-03:00', NULL, 1, 1480);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-13', 'Gather union DB', 'Implements ADR-0003 wave 4: gather.py ingests verified artifacts into a Repository-key union DB with idempotent/slug-refusal rules, org report surface, and least-complete sampling.', '[]', '2026-10-11T13:13:42-03:00', NULL, 1, 1594);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-14', 'Repo CI scaffold adoption', 'Stages the generated .github/workflows/skill-stats.yml so the pipeline runs on this repo itself (first dogfood run, with Pages opt-in).', '[]', '2026-10-11T13:53:28-03:00', NULL, 1, 80);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-15', 'System One backend rejection', 'Adds ADR-0004 recording the measured rejection of the System One decision-model backend, and passes OPENROUTER_API_KEY through the CI workflow template.', '[]', '2026-10-15T12:29:19-03:00', NULL, 1, 61);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-16', 'coding-stats rename and migrate', 'Renames the project to coding-stats across all identity surfaces, adds ADR-0005, a migrate verb automating legacy skill-stats repo migration, and a driven migration skill.', '[]', '2026-10-16T20:57:32-03:00', NULL, 1, 1513);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-2', 'SQLite schema scaffold', 'Initial uv package scaffold with SQLite schema (commits, features, fix tables), migration mechanism, and project tooling config.', '[]', '2026-10-05T10:10:03-03:00', NULL, 1, 1055);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-3', 'Target-branch commit walk', 'Adds gitwalk module: first-parent chronological walk of the Target branch importing commits into the DB with patch-id evidence, resumable and history-rewrite detection, plus tests.', '[]', '2026-10-05T10:10:03-03:00', NULL, 1, 335);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-4', 'LLM commit classification', 'Adds classify.py: rolling opencode-CLI session that assigns one verdict per commit with resumable per-commit checkpointing, feature registry context, and reply-parsing/apply logic, plus tests.', '[]', '2026-10-05T10:10:03-03:00', NULL, 1, 392);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-5', 'Fix attribution pipeline', 'Adds blame.py, targets.py, and lineage.py: blame -w -C --first-parent deletion evidence, fix_touches/fix_targets reduction, and transitive fixes_features lineage closure, plus tests.', '[]', '2026-10-05T10:10:03-03:00', NULL, 1, 361);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-6', 'Metrics and CLI reporting', 'Adds metrics.py, report.py, and cli.py: features/year, fixes/feature, churn-per-feature computation with text/JSON/chart reporting and the skill-stats CLI wiring all pipeline stages.', '[]', '2026-10-05T10:10:03-03:00', NULL, 1, 463);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-7', 'Shared git test fixture', 'Adds tests/conftest.py: a reusable fixture repo builder with a scripted feature/fix/refactor/revert history used by the pipeline test suites.', '[]', '2026-10-05T10:10:07-03:00', NULL, 1, 78);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-8', 'Self-contained HTML report', 'Adds htmlreport.py with inline SVG charts/CSS rendering fix ranking, features-over-time, rolling fixes/feature, verdict mix, and churn, plus a --html CLI flag.', '[]', '2026-10-08T14:03:03-03:00', NULL, 1, 650);
INSERT INTO features (id, title, about, title_history_json, created_at, created_run_id, repo_id, live_lines) VALUES ('dahep-coding-stats-9', 'Artifact multi-repo design docs', 'Adds ADR-0003 (committed .skill-stats artifact, digest integrity, per-push CI landing, gather) and extends CONTEXT/DESIGN/README with the artifact domain vocabulary.', '[]', '2026-10-10T00:16:26-03:00', NULL, 1, 329);
CREATE TABLE commits_features (
               commit_id INTEGER NOT NULL,
               feature_id TEXT NOT NULL,
               role TEXT NOT NULL CHECK (role IN ('defines', 'touches')),
               PRIMARY KEY (commit_id, feature_id),
               UNIQUE (feature_id, commit_id)
           );
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (1, 'dahep-coding-stats-1', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (2, 'dahep-coding-stats-2', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (3, 'dahep-coding-stats-3', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (4, 'dahep-coding-stats-4', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (5, 'dahep-coding-stats-5', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (6, 'dahep-coding-stats-6', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (7, 'dahep-coding-stats-7', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (9, 'dahep-coding-stats-8', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (10, 'dahep-coding-stats-9', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (11, 'dahep-coding-stats-10', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (12, 'dahep-coding-stats-11', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (13, 'dahep-coding-stats-12', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (14, 'dahep-coding-stats-13', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (15, 'dahep-coding-stats-12', 'touches');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (17, 'dahep-coding-stats-14', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (19, 'dahep-coding-stats-1', 'touches');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (21, 'dahep-coding-stats-15', 'defines');
INSERT INTO commits_features (commit_id, feature_id, role) VALUES (22, 'dahep-coding-stats-16', 'defines');
CREATE TABLE fix_touches (
               fix_commit_id INTEGER NOT NULL,
               source_commit_id INTEGER NOT NULL,
               hit_lines INTEGER NOT NULL,
               PRIMARY KEY (fix_commit_id, source_commit_id)
           );
INSERT INTO fix_touches (fix_commit_id, source_commit_id, hit_lines) VALUES (8, 1, 7);
INSERT INTO fix_touches (fix_commit_id, source_commit_id, hit_lines) VALUES (8, 4, 1);
INSERT INTO fix_touches (fix_commit_id, source_commit_id, hit_lines) VALUES (16, 3, 2);
INSERT INTO fix_touches (fix_commit_id, source_commit_id, hit_lines) VALUES (16, 12, 5);
INSERT INTO fix_touches (fix_commit_id, source_commit_id, hit_lines) VALUES (18, 12, 12);
INSERT INTO fix_touches (fix_commit_id, source_commit_id, hit_lines) VALUES (18, 16, 15);
INSERT INTO fix_touches (fix_commit_id, source_commit_id, hit_lines) VALUES (20, 10, 1);
INSERT INTO fix_touches (fix_commit_id, source_commit_id, hit_lines) VALUES (20, 11, 3);
CREATE TABLE fix_targets (
               fix_commit_id INTEGER NOT NULL,
               target_commit_id INTEGER NOT NULL,
               PRIMARY KEY (fix_commit_id, target_commit_id)
           );
INSERT INTO fix_targets (fix_commit_id, target_commit_id) VALUES (8, 1);
INSERT INTO fix_targets (fix_commit_id, target_commit_id) VALUES (8, 4);
INSERT INTO fix_targets (fix_commit_id, target_commit_id) VALUES (16, 3);
INSERT INTO fix_targets (fix_commit_id, target_commit_id) VALUES (16, 12);
INSERT INTO fix_targets (fix_commit_id, target_commit_id) VALUES (18, 12);
INSERT INTO fix_targets (fix_commit_id, target_commit_id) VALUES (18, 16);
INSERT INTO fix_targets (fix_commit_id, target_commit_id) VALUES (20, 10);
INSERT INTO fix_targets (fix_commit_id, target_commit_id) VALUES (20, 11);
CREATE TABLE fixes_features (
               fix_commit_id INTEGER NOT NULL,
               feature_id TEXT NOT NULL,
               via_fix_commit_id INTEGER,
               PRIMARY KEY (fix_commit_id, feature_id),
               UNIQUE (fix_commit_id, feature_id, via_fix_commit_id)
           );
INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id) VALUES (8, 'dahep-coding-stats-1', NULL);
INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id) VALUES (8, 'dahep-coding-stats-4', NULL);
INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id) VALUES (16, 'dahep-coding-stats-11', NULL);
INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id) VALUES (16, 'dahep-coding-stats-3', NULL);
INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id) VALUES (18, 'dahep-coding-stats-11', NULL);
INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id) VALUES (18, 'dahep-coding-stats-3', 16);
INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id) VALUES (20, 'dahep-coding-stats-10', NULL);
INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id) VALUES (20, 'dahep-coding-stats-9', NULL);
CREATE TABLE feature_line_samples (
               feature_id TEXT NOT NULL,
               at_commit_sha TEXT NOT NULL,
               live_lines INTEGER NOT NULL,
               PRIMARY KEY (feature_id, at_commit_sha)
           );
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', '1071f03aa2742cc7fb662e049eb76ec3dbd024cd', 185);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', '1bbe64a60d3a3ec30fc891a01439c9025edb9b2e', 231);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 233);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', '5286dc4f71aa54bf355d519045e5b86d79d9141f', 231);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', '7401695136ca5cb12b38de7c84f9d628c276fe5f', 185);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', '80802547b5142cd82882ad3cca5750a7ce775693', 227);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', '86645df2aee5c8f5fe9a4d0e620e3dc4c69cc3fb', 185);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', '870a7439351a086b07a40978ee855c29e50b9930', 227);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', '8b9154ccec75712fe9f67a1fee7ade207519b515', 185);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', '8e02366903207fa1c4eafbed7e646e973d3df525', 227);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', '92da29865a51b83ccdc197265743b44e1fc76fb3', 229);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', '96bba5112039499782c5c92cfc68a666e2dca5c9', 185);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 229);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', 227);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', 227);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', 'ae43736da29ae81139769ab1a8c6335e9d4a0065', 227);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', 'b04223b4dfe071528bc2cce640b5371b3652d965', 185);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 233);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 233);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 227);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', 'c9409dc81199a73fca430c568055340bfadadda1', 227);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', 'dc519177d9b1e47cc39a386d49d4fc2b9670b918', 185);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-1', 'fb62a1504040479027cb455e4c30f23760e7daff', 227);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-10', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 2019);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-10', '80802547b5142cd82882ad3cca5750a7ce775693', 1915);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-10', '870a7439351a086b07a40978ee855c29e50b9930', 1915);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-10', '8e02366903207fa1c4eafbed7e646e973d3df525', 1889);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-10', '92da29865a51b83ccdc197265743b44e1fc76fb3', 1947);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-10', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 1947);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-10', 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', 1915);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-10', 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', 1915);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-10', 'ae43736da29ae81139769ab1a8c6335e9d4a0065', 1911);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-10', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 2019);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-10', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 1915);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-10', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 1915);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-10', 'fb62a1504040479027cb455e4c30f23760e7daff', 1915);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-11', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 1862);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-11', '80802547b5142cd82882ad3cca5750a7ce775693', 1862);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-11', '870a7439351a086b07a40978ee855c29e50b9930', 1751);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-11', '92da29865a51b83ccdc197265743b44e1fc76fb3', 1817);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-11', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 1817);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-11', 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', 1783);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-11', 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', 1805);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-11', 'ae43736da29ae81139769ab1a8c6335e9d4a0065', 1773);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-11', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 1862);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-11', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 1862);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-11', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 1751);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-11', 'fb62a1504040479027cb455e4c30f23760e7daff', 1805);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-12', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 1592);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-12', '80802547b5142cd82882ad3cca5750a7ce775693', 1592);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-12', '870a7439351a086b07a40978ee855c29e50b9930', 1592);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-12', '92da29865a51b83ccdc197265743b44e1fc76fb3', 1480);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-12', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 1480);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-12', 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', 1427);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-12', 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', 1592);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-12', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 1589);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-12', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 1592);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-12', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 1421);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-12', 'fb62a1504040479027cb455e4c30f23760e7daff', 1592);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-13', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 1642);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-13', '80802547b5142cd82882ad3cca5750a7ce775693', 1642);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-13', '870a7439351a086b07a40978ee855c29e50b9930', 1642);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-13', '92da29865a51b83ccdc197265743b44e1fc76fb3', 1594);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-13', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 1594);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-13', 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', 1642);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-13', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 1642);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-13', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 1642);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-13', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 1642);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-13', 'fb62a1504040479027cb455e4c30f23760e7daff', 1642);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-14', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 98);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-14', '80802547b5142cd82882ad3cca5750a7ce775693', 98);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-14', '92da29865a51b83ccdc197265743b44e1fc76fb3', 80);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-14', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 80);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-14', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 98);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-14', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 98);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-14', 'fb62a1504040479027cb455e4c30f23760e7daff', 98);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-15', '92da29865a51b83ccdc197265743b44e1fc76fb3', 61);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-15', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 61);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-15', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 57);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-16', '92da29865a51b83ccdc197265743b44e1fc76fb3', 1513);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-16', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 1513);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', '1071f03aa2742cc7fb662e049eb76ec3dbd024cd', 1030);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', '1bbe64a60d3a3ec30fc891a01439c9025edb9b2e', 1030);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 1063);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', '5286dc4f71aa54bf355d519045e5b86d79d9141f', 1030);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', '7401695136ca5cb12b38de7c84f9d628c276fe5f', 1030);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', '80802547b5142cd82882ad3cca5750a7ce775693', 1063);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', '86645df2aee5c8f5fe9a4d0e620e3dc4c69cc3fb', 1030);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', '870a7439351a086b07a40978ee855c29e50b9930', 1063);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', '8b9154ccec75712fe9f67a1fee7ade207519b515', 1030);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', '8e02366903207fa1c4eafbed7e646e973d3df525', 1046);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', '92da29865a51b83ccdc197265743b44e1fc76fb3', 1055);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', '96bba5112039499782c5c92cfc68a666e2dca5c9', 1030);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 1055);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', 1063);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', 1063);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', 'ae43736da29ae81139769ab1a8c6335e9d4a0065', 1064);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', 'b04223b4dfe071528bc2cce640b5371b3652d965', 1030);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 1063);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 1063);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 1063);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', 'c9409dc81199a73fca430c568055340bfadadda1', 1030);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-2', 'fb62a1504040479027cb455e4c30f23760e7daff', 1063);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', '1071f03aa2742cc7fb662e049eb76ec3dbd024cd', 243);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', '1bbe64a60d3a3ec30fc891a01439c9025edb9b2e', 243);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 335);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', '5286dc4f71aa54bf355d519045e5b86d79d9141f', 243);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', '7401695136ca5cb12b38de7c84f9d628c276fe5f', 243);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', '80802547b5142cd82882ad3cca5750a7ce775693', 335);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', '86645df2aee5c8f5fe9a4d0e620e3dc4c69cc3fb', 243);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', '870a7439351a086b07a40978ee855c29e50b9930', 217);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', '8b9154ccec75712fe9f67a1fee7ade207519b515', 243);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', '8e02366903207fa1c4eafbed7e646e973d3df525', 217);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', '92da29865a51b83ccdc197265743b44e1fc76fb3', 335);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 335);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', 217);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', 269);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', 'ae43736da29ae81139769ab1a8c6335e9d4a0065', 217);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', 'b04223b4dfe071528bc2cce640b5371b3652d965', 243);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 335);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 335);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 217);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', 'c9409dc81199a73fca430c568055340bfadadda1', 243);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-3', 'fb62a1504040479027cb455e4c30f23760e7daff', 269);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', '1071f03aa2742cc7fb662e049eb76ec3dbd024cd', 402);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', '1bbe64a60d3a3ec30fc891a01439c9025edb9b2e', 454);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 394);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', '5286dc4f71aa54bf355d519045e5b86d79d9141f', 454);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', '7401695136ca5cb12b38de7c84f9d628c276fe5f', 402);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', '80802547b5142cd82882ad3cca5750a7ce775693', 394);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', '86645df2aee5c8f5fe9a4d0e620e3dc4c69cc3fb', 402);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', '870a7439351a086b07a40978ee855c29e50b9930', 394);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', '8b9154ccec75712fe9f67a1fee7ade207519b515', 402);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', '8e02366903207fa1c4eafbed7e646e973d3df525', 394);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', '92da29865a51b83ccdc197265743b44e1fc76fb3', 392);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 392);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', 394);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', 394);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', 'ae43736da29ae81139769ab1a8c6335e9d4a0065', 394);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 394);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 394);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 394);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', 'c9409dc81199a73fca430c568055340bfadadda1', 454);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-4', 'fb62a1504040479027cb455e4c30f23760e7daff', 394);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', '1071f03aa2742cc7fb662e049eb76ec3dbd024cd', 384);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', '1bbe64a60d3a3ec30fc891a01439c9025edb9b2e', 384);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 365);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', '5286dc4f71aa54bf355d519045e5b86d79d9141f', 384);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', '7401695136ca5cb12b38de7c84f9d628c276fe5f', 384);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', '80802547b5142cd82882ad3cca5750a7ce775693', 365);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', '870a7439351a086b07a40978ee855c29e50b9930', 365);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', '8b9154ccec75712fe9f67a1fee7ade207519b515', 384);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', '8e02366903207fa1c4eafbed7e646e973d3df525', 365);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', '92da29865a51b83ccdc197265743b44e1fc76fb3', 361);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 361);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', 365);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', 365);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', 'ae43736da29ae81139769ab1a8c6335e9d4a0065', 365);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 365);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 365);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 365);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', 'c9409dc81199a73fca430c568055340bfadadda1', 384);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-5', 'fb62a1504040479027cb455e4c30f23760e7daff', 365);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', '1071f03aa2742cc7fb662e049eb76ec3dbd024cd', 534);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', '1bbe64a60d3a3ec30fc891a01439c9025edb9b2e', 534);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 489);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', '5286dc4f71aa54bf355d519045e5b86d79d9141f', 534);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', '7401695136ca5cb12b38de7c84f9d628c276fe5f', 534);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', '80802547b5142cd82882ad3cca5750a7ce775693', 489);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', '870a7439351a086b07a40978ee855c29e50b9930', 489);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', '8e02366903207fa1c4eafbed7e646e973d3df525', 481);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', '92da29865a51b83ccdc197265743b44e1fc76fb3', 463);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 463);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', 490);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', 489);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', 'ae43736da29ae81139769ab1a8c6335e9d4a0065', 489);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 489);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 489);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 489);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', 'c9409dc81199a73fca430c568055340bfadadda1', 534);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-6', 'fb62a1504040479027cb455e4c30f23760e7daff', 489);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', '1071f03aa2742cc7fb662e049eb76ec3dbd024cd', 90);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', '1bbe64a60d3a3ec30fc891a01439c9025edb9b2e', 90);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 79);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', '5286dc4f71aa54bf355d519045e5b86d79d9141f', 90);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', '80802547b5142cd82882ad3cca5750a7ce775693', 79);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', '870a7439351a086b07a40978ee855c29e50b9930', 79);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', '8e02366903207fa1c4eafbed7e646e973d3df525', 79);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', '92da29865a51b83ccdc197265743b44e1fc76fb3', 78);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 78);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', 79);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', 79);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', 'ae43736da29ae81139769ab1a8c6335e9d4a0065', 79);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 79);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 79);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 79);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', 'c9409dc81199a73fca430c568055340bfadadda1', 90);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-7', 'fb62a1504040479027cb455e4c30f23760e7daff', 79);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', '1bbe64a60d3a3ec30fc891a01439c9025edb9b2e', 701);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 656);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', '80802547b5142cd82882ad3cca5750a7ce775693', 656);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', '870a7439351a086b07a40978ee855c29e50b9930', 656);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', '8e02366903207fa1c4eafbed7e646e973d3df525', 692);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', '92da29865a51b83ccdc197265743b44e1fc76fb3', 650);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 650);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', 667);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', 656);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', 'ae43736da29ae81139769ab1a8c6335e9d4a0065', 665);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 656);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 656);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 656);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', 'c9409dc81199a73fca430c568055340bfadadda1', 701);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-8', 'fb62a1504040479027cb455e4c30f23760e7daff', 656);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', '2c2d504fdd9054ffd72bed97c1157826e90a3797', 335);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', '80802547b5142cd82882ad3cca5750a7ce775693', 237);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', '870a7439351a086b07a40978ee855c29e50b9930', 237);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', '8e02366903207fa1c4eafbed7e646e973d3df525', 244);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', '92da29865a51b83ccdc197265743b44e1fc76fb3', 329);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', 'a04a9a759ee98f11cbbc2880980473add5e3a49a', 329);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', 'a7357b86f9adfdd5cfbe2d940d65c542b24528da', 241);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', 'a9a2149664d465d8d363e89d0a52a1fcb5c47fc6', 237);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', 'ae43736da29ae81139769ab1a8c6335e9d4a0065', 244);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', 'b7ad4f830ee66a4f8c321d300b729c3218eb115e', 335);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', 'b962f225b1bf29df253169cecbb5fd6c1ab1d4a8', 237);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', 'c7e966a38cb3eae5ed83e29e0e789ddf5cbfaad9', 237);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', 'c9409dc81199a73fca430c568055340bfadadda1', 244);
INSERT INTO feature_line_samples (feature_id, at_commit_sha, live_lines) VALUES ('dahep-coding-stats-9', 'fb62a1504040479027cb455e4c30f23760e7daff', 237);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO settings (key, value) VALUES ('backfill_state', '{"status": "done", "max_classified_walk_index": 29}');
INSERT INTO settings (key, value) VALUES ('branch', '"main"');
INSERT INTO settings (key, value) VALUES ('unattributed_lines', '0');
