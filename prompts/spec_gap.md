# Prompt: TLA+ Abstraction Gap Analysis

You are a formal methods analyst. A bug escaped a TLA+ specification — the spec
was checked by TLC, the proof passed, but the implementation had a real defect.
Your job is to identify exactly which abstraction in the spec was too weak, and
why it failed to exclude the bug.

## The bug

Description: {{bug_description}}
File: {{bug_file}}
Line: {{bug_line}}
How it manifested: {{bug_manifestation}}
How it was fixed: {{bug_fix}}

## The TLA+ spec that missed it

```tla
{{tla_spec}}
```

## The TLC result (passed — incorrectly)

```
{{tlc_output}}
```

---

## Your task

### Step 1 — Trace the execution path

Describe the concrete execution sequence that triggered the bug:
1. What state was the system in?
2. What action was taken?
3. What assumption did the implementation violate that the spec didn't model?

### Step 2 — Identify the abstraction gap

Name the specific TLA+ variable or operator that was too abstract:

- **Variable**: which state variable hid the concrete implementation detail?
- **Abstraction level**: what type was it (Nat, Bool, set, function, sequence)?
- **What it missed**: what concrete structure does the implementation actually have?
- **Gap name**: give this gap a short name (e.g. `violations_as_nat`, `cache_opacity`, `atomicity_assumption`)

Common abstraction gaps to check:
- Counting something as `Nat` when it's really a `set` with identity (membership matters, not just cardinality)
- Modeling mutable state as a single value when it has multiple observers with stale views
- Treating a multi-step operation as atomic when it has observable intermediate states
- Ignoring internal cache/memo state that can diverge from ground truth
- Modeling sequences as sets (losing order and duplicates)
- Assuming fair scheduling when the implementation has priority queues

### Step 3 — Why TLC didn't catch it

Explain the specific reason TLC's model checking passed despite the bug:
- Which invariant SHOULD have failed?
- What would the state look like in TLC's model when the bug triggers?
- Why does that state look safe in the current model?

### Step 4 — Propose the minimal refinement

State what the refined model needs:
- **New variable type**: what should `{{variable_name}}` become?
- **New invariant**: write it in TLA+ syntax
- **Enabling condition**: what action must preserve this invariant?
- **State space impact**: rough estimate — does this double, 10x, or explode the reachable states?

---

## Output format

Respond with JSON only:

```json
{
  "gap_name": "short_snake_case_identifier",
  "variable": "tla_variable_that_was_too_abstract",
  "abstraction_level_before": "Nat | Bool | set | BOOLEAN | sequence",
  "abstraction_level_after": "the tighter type",
  "gap_description": "one paragraph explaining what the abstraction missed",
  "execution_trace": ["step 1", "step 2", "step 3"],
  "invariant_that_should_have_failed": "TLA+ expression that would have caught it",
  "tla_refinement": "the refined variable declaration and/or invariant in TLA+ syntax",
  "state_space_multiplier": "1x | 2x | 10x | exponential | unknown",
  "confidence": 0.0
}
```
