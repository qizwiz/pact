# ADR-037: pact-self-loop — Recursive Self-Improvement with ML Convergence

**Status**: Accepted
**Date**: 2026-05-21

---

## Context

pact can find and fix bugs in external codebases. The natural extension is to
run pact on itself — recursively — until its own codebase is formally clean.
This requires:

1. A termination criterion (not infinite)
2. A way to know when improvement has stalled (convergence vs. stuck)
3. A fitness function that combines all quality signals into a single scalar
4. ADR generation to record what the loop decided and why

The prior art for this is gradient-free ML optimization (Nelder-Mead, CMA-ES,
Bayesian optimization). The key difference: our "gradient" is the CEGIS oracle
loop — the target test suite validates each patch before it's applied. We
cannot overfit because the oracle is impartial.

---

## Decision

Build `pact_loop.py` as a recursive self-improvement orchestrator with:

### Fitness function (loss analog, range [0,1], higher = better)

```
f = 0.25 * (1 − violation_rate)   ← primary goal: clean code
  + 0.20 * heal_accept_rate        ← CEGIS synthesis quality
  + 0.15 * oracle_confirm_rate     ← ground truth (oracle trust)
  + 0.15 * find_confirm_rate       ← real bug detection vs. hallucinations
  + 0.10 * topo_score              ← β₁ Betti topology health
  + 0.10 * avg_prompt_score        ← prompt quality (future capacity)
  + 0.05 * sheaf_score             ← Ȟ¹ rank reduction (interprocedural)
```

Weights chosen to reflect project priorities:
- Violation reduction is the primary goal but not the only signal
- Oracle confirmation is the ground truth signal — it cannot be gamed
- Topology and sheaf are lower weight because graphify is not always available
- Prompt quality matters for future iterations, not just current state

### Termination conditions (first one wins)

| Condition | Criterion | Rationale |
|-----------|-----------|-----------|
| `PROVED_CLEAN` | violations=0 AND Ȟ¹ rank=0 | Zero static + zero interprocedural |
| `CONVERGED` | \|Δfitness\| < ε for 3 iters | ML convergence: gradient vanished |
| `STUCK` | 0 patches accepted for 2 iters | CEGIS cannot make progress |
| `TIMEOUT` | iter ≥ max_iters | Safety bound |

### Analyzers run every iteration (MEASURE phase)

| Analyzer | Source | Signal |
|----------|--------|--------|
| Static checker | `checker.py` | 15 Python violation modes (AST) |
| Interprocedural | `pact_interproc.py` | Z3 Fixedpoint transitive violations |
| Sheaf | `pact_sheaf.py` | Ȟ¹ rank = min independent fixes needed |
| Z3 Datalog | `z3_engine.py` | Formally proved constraint violations |
| TDA | `pact_tda.py` | β₁ Betti topology score (requires graphify) |
| Blast radii | `reduce.py` | SCCs + hubs + blast radius (requires graphify) |
| LLM find | `find.py` | Semantic property violations + Hypothesis confirm |

### Priority ordering (before HEAL phase)

Violations are sorted by: `blast_radius × topology_score` (descending).
Fix the highest-connectivity functions first — maximum violation reduction
per oracle invocation.

### Prompt self-improvement trigger (IMPROVE phase)

| Prompt | Trigger condition |
|--------|------------------|
| `heal.md` | accept_rate < 85% |
| `find.md` | hypothesis confirm rate < 30% OR parse failures > 0 |
| `context.md` | empty output with rich git history (>500 chars) |

Each failed prompt invokes its `*_improve.md` companion, which scores the
current prompt on a rubric and rewrites it if overall_score < 0.8.
The rewritten prompt is saved in-place — stunspot principle: prompts may
evolve into alien encodings if the rubric score improves.

### ADR generation

The loop generates an ADR after each significant iteration (heal accepted
patches, or sheaf rank > 0, or termination declared). This makes the loop's
decisions auditable and reproducible.

---

## Alternatives Considered

**Fixed schedule** (run N times then stop): rejected because the loop may
converge early (wasted cycles) or stall at a local minimum (no signal to
stop). The convergence criterion is strictly superior.

**Only static checker in MEASURE**: rejected. The static checker finds
direct violations. The interprocedural Z3 analysis finds violations the
checker misses (transitive through call graph). The sheaf rank tells us how
many independent fixes are needed. All three signals are needed for an
accurate picture.

**No ADR generation**: rejected. The loop makes architectural decisions
(which violations to fix first, when to rewrite prompts) that should be
recorded. Without ADRs, we cannot audit why the codebase changed or
reproduce a healing run.

---

## Consequences

- `pact_loop.py` is the top-level orchestrator; all other pact modules are
  called as libraries within it.
- The fitness function is the single source of truth for "is pact better?"
  It encodes the project's stated priorities in quantitative form.
- Self-improvement means prompts change on disk. Track prompt files in git.
- The TLA+ spec `PactLoop.tla` (in `.pact_loop/`) proves OracleSafety and
  Termination. Run TLC to verify: `cd .pact_loop && java -jar tla2tools.jar PactLoop.tla`.
- `PROVED_CLEAN` requires BOTH checker violations = 0 AND sheaf Ȟ¹ rank = 0.
  A codebase can have zero static violations but still have interprocedural
  unsafe paths (e.g., unguarded LLM response access across function boundaries).

---

*Cross-references: ADR-001 (graph-first), ADR-003 (TLA+ semantic layer),
ADR-007 (structure-first scoring), ADR-017 (patch generation), ADR-036 (toolkit)*
