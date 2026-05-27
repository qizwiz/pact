# ADR-062: Isolate Cut-Vertex Functions in enrich.py Behind Stable Interfaces

**Status**: Proposed  
**Date**: 2026-05-26

## Context

The file `enrich.py` contains 8 functions that are structural cut vertices (articulation points) in the call graph: `_fetch_commit_metadata`, `render_project_context`, `IntentCoverage.render`, `_fetch_prs`, `_mine_pydriller`, `_fetch_spec_branches`, `_gather_docs`, and `build_intent_graph`. These functions are load-bearing joints where removal or failure would disconnect significant portions of the call graph. Currently, no formal behavioral contract exists for these functions, meaning downstream callers have no stability guarantees and changes ripple unpredictably through the system.

## Decision

Establish explicit interface contracts for all cut-vertex functions in `enrich.py` by:
1. Defining input/output contracts using type annotations and docstrings specifying pre/post-conditions
2. Wrapping external data-fetching functions (`_fetch_commit_metadata`, `_fetch_prs`, `_fetch_spec_branches`) in adapter interfaces with error boundaries
3. Extracting `build_intent_graph` and rendering functions into a facade with versioned contracts

## Rationale

Cut vertices represent single points of failure in the dependency graph. When these 8 functions change, all transitive callers are affected. The lack of behavioral contracts means:
- No compilation or runtime checks prevent breaking changes
- Testing must cover all call paths through these junctions
- Refactoring cost is O(n×m) where n = cut vertices, m = downstream dependents
- The bus factor for `enrich.py` is effectively 1

## Consequences

**Positive**:
- Changes to cut-vertex implementations become safe internal refactoring when contracts are stable
- Test surface reduces to contract boundaries rather than full integration paths
- Downstream teams can depend on versioned interfaces rather than implementation details

**Negative**:
- Initial effort to define and document 8 behavioral contracts
- Adds interface versioning overhead if contracts must evolve
- May require adapter/wrapper layers for external dependencies (GitHub API, PyDriller)
