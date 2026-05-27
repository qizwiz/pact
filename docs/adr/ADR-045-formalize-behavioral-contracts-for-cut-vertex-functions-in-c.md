# ADR-045: Formalize Behavioral Contracts for Cut-Vertex Functions in Corpus Analysis

## Status
Proposed

## Date
2026-05-26

## Context

The call graph of `pact_corpus_analyze.py` contains three articulation points (cut vertices): `analyze_corpus`, `_fetch_raw`, and `analyze_entry`. These functions are structural load-bearing joints—removal of any one would disconnect the call graph into isolated components. No formal behavioral contract (preconditions, postconditions, invariants, error handling) exists for these functions. This creates silent failure risk: callers have no explicit guarantees about state transitions, exception handling, or data validity across the critical path.

## Decision

Define and enforce formal behavioral contracts for the three cut-vertex functions via docstring specifications (preconditions, postconditions, exceptions) and unit-level tests that verify contract compliance. Each contract must document:
- Input validation rules and acceptable domains
- Guaranteed output properties and state mutations
- Exception types raised on contract violation
- Idempotency or statefulness guarantees

## Rationale

Cut vertices are structural dependencies. When no contract exists, callers must infer behavior through reverse engineering or trial. A missing contract at an articulation point amplifies cascading failure risk across disconnected subgraphs. Formalizing contracts at these points creates a testable, auditable specification layer that prevents silent data corruption and makes call-site assumptions explicit and machine-verifiable.

## Consequences

- **Added constraint**: Each cut-vertex function requires documented contract and corresponding test case; code review must verify contract compliance before merge.
- **Improved resilience**: Callers can now validate inputs before crossing articulation points; failures become explicit exceptions rather than silent state corruption.
- **Maintenance burden**: Contract specifications must be kept in sync with implementation changes; drift becomes a code review task.
- **Better debugging**: Exception traces now reference contract violations, reducing time to root cause.
