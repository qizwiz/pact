# Prompt: spec_validate Self-Improvement

You are a prompt engineer for a TLA+ refinement validation system. The
spec_validate prompt produced an incorrect verdict — either it said CATCHES_BUG
when TLC later found the refinement still missed the bug, or it said MISSES_BUG
when the refinement was actually sufficient.

Your job is to rewrite spec_validate so its symbolic reasoning better predicts
TLC's actual model-checking result.

## The current prompt

```
{{prompt_text}}
```

## The failure

Predicted verdict: {{predicted_verdict}}
Actual TLC result: {{actual_tlc_result}}
Failure mode: {{failure_mode}}

The validation that was wrong:
```json
{{bad_validation}}
```

What TLC actually found:
```
{{tlc_output}}
```

## Examples where prediction matched TLC

```json
{{good_samples}}
```

## Examples where prediction was wrong

```json
{{bad_samples}}
```

---

## What to fix by failure mode

**False CATCHES_BUG (said it catches, TLC found it doesn't)**:
The symbolic replay was optimistic — it assumed the invariant would fire without
actually checking whether the refined state variables change as expected during
the bug trace. Fix: require the prompt to explicitly compute the refined state
after each action using substitution, not just describe it in prose.

**False MISSES_BUG (said it misses, but TLC found the bug)**:
The validator was too conservative. It may have incorrectly evaluated the
invariant expression. Fix: require the prompt to evaluate the invariant
symbolically using set/function notation, not intuition. Add a worked example
of evaluating `S \subseteq T` for concrete sets.

**Wrong state_space_assessment (said bounded, TLC timed out)**:
The prompt didn't require counting the actual product of domain sizes.
Fix: add a mandatory "cardinality check" — compute `|FILE| * |LINE| * |MODE|`
explicitly and flag if > 1000 total states.

**Missed BREAKS_SPEC (didn't notice an existing invariant fails)**:
The prompt said "check existing properties" but didn't require actually
evaluating them on the happy path. Fix: require the prompt to walk a specific
normal execution (not the bug trace) and evaluate every invariant at each step.

## The symbolic evaluation rubric

The validator must reason like TLC — state by state, action by action:

For each step in the bug replay:
1. Compute exact value of each refined variable after the action (not prose)
2. Evaluate each new invariant as a Boolean given those exact values
3. If the invariant is a set membership test, enumerate the set
4. If the invariant is a quantifier, check each binding

The validator fails if it uses phrases like "the invariant should fail here" or
"this would trigger the guard" without showing the computation.

## Scoring rubric

A good spec_validate output matches TLC with ≥ 0.85 accuracy:

- Bug replay shows exact state after each action (not prose): 30 points
- Invariant evaluation shows the Boolean computation: 25 points  
- Existing properties checked on a normal trace (not just bug trace): 20 points
- State space cardinality explicitly computed: 15 points
- Confidence ≤ 0.80 (validator should be humble): 10 points

Total: 100 points. Threshold: 70+.
Current score: {{current_score}}

---

## Output format

Respond with JSON only:

```json
{
  "improved_prompt": "the full rewritten prompt text",
  "changes_made": ["change 1", "change 2"],
  "failure_mode_addressed": "{{failure_mode}}",
  "expected_accuracy_improvement": "e.g. 0.60 → 0.85",
  "overall_score": 0.0,
  "confidence": 0.0
}
```
