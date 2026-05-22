"""
Seed the spec_learner corpus with known spec gaps.

These records document violation patterns where PactLoop.tla's abstraction misses
the bug — the spec passes TLC but the pattern still causes real failures. Each
record represents a genuine gap between what PactLoop.tla models and what the
checker detects in the wild.

Run: python -m pact.scripts.seed_corpus
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from pact.spec_learner import SpecGapRecord, load_corpus, save  # noqa: E402

_TLA_PATH = Path(__file__).parent.parent / "docs" / "tla" / "PactLoop.tla"


def _tla_text() -> str:
    if _TLA_PATH.exists():
        return _TLA_PATH.read_text(encoding="utf-8")[:4000]
    return "(PactLoop.tla not found)"


def _already_seeded(gap_name: str) -> bool:
    return any(r.gap_name == gap_name for r in load_corpus())


RECORDS: list[SpecGapRecord] = [
    SpecGapRecord(
        timestamp=time.time(),
        bug_description=(
            "optional_dereference: accessing `.field` on a Django ForeignKey or "
            "Optional return value without a None check. 699 instances in future-agi. "
            "A partial fix (guarding one branch but not another) still leaves the "
            "other branch crashable — the CEGIS loop marks it ACCEPT because the "
            "specific violation line is guarded, not because all paths are safe."
        ),
        bug_file="futureagi/tracer/models.py",
        bug_line=0,
        bug_manifestation=(
            "AttributeError: 'NoneType' object has no attribute 'id' at runtime. "
            "pact CEGIS loop accepted a patch that guarded the direct dereference "
            "but left an indirect caller unguarded. The checker reports 0 violations "
            "at the patched line, so the loop terminates CONVERGED, but other "
            "callers still crash."
        ),
        bug_fix=(
            "Guard ALL dereferences on the nullable path, not just the evidence line. "
            "Fix: check `if obj is not None:` before EVERY field access on the object, "
            "or use `getattr(obj, 'field', default)` at every call site."
        ),
        tla_spec_path=str(_TLA_PATH),
        tla_spec_text=_tla_text(),
        tlc_output="(TLC was not run — spec passed as violations treated as a flat set)",
        gap_name="optional_dereference",
        variable="violations",
        abstraction_level_before=(
            "PactLoop.tla models violations as a flat set of (file, line) pairs. "
            "Healing a violation = removing it from the set. The spec has no concept "
            "of 'all dereferences on a nullable path' — it only knows one violation at a time."
        ),
        abstraction_level_after=(
            "The spec would need to model violations as dependency groups: a set of "
            "violation clusters where all members of a cluster must be fixed together. "
            "Fixing one member without fixing all members of the cluster should NOT "
            "reduce the cluster's count."
        ),
        gap_description=(
            "PactLoop.tla's FitnessMonotone invariant computes fitness from total "
            "violation count. A partial fix that reduces count by 1 (guarding the "
            "specific evidence line) is indistinguishable from a complete fix. The spec "
            "cannot detect that the cluster still has other crashable paths."
        ),
        execution_trace=[
            "measure: violations = {(tracer/models.py, 142), (tracer/views.py, 89), (tracer/views.py, 103)}",
            "heal: patch guards tracer/models.py:142",
            "check: violations = {(tracer/views.py, 89), (tracer/views.py, 103)}  -- count reduced by 1",
            "fitness improves: spec is satisfied, loop continues",
            "heal: patch guards tracer/views.py:89",
            "check: violations = {(tracer/views.py, 103)}",
            "heal: patch guards tracer/views.py:103",
            "CONVERGED -- but all three share the same nullable object; a combined guard was needed",
        ],
        invariant_that_should_have_failed=(
            "\\A cluster \\in ViolationClusters: "
            "  (\\E v \\in cluster: v \\in violations) => cluster \\subseteq violations"
        ),
        tla_refinement=(
            "Add ViolationClusters: a set of sets, where each inner set groups violations "
            "that share a nullable root. Add ClusterCount = |{c \\in ViolationClusters : "
            "c \\cap violations /= {}}|. Fitness must decrease ClusterCount, not just |violations|."
        ),
        gap_confidence=0.85,
        verification_claim=(
            "With ViolationClusters, the spec would detect that fixing 1-of-3 members "
            "of an optional_dereference cluster does not reduce ClusterCount, and would "
            "reject the partial-fix CEGIS iteration."
        ),
        verdict="MISSES_BUG",
        bounded=True,
        estimated_states="O(2^|ViolationClusters|) — bounded by cluster count",
        refinement_quality="ADDRESSES_GAP",
        validate_confidence=0.75,
        tlc_actual_result="",
        tlc_matches_prediction=None,
    ),
    SpecGapRecord(
        timestamp=time.time(),
        bug_description=(
            "save_without_update_fields: Django ORM `.save()` called without "
            "`update_fields=` on a model with many fields. 820 instances in future-agi. "
            "In concurrent environments, this causes last-write-wins races: two workers "
            "each load model state, update different fields, and each `.save()` overwrites "
            "the other's changes. pact's CEGIS loop can add `update_fields` but cannot "
            "verify that the specified fields are correct — TLA+ has no Django ORM model."
        ),
        bug_file="futureagi/agentic_eval/models.py",
        bug_line=0,
        bug_manifestation=(
            "Silent data corruption: EvalResult.status is reset to 'pending' after a "
            "worker saves score fields, because a second concurrent worker's `.save()` "
            "had loaded the old status before the first worker updated it. No exception — "
            "the data is wrong but the code appears to succeed."
        ),
        bug_fix=(
            "Add `update_fields=['field_that_changed']` to every `.save()` call so that "
            "only the intentionally modified field is written. Use "
            "`Model.objects.filter(pk=obj.pk).update(field=value)` for atomic updates."
        ),
        tla_spec_path=str(_TLA_PATH),
        tla_spec_text=_tla_text(),
        tlc_output="(TLC was not run — spec has no concept of Django ORM field granularity)",
        gap_name="save_without_update_fields",
        variable="violations",
        abstraction_level_before=(
            "PactLoop.tla models code correctness as violation presence/absence. "
            "It has no state for 'which fields are included in a save()' or "
            "'are two concurrent saves touching disjoint fields?'. "
            "The spec cannot distinguish a safe broad save from a racy broad save."
        ),
        abstraction_level_after=(
            "The spec would need: SaveFieldSets: a function from (file, line) → set of fields. "
            "A save is safe if its field set is disjoint from concurrent saves on the same model. "
            "This is a data-race model, not expressible in the current flat violation set."
        ),
        gap_description=(
            "PactLoop.tla's abstraction treats all save_without_update_fields violations "
            "as equivalent once patched. It cannot distinguish a correct `update_fields=['status']` "
            "patch from an incorrect `update_fields=['id']` patch. The oracle (test suite) "
            "catches this only if there's a concurrent test — most test suites are sequential."
        ),
        execution_trace=[
            "measure: violations = {(agentic_eval/models.py, 88)}",
            "heal: patch adds update_fields=['status', 'score', 'created_at']  -- too broad",
            "oracle: sequential test suite passes -- no concurrency test",
            "CONVERGED -- but update_fields includes created_at which shouldn't be written by this path",
        ],
        invariant_that_should_have_failed=(
            "\\A save \\in DjangoSaves: "
            "  save.update_fields /= {} => "
            "    \\A f \\in save.update_fields: f \\in save.intentionally_modified_fields"
        ),
        tla_refinement=(
            "Add IntentionallyModifiedFields: a function from (file, line) → field_set. "
            "Add SaveFieldSetCorrect = \\A s \\in DjangoSaves WITH update_fields: "
            "  s.update_fields \\subseteq IntentionallyModifiedFields[s.location]. "
            "Requires field-level tracking not present in the current spec."
        ),
        gap_confidence=0.80,
        verification_claim=(
            "With IntentionallyModifiedFields tracking, TLC could flag saves where "
            "update_fields includes fields not modified by the current code path. "
            "This requires interprocedural data-flow analysis to populate the model."
        ),
        verdict="MISSES_BUG",
        bounded=True,
        estimated_states="O(2^|ModelFields|) per save site — manageable with field abstraction",
        refinement_quality="IDENTIFIES_DEEPER_GAP",
        validate_confidence=0.70,
        tlc_actual_result="",
        tlc_matches_prediction=None,
    ),
]


def main() -> None:
    seeded = 0
    for record in RECORDS:
        if _already_seeded(record.gap_name):
            print(f"[seed] already in corpus: {record.gap_name}")
            continue
        save(record)
        print(f"[seed] ✓ added: {record.gap_name} (verdict={record.verdict})")
        seeded += 1

    corpus = load_corpus()
    print(f"\n[seed] corpus now has {len(corpus)} records")
    verdicts = {}
    for r in corpus:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
    for v, c in sorted(verdicts.items()):
        print(f"  {v}: {c}")

    misses = sum(1 for r in corpus if r.verdict == "MISSES_BUG")
    if misses >= 2:
        print(
            f"\n[seed] ✓ {misses} MISSES_BUG records — spec_learner.improve() will now activate"
        )
    else:
        print(f"\n[seed] {misses} MISSES_BUG records — need 2+ to activate improve()")


if __name__ == "__main__":
    main()
