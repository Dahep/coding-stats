"""The recorded Commit verdict vocabulary: ONE owner for the verdict strings.

A Commit verdict (CONTEXT.md) is the single class a commit is classified
into. The seven verdicts recorded today are the pre-ADR-0008 recorded shape
(``enhancement`` and ``chore`` arrive with spec #49 — until then the schema
CHECK, the classifier, the derived stages and the reports all speak exactly
this set, and this module is the only place any of their names is written).

Everything downstream derives from here:

* the ``commit_verdicts.verdict`` CHECK constraint — :func:`check_constraint`
  is fed through the tablecatalog, so the working schema's DDL (schema step 0)
  and the Artifact's DDL generate that ONE copy (PR 4's rule: one declaration
  owns every table's shape);
* the classifier's verdict writes and the derived stages' fix filters
  (classify, targets, lineage);
* the metrics/reports' verdict-donut slices (REPORT_ORDER — every
  schema-accepted verdict is a slice, so the donut's slices always sum to its
  center total by construction).

LEAF: zero imports from the coding-stats package. tablecatalog (itself a
leaf above this one), classify, targets, lineage, metrics and the reports
consume it; it imports nothing back.
"""

# The recorded verdicts, exactly the set the schema CHECK accepts today
# (the pre-ADR-0008 recorded shape; spec #49 adds ``enhancement`` and
# ``chore``). Keep taxonomy order: the work verdicts, then the system
# verdicts.
FEATURE = "feature"
FIX = "fix"
REFACTOR = "refactor"
REVERT = "revert"
CLEANUP = "cleanup"
MERGE = "merge"
UNKNOWN = "unknown"

RECORDED: tuple[str, ...] = (FEATURE, FIX, REFACTOR, REVERT, CLEANUP, MERGE, UNKNOWN)
"""The recorded verdict set — what ``commit_verdicts.verdict`` accepts."""

REPORT_ORDER: tuple[str, ...] = (FEATURE, FIX, REFACTOR, REVERT, CLEANUP, MERGE, UNKNOWN)
"""The canonical display order for verdict slices and sections. Today it
lists every recorded verdict in RECORDED's order; the two tuples are
deliberately separate names — the coupling test (test_verdicts.py) fails the
moment the schema accepts a verdict the reports do not show, or vice versa."""


def is_fix(verdict: str) -> bool:
    """Whether ``verdict`` is the Fix verdict (either subtype; the recorded
    shape has no subtypes yet — #49 records ``repair``/``revert``)."""
    return verdict == FIX


def check_constraint() -> str:
    """The CHECK-constraint SQL over the recorded verdicts — the ONE copy
    the tablecatalog embeds in ``commit_verdicts.verdict``'s declaration, so
    the working schema's and the Artifact's DDL cannot drift apart."""
    values = ", ".join(repr(v) for v in RECORDED)
    return f"CHECK (verdict IN ({values}))"
