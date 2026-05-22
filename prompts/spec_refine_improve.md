# Prompt: spec_refine Self-Improvement

You are a prompt engineer for a TLA+ specification refinement system. The
spec_refine prompt produced a refinement that either failed validation
(MISSES_BUG or BREAKS_SPEC) or was unbounded and couldn't be model-checked.

Your job is to rewrite spec_refine so it generates refinements that pass
spec_validate with CATCHES_BUG and BOUNDED verdicts.

## The current prompt

```
{{prompt_text}}
```

## The failure

Validation verdict: {{validation_verdict}}
Failure mode: {{failure_mode}}

The refinement that failed:
```json
{{bad_refinement}}
```

The validation output explaining why it failed:
```json
{{validation_output}}
```

## Examples that succeeded (CATCHES_BUG + BOUNDED)

```json
{{good_samples}}
```

## Examples that failed

```json
{{bad_samples}}
```

---

## What to fix by verdict

**MISSES_BUG**: The new invariant doesn't actually exclude the bug pattern.
Root cause: the invariant references refined variables but doesn't constrain
them tightly enough. Fix: require the prompt to explicitly state the
counterexample state in terms of the refined variables, then verify the
invariant fails on that state before outputting.

**BREAKS_SPEC**: The refinement is over-constrained — it excludes valid
behavior. Root cause: the new invariant is too strong, or the modified actions
don't allow the system to make normal progress. Fix: require the prompt to
walk a "happy path" execution through the refined spec and check no new
invariant fires.

**UNBOUNDED**: The refined state space is infinite. Root cause: the prompt
chose a refinement (e.g., `violations \in SUBSET (FILE \X LINE \X MODE)`)
without providing concrete finite bounds for TLC. Fix: make the CONSTANT
binding step mandatory — always produce bounded symbolic sets with ≤ 4
elements per dimension.

**UNCERTAIN**: The prompt didn't generate enough structure for the validator.
Fix: require the prompt to produce a concrete counterexample trace in the
output — not just the invariant, but the specific state sequence that hits it.

## Scoring rubric

A good spec_refine output leads to spec_validate returning CATCHES_BUG:

- New invariant fails on the bug's execution trace: 30 points
- Existing invariants still hold on normal execution: 25 points
- TLC config includes explicit CONSTANT bounds (all sets finite): 20 points
- Modified actions are complete and syntactically valid TLA+: 15 points
- state_space_multiplier is ≤ 10x: 10 points

Total: 100 points. Threshold: 70+.
Current score (based on validation verdict {{validation_verdict}}): {{current_score}}

---

## Output format

Respond with JSON only:

```json
{
  "improved_prompt": "the full rewritten prompt text",
  "changes_made": ["change 1", "change 2"],
  "verdict_addressed": "{{validation_verdict}}",
  "overall_score": 0.0,
  "confidence": 0.0
}
```
