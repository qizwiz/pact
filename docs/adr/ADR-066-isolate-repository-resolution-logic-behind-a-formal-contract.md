# ADR-066: Isolate Repository Resolution Logic Behind a Formal Contract

## Status
Proposed

## Date
2026-05-26

## Context
The function `heuristic_pkg_to_repo` in `upstream_analysis.py` is a cut vertex in the system's call graph — an articulation point whose removal would disconnect multiple components. This function performs package-to-repository mapping, a critical operation that multiple subsystems depend on. Currently, it operates without a formal behavioral contract, meaning:

- Callers have no guaranteed interface stability
- Changes to heuristic logic can cascade unpredictably through dependent modules
- Testing boundaries are undefined, making isolated unit testing difficult
- The structural dependency risk is invisible to maintainers

When a cut vertex lacks a contract, a single implementation change can fragment the entire call graph.

## Decision
We will extract `heuristic_pkg_to_repo` behind a formal interface contract (`RepositoryResolver` protocol or abstract base class) that defines:

1. Input/output types and constraints
2. Error conditions and exception guarantees
3. Performance characteristics (e.g., caching behavior)
4. Versioning policy for contract evolution

The current implementation becomes one concrete strategy behind this interface.

## Rationale
- **Cut vertex evidence**: Graph analysis identifies `heuristic_pkg_to_repo` as an articulation point — its removal disconnects the call graph
- **Missing contract**: No explicit interface exists, creating implicit coupling across module boundaries
- **Blast radius**: Changes to heuristic logic currently propagate uncontrolled through all dependents
- **Testability**: Absence of contract prevents dependency injection and isolated testing

Formalizing the contract converts a structural liability into a managed architectural boundary.

## Consequences
**Positive**:
- Callers depend on stable contract, not volatile implementation
- Multiple resolver strategies can coexist (heuristic, cached, ML-based)
- Unit tests can mock the interface without touching heuristic logic
- Breaking changes become explicit contract violations caught at type-check time

**Negative**:
- Adds interface overhead (one extra abstraction layer)
- Requires migrating existing call sites to use the contract
- Contract evolution requires versioning discipline

**Neutral**:
- Current heuristic logic remains unchanged, only its exposure mechanism changes
