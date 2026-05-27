# ADR-067: Isolate Cut-Vertex Functions in MCP Server Message Handler

**Status**: Proposed  
**Date**: 2026-05-26

## Context

The `mcp_server.py` module contains five cut-vertex functions (`_handle`, `_tool_pact_heal`, `_tool_pact_tda`, `_tool_pact_loop`, `_send`) that serve as articulation points in the call graph. These functions are structural load-bearing joints: their removal would disconnect the graph into isolated components. The module has no formal behavioral contract. A cut vertex in a dependency graph represents a single point of failure where changes ripple uncontrollably through the system. When multiple cut vertices exist without isolation, they amplify integration risk and create cascading failure scenarios.

## Decision

Extract the five cut-vertex functions into an explicit `MessageHandlerContract` interface with formal pre/post-conditions. Implement this contract through two separate classes: `ToolDispatcher` (for `_tool_pact_*` functions) and `MessageTransport` (for `_handle` and `_send` functions). Enforce the contract boundary using dependency injection at module initialization.

## Rationale

- **Cut-vertex concentration**: Five articulation points in a single module indicates structural fragility where any change to these functions forces revalidation of all dependent code paths
- **Missing contract**: Without formal contracts, the behavioral expectations of these load-bearing functions exist only implicitly, preventing safe parallel development and increasing defect injection risk during modifications
- **Cascade containment**: Separating tool dispatch from message transport creates two independent failure domains, reducing the blast radius when either subsystem requires modification
- **Graph connectivity**: Converting cut vertices into interface boundaries preserves system connectivity while enabling independent evolution of subgraphs

## Consequences

- **Improved**: Modifications to tool dispatch logic no longer require analyzing message transport side effects, reducing change analysis cost by isolating dependency chains
- **Improved**: Explicit contracts enable property-based testing of pre/post-conditions, catching behavioral regressions before graph-wide integration testing
- **Added constraint**: All new message handling or tool dispatch code must implement the formal contract interfaces, adding design-time overhead
- **Added constraint**: Dependency injection configuration must be maintained at module boundaries, increasing initialization complexity
