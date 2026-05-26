# Structural Intent Analysis: `pact-standalone`

**Generated:** 2026-05-26  **Model:** anthropic/claude-sonnet-4-5  **Modules:** 7

## Project Summary

pact performs graph-based structural verification of codebases by building a call graph (via ast_utils.py, graph.py, import_graph.py), detecting architectural violations through formal constraint checking (checker.py, z3_engine.py, prover.py), and synthesizing minimal structural fixes (fixer.py, heal.py) using LLM-prompted patch generation with formal post-conditions. Unlike pattern-matching linters, pact treats violations as path properties over the call graph—a violation exists when a constraint fails along some execution path from source to sink.

## Violations

**6 total** — 🟡 6 medium

### `z3_engine.py`

**Purpose:** z3_engine.py implements two Z3 Fixedpoint Datalog engines for pact: (1) model_constraint verification (PactEngine) proves Django Model.objects.create() calls provide all required fields by joining site_creates EDB with model_req EDB to derive site_req IDB, then negating site_provides to yield violation IDB; (2) llm_response_unguarded verification (LLMResponseEngine) proves LLM-assigned variables are never accessed via .choices[0] without prior guard checks by joining llm_var EDB with unguarded_access EDB to derive llm_violation IDB. Both engines intern strings (file paths, field names, variable names) into 16-bit BitVec IDs, populate Z3 Fixedpoint with facts as rules, query with Exists over violation relation, and parse get_answer() DNF formula via _extract_tuples to yield concrete (site, field) or (scope, var) witness tuples. Unlike AST-only scanners, Z3 derives ALL violations in one query via stratified Datalog inference, treating violations as join/negation results over the call graph.

#### 🟡 `z3_engine.py:0`
#### 🟡 `z3_engine.py:0`
#### 🟡 `z3_engine.py:0`
### `tda.py`

**Purpose:** tda.py computes persistent homology (H0, H1) of a directed call graph by converting it to an undirected simplicial complex with filtration values derived from edge betweenness centrality. `compute_persistence(G, weight_attr='weight')` at line 336 is the sole public API — returns `PersistenceResult` with `total_persistence_h1` (sum of finite H1 bar lengths) as the primary fragility score, replacing raw β₁ counts in sheaf_summary. Uses gudhi SimplexTree (preferred) for full persistence diagrams or falls back to Euler-characteristic β₁ counting via Union-Find when gudhi unavailable. Filtration design: edge with high betweenness centrality appears early (low filtration value 1/(centrality+ε)) because it carries structural load; triangles inserted at max(3 edge filtration values) per flag complex construction. Essential H1 classes (infinite bars) count toward β₁; finite bars measure cycle robustness.

#### 🟡 `tda.py:0`
#### 🟡 `tda.py:0`
#### 🟡 `tda.py:0`