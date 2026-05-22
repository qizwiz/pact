# Prompt: TLA+ Spec Refinement

You are a TLA+ specification engineer. You have an identified abstraction gap
and must produce a concrete spec refinement that closes it — tight enough to
catch the original bug, abstract enough that TLC can still model-check it.

## The gap

Name: {{gap_name}}
Variable: {{variable}}
Before: {{abstraction_level_before}}
After: {{abstraction_level_after}}
Description: {{gap_description}}

## The original bug

{{bug_description}}

## The invariant that should have caught it

```tla
{{invariant_that_should_have_failed}}
```

## The current spec

```tla
{{tla_spec}}
```

## The current TLC config

```
{{tlc_config}}
```

---

## Your task

Produce a refined spec that:

1. **Closes the gap** — the original bug pattern is now excluded by an invariant
2. **Stays checkable** — state space doesn't explode (prefer bounded sets/sequences)
3. **Preserves existing properties** — all current INVARIANT and PROPERTY statements still hold
4. **Is minimal** — change as little as possible; don't re-architect the spec

### Refinement strategy by gap type

**violations_as_nat → violations_as_set**:
Replace `violations \in Nat` with `violations \in SUBSET (FILE \X LINE \X MODE)`.
Add `MeasureSeesCurrentState == \A v \in violations: v[1] \notin healed_files`.
Bound: `FILE = {"f1","f2","f3"}`, `LINE = 1..10`, `MODE = {"bare_except","json_loads"}` for TLC.

**cache_opacity → cache_explicit**:
Add `cache \in [FILE -> SUBSET VIOLATION]` to state.
Add `CacheConsistent == \A f \in DOMAIN cache: cache[f] = actual_violations(f)`.
Invalidation action: `ClearCache(f) == cache' = [cache EXCEPT ![f] = {}]`.

**atomicity_assumption → interleaved_steps**:
Split the monolithic action into `BeginHeal`, `ApplyPatch`, `ClearStale`.
Add `HealProgress == []<>(phase = "idle")` to ensure no stuck intermediate state.

**stale_reader → versioned_state**:
Add `version \in Nat` to state. Increment on every write.
Add `ReaderSeesCurrentVersion == reader_version = version`.

### Output sections

**Section A — Variable changes**
List every variable that changes type, with before/after.

**Section B — New operators**
Define any new TLA+ operators needed (Converged, Stale, etc.).

**Section C — New invariants**
Write each new invariant in valid TLA+ syntax.
Prefix with the gap name: `{{gap_name}}_Safe == ...`

**Section D — Modified actions**
For each action that must be updated to preserve the new invariants, show the
full updated action definition.

**Section E — TLC config additions**
The additional lines to add to the .cfg file:
- CONSTANT bindings for any new symbolic sets
- INVARIANT declarations for new invariants
- CONSTRAINT if needed to bound the state space

**Section F — Verification claim**
A 1-2 sentence argument for why TLC will now find a counterexample if the
original bug pattern is introduced, and why existing properties still hold.

---

## Output format

Respond with JSON only:

```json
{
  "variable_changes": [
    {"variable": "name", "before": "type", "after": "type", "reason": "..."}
  ],
  "new_operators": "TLA+ operator definitions as a single string",
  "new_invariants": [
    {"name": "InvariantName", "tla": "TLA+ expression", "catches": "what bug pattern this excludes"}
  ],
  "modified_actions": [
    {"action": "ActionName", "tla": "full updated TLA+ definition"}
  ],
  "tlc_config_additions": "lines to append to .cfg",
  "verification_claim": "why this catches the bug",
  "state_space_multiplier": "estimate vs current",
  "confidence": 0.0
}
```
