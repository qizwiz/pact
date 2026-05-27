# ADR-068: Isolate Cut-Vertex Functions with Behavioral Contracts

**Status**: Proposed  
**Date**: 2026-05-26

## Context

Six functions in `checker.py` are structural cut vertices (articulation points) in the call graph. Removing any one of these disconnects multiple subsystems. Notably:

- `check_codebase` (betweenness=0.0030) bridges 5 callers including test infrastructure and auto-apply logic to violation extraction subsystems
- `_run_semgrep` (betweenness=0.0007) connects 5 test cases to external tool integration
- `_run_mypy` (betweenness=0.0005) links 4 callers to type-checking infrastructure
- `_add`, `_guard_func_called_before`, and `_compute_dirty_set` serve as lower-level joints

None of these functions have documented behavioral contracts. Changes to their signatures, error handling, or side effects ripple through disconnected subsystems without guardrails.

## Decision

Establish explicit behavioral contracts for all six cut-vertex functions using:

1. Docstrings specifying preconditions, postconditions, and error conditions
2. Type annotations including return types and exception declarations
3. Unit tests verifying contract adherence independently of callers

Prioritize `check_codebase`, `_run_semgrep`, and `_run_mypy` (highest betweenness scores) for immediate contract definition.

## Rationale

Cut vertices are single points of failure. Their betweenness centrality scores quantify how many shortest paths traverse them—`check_codebase` alone sits on 0.30% of all paths. Without contracts:

- Breaking changes silently propagate (e.g., changing `_run_semgrep` error handling affects 5 test scenarios)
- Refactoring requires analyzing all transitive callers/callees
- New contributors cannot safely modify these functions

Contracts create interface stability at architectural joints.

## Consequences

**Positive**:
- Isolated testing of critical junctions without dependency chains
- Refactoring safety—contract violations caught before propagation
- Explicit error taxonomy reduces debugging across subsystem boundaries

**Negative**:
- Additional maintenance burden for contract documentation
- Potential performance overhead from contract validation in tests

**Neutral**:
- Future changes to these functions require contract review/update
