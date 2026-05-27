# ADR-063: Isolate Reduction Pipeline Cut Vertices Behind Contracts

## Status
Proposed

## Date
2026-05-26

## Context
The `reduce.py` module contains 8 cut-vertex functions that act as structural load-bearing joints in the call graph: `ReductionResult.summary`, `contract_sccs`, `cut_vertex_files`, `_live_roots`, `compute_blast_radii`, `analyze_graph_reduction`, `_build_digraph`, and `find_sccs`. A cut vertex is an articulation point whose removal would disconnect the call graph, making it a critical dependency for multiple callers. These functions currently operate without formal behavioral contracts, creating systemic fragility: any breaking change to their signatures, invariants, or error handling propagates immediately to all dependent code paths. The absence of contracts means failures cascade unpredictably through the reduction pipeline.

## Decision
Introduce formal behavioral contracts for all 8 cut-vertex functions in `reduce.py` using Python protocols or abstract base classes. Each contract must specify:
- Input/output type signatures with variance annotations
- Preconditions and postconditions
- Error conditions and exception types
- Immutability guarantees for return values

Refactor existing callers to depend on these contracts rather than concrete implementations. Treat the cut vertices as a public API surface requiring semantic versioning and deprecation policies.

## Rationale
Cut vertices represent single points of failure in the module's architecture. With 8 such functions lacking contracts, any signature change requires N-way coordination across all callers, and runtime failures lack isolation boundaries. The structural evidence shows these are not ordinary helper functions but architectural load-bearing elements. Contracts create explicit compatibility surfaces that allow independent evolution of callers and implementations, and enable substitution for testing or optimization without coordinated changes.

## Consequences
- **Improved resilience**: Contract violations fail fast at boundaries rather than cascading as runtime errors deep in call stacks
- **Reduced coordination cost**: Callers can evolve independently as long as they satisfy the contract interface
- **Testing isolation**: Mock implementations can be substituted via the contract for unit testing dependent code
- **Added constraint**: All changes to cut-vertex functions require contract compatibility analysis and potential versioning
