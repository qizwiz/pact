# ADR-060: Isolate Cut-Vertex Functions in intent.py with Behavioral Contracts

**Status**: Proposed  
**Date**: 2026-05-26

## Context

The file `intent.py` contains 8 cut-vertex functions that act as structural load-bearing joints in the call graph. These functions are articulation points: if any fails or changes behavior unexpectedly, multiple downstream call paths break. The affected functions are:

- `_write_importlinter_contract`
- `ProjectIntent.to_markdown`
- `_project_intent`
- `_synthesize_adr_contracts`
- `_extract_test_intent`
- `_read_truncated`
- `_match_tests_for_module`
- `_emit_importlinter_contract`

Currently, no formal behavioral contract exists for these functions. This creates hidden coupling and makes impact analysis impossible when modifying any of these integration points.

## Decision

We will define explicit behavioral contracts for all 8 cut-vertex functions using property-based tests or interface contracts (protocol classes). Each contract will specify preconditions, postconditions, and invariants. Functions will be refactored to minimize their cut-vertex status by extracting stable sub-operations with their own contracts.

## Rationale

Cut vertices in a call graph represent single points of failure. With 8 articulation points concentrated in one file and no behavioral contracts, we have:

- **High blast radius**: Changes to any function propagate unpredictably through multiple call paths
- **Difficult testing**: Without contracts, test coverage cannot guarantee behavioral stability
- **Maintenance risk**: Future developers lack explicit guarantees about function behavior
- **Refactoring paralysis**: Fear of breaking hidden dependencies blocks necessary changes

Formalizing contracts makes implicit dependencies explicit and creates a testing boundary that enables safe evolution.

## Consequences

**Positive**:
- Behavioral contracts enable safe refactoring of cut-vertex functions with regression detection
- Impact analysis becomes tractable: contract violations identify affected call paths immediately
- Testing effort focuses on high-leverage points rather than exhaustive path coverage
- New team members understand integration points through executable specifications

**Negative**:
- Initial effort required to formalize 8 behavioral contracts
- Contract maintenance overhead when function signatures or semantics evolve
- Potential performance cost if contracts include expensive runtime checks (mitigate with debug-only assertions)
