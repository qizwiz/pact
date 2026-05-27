# ADR-059: Isolate Test Utility Functions from Direct Test Dependencies

## Status
Proposed

## Date
2026-05-26

## Context
The file `test_trace_miner.py` contains 5 cut-vertex functions that act as articulation points in the call graph. These functions—`TestFilterToProject.test_stdlib_filtered_out`, `TestMineInvariantsSubprocess.test_stdlib_filtered_in_mine_invariants`, `TestFindPython.test_finds_venv_python`, `TestMineInvariantsSubprocess.fake_run`, and `TestMinedInvariantToDict.test_has_required_keys`—are structural load-bearing joints whose removal would disconnect components of the test suite. This creates brittle test architecture where changes to any of these functions can cascade failures across the test suite. No formal behavioral contract exists to define their interfaces or invariants.

## Decision
Extract reusable test utilities and fixtures from the 5 cut-vertex functions into dedicated test helper modules with explicit contracts. Move `fake_run` to a test fixture module. Convert structural dependencies on specific test methods into dependencies on shared helper functions or pytest fixtures with documented interfaces.

## Rationale
Cut vertices represent single points of failure in the call graph. With 5 articulation points concentrated in one test file, the test suite exhibits high structural coupling. The absence of formal contracts means these integration points lack stability guarantees. Isolating these functions behind explicit interfaces reduces the blast radius of changes and enables independent evolution of test scenarios.

## Consequences
- **Better**: Test failures become localized; changes to test scenarios don't cascade through articulation points
- **Better**: Helper functions gain explicit contracts, improving maintainability and discoverability
- **Better**: Test suite becomes more modular with clear boundaries between utilities and test cases
- **Constraint**: Requires upfront effort to extract and document 5 helper interfaces; ongoing discipline to maintain helper stability
