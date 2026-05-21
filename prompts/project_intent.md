# Prompt: Project-Level Intent Analysis

You are reading a software project to build a **project-level world model** — a
complete formal understanding of what this project is SUPPOSED to do, what
architectural invariants must hold everywhere, and where the highest-risk gaps
are between intent and implementation.

This is not per-file analysis. You are looking at the whole project at once.
The output conditions every per-module analysis that follows.

## Project: {{project_name}}

## File to read

The full project triage is at: {{triage_file}}

The triage contains: file listing, README excerpt, key file rankings, and
cross-cutting concerns identified in the first pass.

## Selected module analyses

{{module_summaries}}

These are per-module analyses already completed. They contain invariants and
violations per module. Your job is to synthesize across them.

---

## PART 0 — COHERENCE CHECK

Before synthesizing, check for consistency across the module analyses:

**Step 0a**: Do any two modules claim conflicting invariants? List any conflicts.
For example: module A says "all exceptions are re-raised" and module B says
"all exceptions are swallowed silently."

**Step 0b**: Are any cross-cutting concerns (auth, error handling, serialization)
described inconsistently across modules? List them.

**Step 0c**: What is the HIGHEST CONFIDENCE cross-module invariant — something that
every module agrees on, even implicitly? This is the project's strongest structural
guarantee.

---

## PART 1 — PROJECT-LEVEL INVARIANTS

From the triage world model and per-module analyses, derive 3–7 **project-level**
invariants. These must:
- Cut across at least 2 modules
- Not be derivable from any single module in isolation
- Be falsifiable — there must be a concrete code pattern that would violate them

For each invariant:
- State it formally (∀ / ∃ / if-then)
- Identify which modules it applies to
- Identify which violations across the module analyses threaten it

---

## PART 2 — CROSS-MODULE VIOLATION CLUSTERS

Group the per-module violations into clusters where:
- The same root cause appears in multiple modules
- A single fix in the right place would resolve all violations in the cluster
- OR a cross-module contract is violated (module A assumes X about module B, but B doesn't guarantee X)

For each cluster:
- Name it (like a failure mode)
- List the violations it contains (file:line)
- State the fix class (add_guard | add_signal | fix_encoding | fix_heuristic | structural)
- Estimate the Ȟ¹ rank — minimum number of independent fixes needed

---

## PART 3 — ANALYSIS PRIORITY

Given the full picture, answer:

1. **Highest-risk violation**: Which single violation, if unfixed, is most likely to
   cause a production incident? Why?

2. **Highest-leverage fix**: Which single fix would resolve the most violations?
   This is the fix with highest Ȟ¹ impact.

3. **World model confidence**: How confident are you in the project-level invariants?
   0.0 = pure guess, 1.0 = proven from visible code. Use discrete ranges:
   0.90–1.00: directly proven from multiple consistent sources
   0.70–0.89: strongly implied by module analyses
   0.60–0.69: plausible but requires seeing more code to confirm

---

Return JSON only:

{
  "coherence_check": {
    "invariant_conflicts": [],
    "cross_cutting_inconsistencies": [],
    "strongest_guarantee": "..."
  },
  "project_invariants": [
    {
      "id": "proj_inv_001",
      "statement": "...",
      "formal": "∀ / ∃ / if-then statement",
      "applies_to_modules": ["mod1.py", "mod2.py"],
      "threatened_by_violations": ["file:line", ...],
      "confidence": 0.85
    }
  ],
  "violation_clusters": [
    {
      "name": "cluster name",
      "violations": ["file:line", ...],
      "shared_root_cause": "...",
      "fix_class": "...",
      "h1_rank": 1
    }
  ],
  "analysis_priority": {
    "highest_risk_violation": {"location": "file:line", "reason": "..."},
    "highest_leverage_fix": {"description": "...", "resolves_violations": ["file:line", ...]},
    "world_model_confidence": 0.75
  }
}
