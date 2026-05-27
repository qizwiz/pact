# ADR-052: Formalize Behavioral Contracts for Cut-Vertex Functions in pact_sheaf.py

## Status
Proposed

## Date
2026-05-26

## Context

`pact_sheaf.py` contains 8 cut-vertex functions that form critical articulation points in the call graph:
- `_Harvester._visit_for_node`
- `_Harvester._enter_func`
- `_Harvester.visit_Subscript`
- `check_file_full`
- `_Harvester.visit_Return`
- `check_file`
- `_z3_check_guarded`
- `_coboundary_matrix_f2`

These functions are load-bearing joints: removing or modifying any one creates graph disconnection or cascading failures across dependent subsystems. Currently, **no formal behavioral contract exists** for these functions, creating ambiguity about:
- Expected input preconditions
- Output invariants and postconditions
- Side effects and state mutations
- Error handling obligations

This lack of specification increases maintenance cost and introduces latent defects when these functions are modified.

## Decision

Establish and document formal behavioral contracts for all 8 cut-vertex functions in `pact_sheaf.py` using Python docstring contracts (preconditions, postconditions, invariants) following the Eiffel-style contract model. Each contract must specify:
1. **Preconditions**: caller's obligation (input constraints)
2. **Postconditions**: callee's guarantee (output state)
3. **Invariants**: conditions that must hold throughout execution
4. **Raises**: exceptions and their trigger conditions

## Rationale

Cut-vertex functions control information flow and state transitions across multiple subsystems. Structural analysis identifies them as high-risk because:
- Modification requires coordination across 2+ dependent callees
- Graph disconnection propagates silently without contracts
- No machine-checkable specification exists to prevent contract violations

Formalizing contracts transforms implicit structural knowledge into explicit requirements, enabling:
- Static verification of call sites
- Automated testing against specification
- Safe refactoring with confidence boundaries

## Consequences

1. **Reduced defect propagation**: Contract violations are caught at entry/exit, preventing cascading failures in dependent call chains.

2. **Increased initial authoring cost**: Contracts require 10–15 minutes per function to formalize accurately; total effort ~2 hours for all 8 functions.

3. **Improved modifiability**: Future changes to these functions must preserve contracts; violations surface immediately during code review and CI.

4. **New constraint**: All call sites must be audited against contracts; violations block merge until resolved.
