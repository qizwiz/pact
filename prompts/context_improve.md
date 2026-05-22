# Prompt: Context Self-Improvement

You are a prompt engineer reviewing the performance of a git-history signal extraction prompt.
You will see: the context prompt that was used, a sample of its outputs, and failure modes
aggregated across runs.

Your job is to identify systematic weaknesses and rewrite the prompt so that future runs
extract more specific, confirmed-violation signals from project history.

## The context prompt that was used

{{prompt_text}}

## Sample outputs (high-quality — specific signals that led to confirmed violations)

```json
{{good_samples}}
```

## Sample outputs (low-quality — vague signals, parse failures, or empty results)

```json
{{bad_samples}}
```

## Failure modes (aggregated)

{{failure_modes}}

---

## Evaluation rubric

Score each dimension 0–10:

**JSON_COMPLIANCE** (target: 9+)
What fraction of runs returned valid JSON? Score 0 if the model writes reasoning text
before the JSON object. This is always a prompt instruction failure.

**SIGNAL_SPECIFICITY** (target: 8+)
Are confirmed_violations specific enough to weight find.md toward real bugs?
Score 10 if violations name the function AND the input class that caused the failure
(e.g., "echo: bytes message to text-mode file raises TypeError").
Score < 5 if violations are vague ("echo has had bugs").

**FALSE_POSITIVE_RATE** (target: 8+)
Do the confirmed_violations accurately reflect what the git history says broke?
Score < 5 if violations are invented rather than grounded in the actual git log,
changelog, or TODO/FIXME lines provided as input.

**FRAGILE_AREA_UTILITY** (target: 7+)
Are fragile areas actionable? Score 10 if each fragile area names a specific function
and describes the recurring failure pattern. Score < 5 if fragile areas are generic
module-level summaries that don't help find.md prioritize.

**CHANGELOG_EXTRACTION** (target: 7+)
Does the prompt extract the maximum signal from CHANGES.rst/CHANGELOG entries?
Score < 5 if "Fixed ..." entries in the changelog input appear as generic summaries
rather than specific function+input violations in the output.

---

## Your output

1. Score each dimension.
2. For each dimension < 8: identify the systemic prompt weakness causing it.
3. Rewrite the prompt. Rules:
   - Keep the same 2-part structure (PART 1 think, PART 2 output)
   - Strengthen any part where failures cluster
   - Add concrete extraction examples: show how a CHANGES.rst line maps to a
     confirmed_violation entry (function name, violation type, input class)
   - For JSON compliance issues: make the "Start with `{`" instruction mandatory and
     earlier in the prompt
   - Keep the same output schema: confirmed_violations[], fragile_areas[]
   - Do not lengthen unless precision requires it

Return JSON only:
{
  "scores": {
    "json_compliance": {"score": 0, "justification": "..."},
    "signal_specificity": {"score": 0, "justification": "..."},
    "false_positive_rate": {"score": 0, "justification": "..."},
    "fragile_area_utility": {"score": 0, "justification": "..."},
    "changelog_extraction": {"score": 0, "justification": "..."}
  },
  "weaknesses": [
    {
      "dimension": "...",
      "systemic_cause": "what in the prompt causes this failure",
      "example_failure": "quote from bad_samples that illustrates it"
    }
  ],
  "overall_score": 0.0,
  "improved_prompt": "full text of the rewritten prompt — complete, ready to use"
}
