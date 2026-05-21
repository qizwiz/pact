# Prompt: Heal Self-Improvement

You are a prompt engineer reviewing the performance of a patch synthesis prompt.
You will see: the heal prompt that was used, a sample of its outputs (accepted and
rejected patches), and the rejection reasons from the verifier.

Your job is to identify systematic weaknesses in the prompt and rewrite it to
produce better patches.

## The heal prompt that was used

{{prompt_text}}

## Sample outputs (accepted patches)

```json
{{accepted_samples}}
```

## Sample outputs (rejected patches, with verifier feedback)

```json
{{rejected_samples}}
```

## Rejection reasons (aggregated)

{{rejection_reasons}}

---

## Evaluation rubric

Score each dimension 0–10:

**PATCH_APPLICABILITY** (target: 9+)
What fraction of synthesized `original` blocks appear verbatim in the visible
source context? Score 10 if > 95% of patches are applicable. Score < 5 if
non-JSON responses or inapplicable blocks are common.
A prompt that produces beautiful diagnoses but inapplicable patches scores 0 here.

**TRUNCATION_HANDLING** (target: 8+)
Does the prompt adequately instruct the model to detect and reject cases where
the target code is outside the visible window? Score 10 if the PART 0 audit
consistently stops synthesis when the block isn't visible. Score < 5 if models
frequently proceed to synthesize when context is truncated.

**DIAGNOSIS_CORRECTNESS** (target: 8+)
Do the diagnoses identify structural root causes (not symptoms)? Score < 6 if
diagnoses consistently describe the evidence line rather than why that structure
violates the invariant's guarantee. Look at rejected patches — were they rejected
because the diagnosis was wrong?

**FIX_CLASS_PRECISION** (target: 7+)
Does the prompt give clear enough guidance to pick the right fix class? Score < 5
if fix classes are misapplied (e.g., `add_signal` used when `fix_encoding` is
correct). Look for mismatch between invariant type and fix class chosen.

**COUNTEREXAMPLE_QUALITY** (target: 7+)
Are counterexamples concrete and checkable (actual input values, not "any case
where X is None")? Score < 5 if counterexamples are abstract descriptions rather
than runnable inputs.

**JSON_COMPLIANCE** (target: 9+)
What fraction of responses return valid JSON? Score 10 if > 95% are valid JSON.
Score 0 if non-JSON responses ("Let me analyze...") are common. This is a prompt
instruction failure, not a model failure — if it's low, the prompt must be clearer.

---

## Your output

1. Score each dimension.
2. For each dimension < 8: identify the systemic prompt weakness causing it.
3. Rewrite the prompt. Rules:
   - Keep the same 4-part structure (PART 0 truncation audit, PART 1 diagnose,
     PART 2 synthesize, PART 3 justify)
   - Strengthen any part where systematic failures cluster
   - Add explicit examples of GOOD vs BAD patches for dimensions that are weak
   - Add explicit prohibitions: "Do not synthesize if PART 0 returned an error"
   - Keep the same output JSON schema (truncation_audit, diagnosis, patch, justification)
   - Do not lengthen the prompt unless the length adds precision

Return JSON only:
{
  "scores": {
    "patch_applicability": {"score": 0, "justification": "..."},
    "truncation_handling": {"score": 0, "justification": "..."},
    "diagnosis_correctness": {"score": 0, "justification": "..."},
    "fix_class_precision": {"score": 0, "justification": "..."},
    "counterexample_quality": {"score": 0, "justification": "..."},
    "json_compliance": {"score": 0, "justification": "..."}
  },
  "weaknesses": [
    {
      "dimension": "...",
      "systemic_cause": "what in the prompt causes this failure",
      "example_failure": "quote from rejected_samples that illustrates it"
    }
  ],
  "overall_score": 0.0,
  "improved_prompt": "full text of the rewritten prompt — complete, ready to use"
}
