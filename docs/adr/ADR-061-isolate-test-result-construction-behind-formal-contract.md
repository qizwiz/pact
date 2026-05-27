# ADR-061: Isolate Test Result Construction Behind Formal Contract

## Status
Proposed

## Date
2026-05-26

## Context
`test_tda.py` contains two cut-vertex functions that act as articulation points in the test module's call graph: `_make_result` and `test_result_render_contains_expected_sections`. These functions are structural load-bearing joints — their removal would disconnect portions of the call graph. Currently, no formal behavioral contract exists for these functions, creating brittleness: changes to either function ripple uncontrolled through dependent call paths, and their responsibilities are implicit rather than explicit.

## Decision
Extract `_make_result` into a dedicated `TestResultBuilder` class with an explicit interface contract. Refactor `test_result_render_contains_expected_sections` to depend on this interface rather than the implementation. Document the contract with protocol or abstract base class definitions.

## Rationale
- **Cut-vertex risk**: Both functions are articulation points; changes propagate broadly with no isolation
- **Missing contract**: No formal interface means no stability guarantee for dependents
- **Testability**: Implicit construction logic makes test fixtures brittle and interdependent
- **Structural clarity**: A load-bearing joint should have explicit responsibilities and boundaries

## Consequences
- **Positive**: Test result construction becomes independently testable and mockable
- **Positive**: Structural dependencies become explicit through interface definitions
- **Positive**: Changes to result construction logic are localized behind the contract
- **Constraint**: Adds interface overhead and indirection to test fixture creation
- **Constraint**: Requires migration of existing test code to use the new builder
