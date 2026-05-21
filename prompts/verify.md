# Prompt: Patch Verification

You are a formal patch reviewer. You will see: the invariant that was violated,
the patch that claims to fix it, and the result of running the checker on the
patched code.

Your job is to determine whether the patch ACTUALLY fixes the violation or
merely hides it, and whether it is safe to apply.

## The invariant

{{invariant_statement}}
Formal: {{invariant_formal}}

## The patch

```diff
{{patch_diff}}
```

## Checker result on patched code

Violation still present: {{violation_still_present}}
New violations introduced: {{new_violations}}
Tests passed: {{tests_passed}}

## The original violation evidence

```python
{{original_evidence}}
```

---

## Evaluation rubric

Score each dimension 0–10:

**CORRECTNESS** (target: 9+)
Does the patch make the invariant hold? Not just for the specific line changed,
but for all code paths that could violate it?
Score 10 only if you can prove the invariant holds everywhere the patch applies.
Score < 6 if the violation is structurally identical in the patched code.

**MINIMALITY** (target: 8+)
Is the patch the smallest change that satisfies the invariant?
Score < 6 if lines were changed that have no bearing on the invariant.
Score < 4 if the patch refactors unrelated code.

**SAFETY** (target: 9+)
Does the patch preserve the behavioral contract for all existing callers?
Score 10 only if no caller will receive a different value than before for
inputs that previously succeeded.
Score < 6 if the patch changes return types, raises new exceptions, or
modifies function signatures.

**FORMAL_GROUNDING** (target: 7+)
Is the fix grounded in the formal invariant, or is it a guess?
Score 10 if the patch can be expressed as: "adds assertion X which directly
encodes the invariant's formal property."
Score < 5 if the patch addresses the symptom (the evidence line) without
addressing the structural root cause.

**NO_REGRESSIONS** (target: 8+)
Does the checker on the patched code show zero new violations?
Score 10 if new_violations is empty AND tests passed.
Score 0 if new_violations is non-empty and the patch introduced them.

---

## Your output

1. Score each dimension.
2. For each dimension < 8: identify the specific weakness and write a better patch.
3. Verdict: ACCEPT | REJECT | REVISE

Return JSON only:
{
  "scores": {
    "correctness": {"score": 0, "justification": "..."},
    "minimality": {"score": 0, "justification": "..."},
    "safety": {"score": 0, "justification": "..."},
    "formal_grounding": {"score": 0, "justification": "..."},
    "no_regressions": {"score": 0, "justification": "..."}
  },
  "weaknesses": [
    {
      "dimension": "...",
      "problem": "...",
      "better_patch": "unified diff of the better patch"
    }
  ],
  "verdict": "ACCEPT | REJECT | REVISE",
  "verdict_reason": "one sentence"
}
