# ADR-058: Isolate Critical Detection Functions in Failure Mode Analysis

## Status
Proposed

## Date
2026-05-26

## Context
The `failure_mode.py` module contains 8 cut-vertex functions that act as structural load-bearing joints in the call graph. These articulation points represent single points of failure: if any of these functions fail or change unexpectedly, entire subgraphs of functionality become unreachable. The affected functions span core detection capabilities:
- Object persistence scanning (`_new_object_save_lines`)
- Format validation (`_scan_file_format_mismatch`, `_scan_file_json_loads_unguarded`)
- Async pattern detection (`_AsyncioRunVisitor.visit_Call`, `_scan_file_asyncio_run_in_async`)
- Security scanning (`_find_llm_guard_functions`)
- Exception handling detection (`_scan_file_bare_except`)
- Lookup chain resolution (`_LookupChainVisitor._clear_for_target`)

No formal behavioral contracts exist to protect these critical junctions from breaking changes or runtime failures.

## Decision
We will refactor cut-vertex functions into isolated, contract-protected components with explicit interfaces. Each function will be extracted behind a documented protocol (abstract base class or Protocol type) with pre/post-conditions enforced via defensive assertions or validation decorators. Critical detection functions will be wrapped in a facade layer that provides fallback behavior and logging for failure scenarios.

## Rationale
- **Structural risk**: 8 cut vertices create 8 single points of failure in the analysis pipeline
- **Missing contracts**: No behavioral guarantees exist; silent failures or interface changes cascade unpredictably
- **Blast radius**: Each cut vertex controls access to dependent subgraphs; failure blocks entire detection categories
- **Maintenance burden**: Private implementation functions (`_scan_file_*`, `_AsyncioRunVisitor`) lack boundaries that prevent unintended coupling

## Consequences
- **Positive**: Failures in detection logic become contained and observable rather than causing silent analysis gaps
- **Positive**: Contract definitions enable confident refactoring and parallel development of detection modules
- **Positive**: Facade layer allows graceful degradation (e.g., skip one detector if it fails, continue others)
- **Constraint**: Adds interface abstraction overhead; protocol definitions require maintenance alongside implementations
- **Constraint**: Requires immediate team agreement on contract enforcement strategy (runtime assertions vs. static type checking)
