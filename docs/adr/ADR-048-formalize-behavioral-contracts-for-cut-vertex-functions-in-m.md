# ADR-048: Formalize Behavioral Contracts for Cut-Vertex Functions in mcp_server.py

## Status
Proposed

## Date
2026-05-26

## Context

The call graph analysis of `mcp_server.py` identified five cut-vertex functions—`_tool_pact_heal`, `_handle`, `_tool_pact_loop`, `_tool_pact_tda`, and `_send`—that serve as articulation points in the system's control flow. These functions are load-bearing joints: removal of any one breaks graph connectivity and isolates critical subsystems. Cut vertices represent single points of failure and high coupling risk. The structural risk is compounded by the absence of formal behavioral contracts for these functions, meaning their preconditions, postconditions, and side effects are implicit or undocumented. This ambiguity increases the blast radius of changes and complicates refactoring.

## Decision

Establish and enforce formal behavioral contracts (via docstrings, type hints, and assertion guards) for all five cut-vertex functions in `mcp_server.py`. Each contract must document:
- **Preconditions**: required state, input invariants, and caller responsibilities
- **Postconditions**: guaranteed state transitions and return guarantees
- **Side effects**: external calls, state mutations, or I/O operations
- **Error modes**: exceptions raised and their recovery semantics

Additionally, introduce integration tests that verify contract compliance at call sites and treat contract violations as test failures.

## Rationale

Cut-vertex functions are structural hinges; their behavior must be unambiguous to enable safe composition and refactoring. Formal contracts are a lightweight mechanism to:
- Reduce cognitive load for maintainers and reduce misunderstanding at integration boundaries
- Enable static tooling (type checkers, linters) to catch contract violations early
- Document the semantic interface, not just the signature
- Support future decomposition or replacement of these functions without behavioral regression

The absence of contracts is a coordination and visibility debt; formalizing them costs upfront but pays compounding returns as the system evolves.

## Consequences

- **Contract documentation artifacts**: Each cut-vertex function gains a detailed docstring with pre/postconditions and side-effect inventory—increases code volume by ~10–15 lines per function but improves clarity and testability.
- **Integration test coverage**: New test suite validates contract invariants at call sites; this adds test code but reduces risk of silent contract violations during refactoring.
- **Dependency transparency**: Side effects (e.g., network calls, state mutations) become explicit and traceable, facilitating later refactoring or isolation of these functions for async/parallel execution.
- **Breaking change risk reduced**: Future changes to cut-vertex functions are now constrained by their declared contracts; changes that violate contracts trigger test failures, enforcing backward compatibility or deliberate versioning.
