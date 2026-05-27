# ADR-064: Isolate AST Utility Cut Vertices Behind Stable Contracts

**Status**: Proposed  
**Date**: 2026-05-26

## Context

The file `ast_utils.py` contains five functions that serve as cut vertices (articulation points) in the codebase call graph:
- `find_enclosing_function_chain`
- `_parse_ts_cached`
- `_ts_fn_name`
- `_ts_collect`
- `_py_chain`

These functions are structural load-bearing joints: their removal would disconnect significant portions of the call graph. Despite this critical role, no formal behavioral contract exists. Any change to these functions' signatures, error handling, or return semantics ripples uncontrolled through dependent modules. This creates a high-impact, high-risk coupling point without architectural safeguards.

## Decision

We will define and enforce explicit behavioral contracts for all five cut-vertex functions in `ast_utils.py` through:

1. **Interface documentation** specifying inputs, outputs, preconditions, postconditions, and error semantics
2. **Contract tests** validating behavioral invariants independent of implementation
3. **Deprecation protocol** requiring two-version migration cycles for any contract-breaking change
4. **Stability tier classification** marking these as "Tier-1 stable" interfaces

## Rationale

Cut vertices represent single points of failure in architectural dependency graphs. The five identified functions are articulation points: removing any one disconnects multiple subsystems. Without contracts, changes propagate unpredictably. The absence of behavioral specifications means:
- No compile-time or test-time enforcement of dependency assumptions
- Implicit coupling through undocumented behavior patterns
- High blast radius for refactoring or bug fixes

Formal contracts transform implicit structural risk into explicit, testable architectural boundaries.

## Consequences

**Positive**:
- Changes to cut-vertex functions become detectable at contract-test time rather than runtime failures in distant modules
- Refactoring is bounded by contract scope, reducing blast radius
- New consumers can depend on documented guarantees rather than implementation reading

**Negative**:
- Contract-breaking changes require two-version deprecation cycles, slowing evolution
- Initial contract authoring requires behavioral archaeology of existing call sites
- Contract tests add maintenance overhead (estimated 15-20 tests across five functions)
