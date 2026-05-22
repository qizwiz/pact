# Prompt: spec_gap Self-Improvement

You are a prompt engineer for a formal methods analysis system. The spec_gap
prompt failed to correctly identify an abstraction gap — either it named the
wrong variable, missed the gap entirely, or produced a gap description that
didn't lead to a useful refinement.

Your job is to rewrite the spec_gap prompt so future runs catch this class of
gap correctly.

## The current prompt

```
{{prompt_text}}
```

## The failure

Failure mode: {{failure_mode}}

What the prompt produced:
```json
{{bad_output}}
```

What the correct gap analysis should have been:
```json
{{correct_output}}
```

How we know the output was wrong:
- {{failure_reason}}

## Examples of correct gap analysis (for calibration)

Good outputs (high confidence, led to useful refinements):
```json
{{good_samples}}
```

Bad outputs (low confidence or wrong verdict):
```json
{{bad_samples}}
```

---

## What to fix

Common failure modes for spec_gap and how to address them:

**Missed the gap (gap_name is null or generic)**:
The prompt didn't give enough structure for identifying WHERE in the spec the
abstraction is weak. Add a mandatory "variable inventory" step: list every
TLA+ variable with its type and what concrete implementation detail it abstracts.
Then check each for the gap.

**Wrong variable (named a variable that wasn't the problem)**:
The prompt didn't require tracing the execution path through the spec. Add a
mandatory replay step: walk the bug's execution trace using TLA+ state semantics
and find where the trace becomes invisible to the spec.

**Gap description too vague to drive refinement**:
The prompt didn't require the analyst to state what concrete type the variable
SHOULD be. Add a constraint: gap_description must include "the variable should
be [type] to model [concrete detail]."

**Confidence calibration off**:
If confidence > 0.7 but refinement led to MISSES_BUG verdict: the prompt is
over-confident. Add a skeptic step: "Before finalizing, argue why this gap
analysis might be wrong."

## Scoring rubric for the improved prompt

A good spec_gap output:
- Names a specific TLA+ variable (not "the spec" generically): 20 points
- States a concrete type transition (Nat → set, Bool → function): 20 points
- Includes an execution trace that becomes invisible to the spec: 20 points
- Proposes an invariant in valid TLA+ syntax: 20 points
- Has state_space_multiplier that isn't "unknown": 10 points
- Has confidence ≤ 0.85 (epistemic humility): 10 points

Total: 100 points. Threshold for "good": 70+.
Current score: {{current_score}}

## Instructions

Rewrite the prompt. Changes must be:
1. **Targeted** — fix the specific failure mode above, don't redesign the whole prompt
2. **Testable** — the new prompt should produce outputs that score ≥ 70 on the rubric
3. **Stable** — don't remove constraints that worked in the good examples above

---

## Output format

Respond with JSON only:

```json
{
  "improved_prompt": "the full rewritten prompt text",
  "changes_made": ["change 1", "change 2"],
  "failure_modes_addressed": ["failure_mode_1"],
  "overall_score": 0.0,
  "confidence": 0.0
}
```
