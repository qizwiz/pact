# ADR-065: Isolate Critical Path Functions in pact_loop.py Behind Behavioral Contracts

**Status**: Proposed  
**Date**: 2026-05-26

## Context

The file `pact_loop.py` contains 8 cut-vertex functions that serve as articulation points in the call graph: `_measure_tda`, `_measure_find`, `_stuck`, `_reprioritize`, `PactLoop.run`, `heal`, `_norm`, and `_z3_optimal_heal_order`. A cut vertex is a structural load-bearing joint — removing it disconnects the call graph. These functions represent single points of failure: any breakage cascades through dependent modules. Currently, no formal behavioral contract exists to specify invariants, preconditions, or postconditions for these critical functions.

## Decision

We will establish formal behavioral contracts (using design-by-contract assertions or a contract verification library) for all 8 cut-vertex functions in `pact_loop.py`, and refactor to reduce the number of cut vertices from 8 to ≤3 by introducing intermediate abstraction layers.

## Rationale

- **Structural evidence**: 8 cut vertices in a single file creates fragility — any change to these functions risks breaking multiple dependent call paths
- **Absence of contracts**: Without specified preconditions, postconditions, and invariants, changes to these functions are high-risk and debugging failures requires tracing through the entire call graph
- **Cascading failure risk**: Cut vertices concentrate control flow; failure in `PactLoop.run` or `heal` can disable entire subsystems with no fault isolation
- **Maintainability**: Future developers cannot safely modify these functions without understanding all transitive dependencies

## Consequences

- **Improved resilience**: Behavioral contracts enable runtime validation of assumptions, catching invariant violations at articulation points before they cascade
- **Reduced coupling**: Refactoring to ≤3 cut vertices introduces intermediate interfaces, creating fault isolation boundaries
- **Testing clarity**: Contracts serve as executable specifications, making test coverage requirements explicit for critical paths
- **Migration cost**: Adding contracts requires analyzing existing call sites to determine correct preconditions; estimated 8-12 developer-days for contract specification and 3-5 days for refactoring to reduce cut vertices
