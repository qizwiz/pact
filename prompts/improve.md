# Prompt: Self-Improvement

You are a prompt engineer whose job is to improve a prompt used for semantic
code analysis. You will see: the prompt, the output it produced, and the
source code it analyzed. Your job is to identify what was weak and rewrite
the prompt to be better.

## The prompt that was used

{{prompt_text}}

## The source code that was analyzed

```python
{{source_excerpt}}
```

## The output it produced

```json
{{output}}
```

---

## Evaluation rubric

Score each dimension 0–10:

**SPECIFICITY** (target: 9+)
Does the output name real functions, classes, constants from the actual source
code? Or does it use generic software terms ("the module processes data",
"handles errors gracefully") that could apply to any codebase?
Score 10 only if every claim is anchored to specific code constructs.
Score < 6 if the output reads like it was written without looking at the code.

**GROUNDEDNESS** (target: 8+)
Are the invariants derived from THIS code's own design decisions, or from
general Python best practices? An invariant like "functions should not return
None without documentation" is generic. An invariant like "all _scan_file_*
functions are cached with lru_cache keyed on path string — this means test
round-trips must use different paths" is grounded.
Score 10 only if invariants could not have been generated from a description
of the module alone — they required reading the actual code.

**CALIBRATION** (target: 7+)
Are confidence scores varied and meaningful? All 0.9 means Claude is not
actually uncertain about anything, which is wrong. Good calibration shows
0.95 for things obviously stated in the code, 0.7 for inferred patterns,
0.6 for things that look like they might be intentional.
Score < 5 if all confidence scores are within 0.1 of each other.

**COMPLETENESS** (target: 8+)
Does the understanding cover the real failure modes in the code? Look at the
source — are there try/except blocks, None checks, empty-list guards that
the output didn't mention? Are the assumptions section listing the actual
implicit assumptions visible in the code?
Score 10 only if you cannot find anything significant the output missed.

**ACTIONABILITY** (target: 8+)
For each violation reported: would a developer reading it immediately
understand what to fix, why it matters given this specific project, and
what the consequence of NOT fixing it is?
Score < 6 if violations are described in abstract terms without quoting
the specific code or explaining the production impact.

**NON-OBVIOUSNESS** (target: 7+)
Does the understanding reveal something about the code that a 30-second
scan wouldn't show? Does it identify the subtle design decisions, the
implicit contracts, the things that would bite you if you didn't know them?
Score < 5 if the output is a summary of what the code does rather than
an analysis of why it works the way it does.

---

## Your output

1. Score each dimension with a number and a one-sentence justification.

2. For each dimension scoring < 8, identify:
   - The specific weakness (quote from the output that illustrates it)
   - What a better version would look like (write the better version)

3. Rewrite the prompt to address the weaknesses. Rules for rewriting:
   - Keep the same output JSON schema
   - Add specificity requirements where output was generic
   - Add reasoning constraints where output skipped steps
   - Add examples of GOOD vs GENERIC answers for sections that were weak
   - Add explicit prohibitions ("do not write X unless you can quote the code")
   - Do not make the prompt longer unless the length adds precision
   - Every sentence in the rewritten prompt must earn its place

Return JSON only:
{
  "scores": {
    "specificity": {"score": 0, "justification": "..."},
    "groundedness": {"score": 0, "justification": "..."},
    "calibration": {"score": 0, "justification": "..."},
    "completeness": {"score": 0, "justification": "..."},
    "actionability": {"score": 0, "justification": "..."},
    "non_obviousness": {"score": 0, "justification": "..."}
  },
  "weaknesses": [
    {
      "dimension": "specificity",
      "example": "quote from output showing the problem",
      "better_version": "what it should have said"
    }
  ],
  "improved_prompt": "full text of the rewritten prompt — complete, ready to use"
}
