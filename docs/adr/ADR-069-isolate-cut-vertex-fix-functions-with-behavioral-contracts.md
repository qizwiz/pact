# ADR-069: Isolate Cut-Vertex Fix Functions with Behavioral Contracts

**Status**: Proposed  
**Date**: 2026-05-26

## Context

The `fixer.py` module contains 8 cut-vertex functions — structural articulation points whose removal would disconnect the call graph. These functions (`fix_file`, `_fix_llm_unguarded`, `_fix_eager_any_guard`, `_fix_prompt_injection_risk`, `_fix_unvalidated_lookup_chain`, `_collect_preceding_assignments`, `_collect`, `_split_list_elements`) have zero behavioral contracts documenting their preconditions, postconditions, or invariants. The primary entry point `fix_file` (betweenness=0.0013) bridges test code to 7 other cut vertices, creating a brittle dependency chain where any single function's failure cascades through the entire fix pipeline.

## Decision

We will add explicit behavioral contracts (preconditions, postconditions, invariants) to all 8 cut-vertex functions in `fixer.py`, enforced through runtime assertions in development and logged verification in production. Each contract will document expected AST node types, required attributes, and guaranteed output structure.

## Rationale

Cut vertices with betweenness scores between 0.0000 and 0.0013 indicate critical control-flow bottlenecks. The absence of behavioral contracts means:
- No documented expectations for `_fix_*` transformation functions that modify AST structures
- No guarantees about helper functions (`_collect`, `_split_list_elements`) that prepare data for transformations
- `fix_file` serves 5 distinct test scenarios without explicit contracts defining success/failure states
- Maintenance requires reverse-engineering behavior from implementation rather than contracts

## Consequences

- **Testing becomes targeted**: Each cut vertex can be validated against its contract independently, reducing combinatorial test explosion
- **Refactoring safety improves**: Contracts create a verifiable interface, allowing internal implementation changes without breaking callers
- **Performance overhead added**: Runtime assertion checking adds 5-10% overhead in development mode (disabled in production)
- **Documentation debt reduced**: Contracts serve as machine-checkable documentation, replacing stale comments
