# ADR-007: Structure-first scoring — violations annotate, not rank

**Status**: Accepted  
**Date**: 2026-05-16  
**Deciders**: qizwiz/pact graph reduction design

## Context

`--reduce` ranked simplification targets (tangles, pass-throughs, hubs) using:

```python
score = reduction_potential + violation_count * 0.5
```

This formula conflates two independent signals. A structural problem (a call cycle, a pure pass-through hop) exists and carries complexity cost regardless of whether any violation has been observed at that node. The formula penalizes structurally identical candidates by violation count, placing a clean cycle below a dirty one.

The underlying engineering principle: fewer moving parts is better unconditionally, in the same way gate minimization in chip design and structural member reduction in bridge engineering are engineering objectives independent of observed failures. A bridge engineer does not leave a redundant member in place because it hasn't failed yet.

## Decision

Split the concepts:

- **`score`**: pure structural complexity eliminated (`float(reduction_potential)`). Drives ranking. Independent of violations.
- **`urgency`**: violations × 0.5. Annotates the output when violations are present. Does not affect rank.
- **`--fitness`**: new flag reporting the ratio of the actual call graph to its theoretical minimum (transitive reduction of the condensation DAG), as a geometric mean of node and edge compression ratios. Gives a per-codebase structural fitness score (0.0–1.0, higher = closer to minimum).

The `summary()` method for each candidate now shows `violations=N` only when N > 0, making it clear violations are informational rather than primary.

## Consequences

- A pass-through with zero violations ranks equally to one with violations if their `reduction_potential` is equal. Both are equally worth removing.
- The structural fitness score tracks independently of violation density — a codebase can improve its fitness score without finding or fixing any violations, by refactoring structural complexity.
- Violation reachability (how many callers can reach a violation site through the call graph) is the correct causal metric connecting structural complexity to violation risk — this is a separate feature, not encoded in `score` or `urgency`. See also: Younis/Malaiya 2014 (IEEE HASE) for academic precedent.
- `GraphFitness.score = geometric_mean(node_ratio, edge_ratio)` where ratios are `minimum/actual` nodes and edges after full three-stage reduction.

## Alternatives considered

1. **Keep violations in score, increase weight** — rejected. Doubles down on conflating independent signals.
2. **Two separate sort keys** (structural first, violations as tiebreaker) — viable but adds complexity. Rejected in favor of clean separation: rank by structure, annotate with violations separately.
3. **Remove violation_count entirely from ReductionCandidate** — rejected. The annotation is genuinely useful for prioritizing which structural problems to fix first when you have limited time. It just shouldn't drive rank.
