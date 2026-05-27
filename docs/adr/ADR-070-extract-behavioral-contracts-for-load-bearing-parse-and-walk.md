# ADR-070: Extract Behavioral Contracts for Load-Bearing Parse and Walk Functions

## Status
Proposed

## Date
2026-05-26

## Context
`ts_checker.py` contains seven cut-vertex functions that are structural articulation points in the call graph. Their removal would disconnect the graph, yet none have explicit behavioral contracts. The four highest-betweenness nodes (`check_ts_files` at 0.0008, `check_ts_file` at 0.0008, `walk` at 0.0003, `walk_async_body` at 0.0001) bridge multiple distinct checking concerns—parsing, traversal, async validation, and LLM pattern detection. The recursive functions (`walk`, `walk_async_body`, `_find_all`) amplify failure impact through the call stack. Without contracts, callers depend on implicit behavior that can break silently across the seven identified functions.

## Decision
Extract explicit behavioral contracts for all seven cut-vertex functions into a separate `contracts.py` module. Define pre-conditions, post-conditions, and invariants using Python's `typing.Protocol` or decorator-based assertions for:
- `_get_lang` and `check_ts_file` (parsing layer)
- `walk`, `walk_async_body`, `_find_all` (traversal layer)
- `check_ts_files` (orchestration layer)
- `_scan_llm_unguarded` (pattern detection layer)

Wrap each function's entry and exit points with contract validation that can be toggled via environment variable for production.

## Rationale
Cut-vertex functions by definition are single points of failure. The betweenness scores, though low in absolute terms, indicate these seven functions mediate between otherwise disconnected components. `walk` is called by four different scanning functions and recursively calls itself—a contract violation here cascades. `check_ts_file` is the gateway for three different test and production paths. Missing contracts mean 7+ call sites depend on undocumented assumptions about return types, side effects, and error states.

## Consequences
- **Positive**: Callers gain explicit guarantees about parse results, traversal completeness, and error handling; contract violations fail fast at boundaries rather than propagating corrupt state
- **Positive**: Recursive functions (`walk`, `_find_all`) gain invariant checks that catch stack corruption or infinite loops earlier
- **Negative**: Runtime overhead of 5-10% when contract validation is enabled; requires discipline to keep contracts synchronized with implementation
- **Constraint**: New functions calling into these seven must satisfy documented pre-conditions; contract module becomes a mandatory dependency for all checker modules
