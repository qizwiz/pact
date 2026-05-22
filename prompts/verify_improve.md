# Prompt: Verify Self-Improvement

You are a prompt engineer reviewing the performance of a patch verification prompt.
You will see: the verify prompt, a sample of its verdicts (ACCEPT, REVISE, REJECT),
the oracle outcomes for those same patches (test suite pass/fail), and any systematic
discrepancies between verifier verdict and oracle outcome.

Your job is to identify systematic weaknesses in the verify prompt and rewrite it
so that ACCEPT predictions are reliable and REJECT predictions catch real failures.

## The verify prompt that was used

{{prompt_text}}

## Sample verdicts — ACCEPT (verifier accepted the patch)

```json
{{accepted_samples}}
```

## Sample verdicts — REJECTED (verifier rejected; may include oracle result if available)

```json
{{rejected_samples}}
```

## Oracle discrepancies

{{oracle_discrepancies}}

(Format: "ACCEPT verdict → oracle FAIL: <file>:<line> reason: <msg>" or
"REJECT verdict → oracle PASS: <file>:<line> reason: <msg>" or "no oracle data")

---

## Evaluation rubric

Score each dimension 0–10:

**PART0_EFFECTIVENESS** (target: 9+)
Does the source scope check reliably catch fabricated patches — ones where the
`original` block was not visible in `original_evidence`?
Score 10 if the PART 0 audit stops scoring for clearly fabricated patches.
Score < 5 if the verifier gives high CORRECTNESS scores to patches whose `original`
block cannot be confirmed in the supplied evidence.

**SCORE_CALIBRATION** (target: 8+)
Are the five dimension scores proportional to actual patch quality?
A good verifier should give CORRECTNESS < 5 when the violation is structurally
unchanged, and ≥ 9 when the invariant holds for all code paths.
Score < 5 if CORRECTNESS scores cluster at 7–8 regardless of actual fix quality
(grade inflation), or if all scores are identical across very different patches.

**VERDICT_PRECISION** (target: 8+)
Does ACCEPT → oracle pass and REJECT → oracle fail consistently?
Score 10 if oracle_discrepancies is empty or rare (< 10% mismatch).
Score < 5 if more than 20% of ACCEPT verdicts lead to oracle failures.
If oracle data is absent, score 5 (unverifiable).

**WEAKNESS_SPECIFICITY** (target: 7+)
Do weakness descriptions give concrete, actionable better patches?
Score 10 if every `better_patch` in the weaknesses array is a valid unified diff.
Score < 5 if `better_patch` fields contain natural-language descriptions instead of
diffs, or if weaknesses are tautological ("fix the bug more correctly").

**JSON_COMPLIANCE** (target: 9+)
What fraction of verify calls return valid JSON matching the required schema?
Score 10 if > 95% of calls return parseable JSON with all required keys.
Score < 5 if non-JSON responses or missing keys (`scores`, `verdict`, `weaknesses`)
are common. This is a prompt instruction failure — if it's low, the schema example
in the prompt must be made more explicit.

**UNVERIFIABLE_HANDLING** (target: 8+)
When `tests_passed` is "unknown" or empty, does the prompt correctly score
FORMAL_GROUNDING and NO_REGRESSIONS as 5 (neutral) rather than 0 or 10?
Score < 5 if the verifier consistently gives 0 or 10 to unverifiable dimensions —
this distorts the overall score and triggers false REJECT verdicts.

---

## Your output

1. Score each dimension.
2. For each dimension < 8: identify the systemic prompt weakness causing it.
3. Rewrite the prompt. Rules:
   - Preserve the PART 0 / rubric / output structure from the original
   - Strengthen any part where systematic failures cluster
   - For SCORE_CALIBRATION failures: add concrete scoring anchors (e.g., "Score 9
     only if you can prove the invariant holds for ALL call paths, not just the
     patched line")
   - For VERDICT_PRECISION failures: tighten ACCEPT criteria — require that
     `original_confirmed_in_evidence` is true AND `violation_still_present` is false
   - For UNVERIFIABLE_HANDLING: make the neutral-5 rule a bolded mandatory step
   - Keep the same output JSON schema (source_scope, scores, weaknesses, verdict,
     verdict_reason)
   - Do not lengthen the prompt unless the length adds precision

Return JSON only:
{
  "scores": {
    "part0_effectiveness": {"score": 0, "justification": "..."},
    "score_calibration": {"score": 0, "justification": "..."},
    "verdict_precision": {"score": 0, "justification": "..."},
    "weakness_specificity": {"score": 0, "justification": "..."},
    "json_compliance": {"score": 0, "justification": "..."},
    "unverifiable_handling": {"score": 0, "justification": "..."}
  },
  "weaknesses": [
    {
      "dimension": "...",
      "systemic_cause": "what in the prompt causes this failure",
      "example_failure": "quote from rejected_samples or oracle_discrepancies that illustrates it"
    }
  ],
  "overall_score": 0.0,
  "improved_prompt": "full text of the rewritten prompt — complete, ready to use"
}
