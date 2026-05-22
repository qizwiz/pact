# Prompt: TLA+ Refinement Validation

You are a TLA+ model checker interpreter. A spec refinement has been proposed
to close an abstraction gap. Your job is to determine whether the refinement
actually catches the original bug — without running TLC (which may be slow).
You reason symbolically about reachable states.

## The original bug

{{bug_description}}
Execution trace: {{execution_trace}}

## The gap it exploited

{{gap_name}}: {{gap_description}}

## The proposed refinement

Variable changes:
{{variable_changes}}

New invariants:
```tla
{{new_invariants}}
```

Modified actions:
```tla
{{modified_actions}}
```

## The original spec (for reference)

```tla
{{tla_spec}}
```

---

## Your task

### Step 1 — Replay the bug in the refined model

Walk through the execution trace that triggered the original bug, step by step,
using the REFINED state variables and actions:

For each step:
- What is the state before?
- Which action fires?
- What is the state after?
- Does any new invariant fail? If so, which one and what's the counterexample?

### Step 2 — Existing properties check

For each existing INVARIANT and PROPERTY in the original spec:
- Does it still hold in the refined model? (brief argument, not a full proof)
- If uncertain, flag it: `"status": "uncertain"`

### Step 3 — State space assessment

Estimate whether TLC can actually check this:
- How many distinct states does the refined model add?
- Is it bounded? (TLC requires finite state spaces)
- If unbounded, suggest a CONSTRAINT to bound it

### Step 4 — Verdict

One of:
- `CATCHES_BUG` — the bug trace hits a new invariant violation
- `MISSES_BUG` — the bug trace still passes all invariants (refinement insufficient)
- `BREAKS_SPEC` — an existing invariant fails on valid behavior (over-constrained)
- `UNBOUNDED` — state space is infinite; TLC cannot check it without CONSTRAINT
- `UNCERTAIN` — cannot determine without running TLC

---

## Output format

Respond with JSON only:

```json
{
  "verdict": "CATCHES_BUG | MISSES_BUG | BREAKS_SPEC | UNBOUNDED | UNCERTAIN",
  "bug_replay": [
    {
      "step": 1,
      "action": "ActionName",
      "state_before": "...",
      "state_after": "...",
      "invariant_violated": "InvariantName or null",
      "counterexample": "state description if violated, else null"
    }
  ],
  "existing_properties": [
    {"property": "PropertyName", "status": "holds | fails | uncertain", "reason": "..."}
  ],
  "state_space_assessment": {
    "bounded": true,
    "estimated_states": "integer or 'unbounded'",
    "suggested_constraint": "TLA+ CONSTRAINT expression or null"
  },
  "refinement_quality": "sufficient | insufficient | over_constrained",
  "suggested_fix": "if verdict is not CATCHES_BUG, what additional refinement is needed",
  "confidence": 0.0
}
```
