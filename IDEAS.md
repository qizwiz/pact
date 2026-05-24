# IDEAS.md — pact structural improvement queue

Research synthesized from 5 agents: Z3, NetworkX, Hypothesis, TLA+, Semgrep+LLM hybrids.
Last updated: 2026-05-23.

Anti-drift check: every item below requires at least one of Z3 / NetworkX / Hypothesis / TLA+ / Graphify.
Items a linter could produce are banned.

---

## PRIORITY QUEUE (build in this order)

### P1 — Z3 MaxSMT repair oracle (closes heal.py intent gap)
`heal.py` docstring: "Verification oracle: Z3 + test suite." Actual: LLM rubric only.

Fix: use Z3's `Optimize` API to find the **minimal syntactic change** that eliminates a contract violation.
- Source: DirectFix (ICSE 2015), Angelix (ICSE 2016), LLM-CEGIS-Repair (AAAI 2025)
- Implementation: MaxSMT soft clauses = "keep original expression", hard clause = "pass contract"
- LLM as sketch-filler only; Z3 certifies the patch is correct before it ships

### P2 — LLM+Z3 CEGIS feedback loop (LAUREL pattern)
LLM proposes invariant/postcondition → Z3 checks → counterexample injected back into LLM prompt → repeat.
- Source: LAUREL (arXiv 2405.16792), Loop Invariant Generation (arXiv 2508.00419)
- Closes: `intent.py` pipeline step 4 ("Violations — no formal verification")
- Already 90% wired: `contract_encoder.py` does Z3 check; feed `counterexample` back into LLM re-prompt

### P3 — Hypothesis from behavioral contracts (adversarial input generation)
Given a `behavioral_contract` string (already extracted by intent), generate `@given` strategies:
- `target(metric)` as gradient-free optimizer: "O(n log n)" → target execution time → find adversarial input
- `assume(precondition)` encodes contract preconditions without modifying strategies
- `RuleBasedStateMachine` for async/stateful contracts
- Crosshair backend (`hypothesis-crosshair`) for SMT-complete exhaustive path coverage
- Source: HypoFuzz, AUTOSPEC (arXiv 2511.17977), SpecPylot (arXiv 2604.16560), arXiv 2510.09907

### P4 — NetworkX → intent trigger (cut vertices auto-trigger contract extraction)
`reduce.py` computes cut vertices but does not trigger intent analysis on them.
Fix: when a cut vertex is found, automatically call `pact intent analyze` on that file.
- k-core decomposition: high-k core functions are mutually dependent → higher bug risk (CoreBug, MDPI 2022)
- Minimum vertex cut for blast-radius estimation ("if X breaks, at minimum Y subsystems affected")
- Source: CoreBug, arXiv 2405.03801

### P5 — TLA+ async loop verification (fairness from μ-calculus)
Check Python async task queues for starvation / livelock.
- Emit PlusCal spec from Celery/asyncio control flow
- Check: eventual termination (liveness under weak fairness), deadlock freedom, starvation freedom
- Apalache (Z3-backed TLA+) for large state spaces where TLC is intractable
- Source: TraceFix (arXiv 2605.07935), CONCUR 2024 (arXiv 2407.08060), Apalache (PACMPL 2019)

### P6 — Graphify rationale nodes → intent layer
Rationale nodes (free intent text in call graph) are extracted but never fed to intent.
Fix: pass rationale node text as a declared intent context layer to `understand.md`.
- Source: CLAUDE.md gap list item 5

---

## Z3 IDEATION (15 items from research)

**Immediately applicable:**
1. **MaxSMT minimal repair** — Z3 `Optimize` API, soft clauses = original code, hard = contract. Finds one-click patch. (DirectFix, Angelix, Verifix)
2. **CEGIS counterexample feedback** — LLM proposes → Z3 finds violating input → back into LLM. (LAUREL arXiv 2405.16792)
3. **MaxSMT type inference** — encode Python typing rules as SMT, MaxSMT finds most-specific consistent typing. "Suggest type annotations" mode. (ETH Zürich MaxSMT-Based Type Inference)
4. **Deal/contract decorator verification** — parse `@require`/`@ensure` AST, emit Z3, check for contradictory contracts. (deal library)
5. **Decision bisection for optimization** — binary search over an optimization variable vs. one big MaxSMT query. O(log N) SAT checks, each faster. (johndcook.com 2025)

**Medium-term:**
6. **Concolic test generation** — instrument Python symbolically, negate branch conditions in Z3, generate inputs that flip each branch. (SAGE/Pex pattern)
7. **Exploit-path generation** — mark taint sources, propagate symbolically, ask Z3 if tainted value reaches dangerous sink. (SAEG ESORICS 2024)
8. **API contract inference from tests** — Z3 infers weakest precondition that makes passing tests satisfy postcondition. (SemFix generalization)
9. **SQL/ORM query equivalence** — encode relational algebra as Z3 set constraints, detect semantically equivalent but slower queries. (Provenance-guided Datalog, POPL 2020)
10. **Z3 tactic auto-selection** — MCTS over Z3 tactic chains finds fastest sequence per formula class. (Z3alpha 2024, +42.7% on QF_BV)

**Limitations to avoid:**
11. **Nonlinear arithmetic wall** — Z3 gives `unknown` on `i * stride`. Workaround: linearise, treat products as uninterpreted functions + Gröbner basis upstream.
12. **Path explosion from loops** — compress integer ranges to intervals, summarise loops with Z3-verified summaries, never unroll.
13. **CVC5 for hot-path checks** — CVC5 ~2x faster than Z3 on pure bitvector; abstract solver interface to swap backends.
14. **Network/firewall ACL verification** — encode routing rules as bitvectors, check reachability. Azure pattern. (Microsoft Research)
15. **CEGIS synthesis of pandas/numpy idioms** — given assertion, CEGIS generates expression satisfying it. (fitzgen.com)

---

## NETWORX IDEATION (13 items from research)

**Immediately applicable:**
1. **k-core decomposition → bug risk** — `nx.core_number()`: high-k core = mutually dependent = statistically more likely to be buggy. (CoreBug MDPI 2022)
2. **Minimum vertex cut → blast radius** — "if X breaks, at minimum Y subsystems affected." Near-linear algorithm now exists. (arXiv 2405.03801)
3. **Harmonic centrality → universal entry points** — handles disconnected subgraphs better than betweenness. `nx.harmonic_centrality()`
4. **Eigenvector centrality → propagation risk** — "called by important functions" = bug amplifier score. `nx.eigenvector_centrality()`
5. **PageRank on weighted call graph** — weight edges by call frequency, rank as highest-value test targets after a change. (arXiv 2010.03482)

**Medium-term:**
6. **Leiden community detection → module boundaries** — Leiden guarantees well-connected communities (fixes Louvain). Edges crossing boundaries = architectural debt. (`leidenalg` package)
7. **Spectral gap (Laplacian eigenvalues) → anomaly detection** — commit shifts spectral gap significantly = topology-level structural change, not just diff. (SpectralGap IJCAI 2025, arXiv 2505.15177)
8. **Temporal call graph evolution** — track k-core, community, spectral gap across releases to detect coupling creep. (arXiv 2210.08316)
9. **Graph edit distance → refactoring impact score** — "cosmetic refactor" with high GED is flagged. `nx.optimize_graph_edit_distance()` (arXiv 2601.04085)
10. **Taint as graph reachability** — DFG + call graph, `nx.has_path(G, source, sink)` over taint subgraph. (arXiv 2501.08947, VIVID arXiv 2505.16205)

**Longer-term:**
11. **GNN defect prediction** — AST + CFG + DFG as multi-layer graph, dual-branch attention. (Nature Scientific Reports 2025)
12. **GNN call graph completion** — predict missing dynamic call edges that static analysis misses. (Call Me Maybe arXiv 2506.18191)
13. **Hypergraph dependency resolution** — many-to-many version constraints as hyperedges, expose monorepo conflicts pairwise graphs hide. (HyperRes arXiv 2506.10803)

---

## TLA+ IDEATION (15 items from research)

**Highest ROI for Python code:**
1. **Async loop termination** — prove Celery/asyncio loops always reach terminal state, never exceed delegation budgets. (TraceFix arXiv 2605.07935 — cut deadlock from 31.1% to 14.1%)
2. **Queue starvation via μ-calculus** — progress, justness, weak/strong fairness as template formulae. mu X.phi = liveness. nu X.phi = safety. Nested mu-nu = fairness. (CONCUR 2024, arXiv 2407.08060)
3. **TLA+ in Python notebooks** — TLC inside Jupyter kernel, no context switch. (TLA+ Community Event 2025)
4. **Apalache (Z3-backed TLA+)** — outperforms TLC on large integer domains (retry counters, queue depths). (PACMPL 2019, TACAS 2023)
5. **Deadlock freedom in concurrent task graphs** — PlusCal model of Python async + TLC check.

**Amazon/Microsoft patterns worth stealing:**
6. **Trace validation in CI** — instrument async code with trace logging, validate against TLA+ spec in CI. (CCF/Microsoft Research, decentralizedthoughts May 2025 — found 5 safety + 1 liveness bug)
7. **Cosmos DB consistency verification** — exposed undocumented user-observable behaviors. Pattern: multi-consistency-level ORM/cache verification. (arXiv 2210.13661)
8. **PlusCal from OpenAPI/AsyncAPI** — auto-generate PlusCal specs from REST/gRPC schemas. (IFM 2023)

**Longer-term:**
9. **LLM → TLA+ spec generation** — parse async docstrings into IR, LLM emits PlusCal, TLC checks, counterexamples fed back. (arXiv 2509.23130, arXiv 2512.09758)
10. **TLAPS + Z3 for unbounded proof** — discharge "retry counter always below circuit-breaker threshold" deductively over unbounded queues.
11. **Inductive invariant inference** — auto-discover TLA+ invariants, feed back as Python assertions. (arXiv 2205.06360)
12. **Cryptographic protocol verification** — OAuth2/JWT/PKCE as finite state → TLC. (Springer 2025)
13. **Bisimulation equivalence** — refactored async service observationally equivalent to original. (arXiv 2108.00142)
14. **Fairness in mu-calculus**: nu X. mu Y. (phi ∧ [a]Y ∨ X) = "eventually stable liveness" vs mu X. nu Y. (phi ∨ X) = "persistent reachability" — neither expressible in LTL/CTL alone.
15. **Smart casual verification cadence** — TLA+ spec for core algorithm, TLC on finite subsets in CI, runtime trace validation.

---

## HYPOTHESIS IDEATION (14 items from research)

**Wire into contract_encoder pipeline immediately:**
1. **`target()` as adversarial optimizer** — pass execution time as metric, Hypothesis hill-climbs toward worst case. "O(n log n) claim → adversarial input." (HypoFuzz)
2. **Crosshair backend** — Z3 at choice-sequence level, exhaustive path coverage. Ensemble: random + coverage + SMT. (`hypothesis-crosshair`, officially supported 2025)
3. **SpecPylot pipeline** — NL contract → `icontract` `@require`/`@ensure` → Crosshair verify → Hypothesis stubs. (arXiv 2604.16560) — this is exactly what pact's contract_encoder should emit
4. **`assume()` as precondition gate** — encode contract preconditions without modifying strategy; combine with `target()` for constrained adversarial search.
5. **Ghostwriter as scaffold** — `hypothesis write <module>` emits strategies from type annotations; NL-contract layer adds property oracle on top.

**Medium-term:**
6. **AUTOSPEC: NL spec → stateful machine** — RFC prose → I/O grammar → `RuleBasedStateMachine`. 92.8% message-type recovery. Adapt for API behavioral contracts in English. (arXiv 2511.17977)
7. **Agentic property inference** — LLM reads module + docstrings, infers cross-function properties, synthesizes PBTs, executes. 56% valid bugs found. (arXiv 2510.09907)
8. **Differential oracle via `RuleBasedStateMachine`** — model two implementations, each `@rule` fires both, any divergence = counterexample. Minimal shrinking finds minimal diverging sequence.
9. **Metamorphic relations from NL** — "variance is shift-invariant", "sort is idempotent" — no oracle needed. LLM-generated MRs from docstrings: 75% GPT-4 error detection at 8.6% FPR. (arXiv 2406.06864)
10. **PropertyGPT pattern** — RAG over codebase → generate `@given` properties from docstring contracts → Crosshair verifier. No human property authorship. (arXiv 2405.02580)

**Specialized:**
11. **HypoFuzz for CI** — 1 worker per CPU, shared example database, `hypothesis.event()` labels as feedback. Overnight fuzzer, any counterexample replayed automatically.
12. **`hypothesis-trio` for concurrent bugs** — Hypothesis controls task interleaving, finds races in async code. Combine with `target()` for schedule-sensitivity search.
13. **ML framework fuzzing** — adversarial tensor shapes/dtypes for ML API behavioral contracts. (arXiv 2403.12723)
14. **CodeSpecBench harness** — Hypothesis as execution harness for evaluating LLM-proposed spec quality. Ground-truth benchmark for NL-contract translation accuracy.

---

## SEMGREP + LLM HYBRIDS (18 items from research)

**Immediately applicable:**
1. **Autogrep pipeline** — CVE patch → LLM extracts pattern → Semgrep rule → deploy. 39,931 patches → 645 high-quality rules. (LambdaSec, Feb 2025, GitHub: lambdasec/autogrep)
2. **RuleLLM supply chain** — PyPI/npm malicious package detection via YARA+Semgrep rules from malware samples. (arXiv 2504.17198)
3. **LLM-generated rules from NL** — Semgrep Assistant architecture: pattern generation separate from FP filtering. Two-stage template.
4. **Cross-file taint at scale** — Semgrep Pro `--pro`: 72% detection vs 48% CE on WebGoat. Inter-file, inter-procedural.
5. **Reachability-aware SCA** — vulnerable function never called = low priority. 98% fewer FPs vs manifest-only. (Semgrep Supply Chain)

**LLM+Formal hybrids (highest leverage):**
6. **LLM-CEGIS-Repair** — MaxSAT localizes buggy statement → LLM fills hole → test-suite counterexample feeds back. 1,431 faulty programs evaluated. (AAAI 2025, GitHub: pmorvalho/LLM-CEGIS-Repair)
7. **VERGE** — LLM chain-of-thought + formal refinement steps, spec violations guide re-generation. Directly analogous to pact's heal loop. (arXiv 2601.20055)
8. **ALGO dual-LLM oracle** — LLM 1 generates brute-force reference oracle; LLM 2 generates efficient candidate; oracle = formal spec. Oracle vs. efficiency separation. (NeurIPS 2023, arXiv 2305.14591)
9. **LLM-guided quantified SMT** — LLM guides search strategy inside SMT for quantified formulas (domain where pure SMT struggles exponentially). (arXiv 2601.04675)
10. **Neuro-symbolic NL verification** — LLM analyzes constraint relations in NL instructions, encodes as Z3 programs. NL spec → machine-checkable constraint. (arXiv 2601.17789)

**Medium-term:**
11. **Lean Copilot / DeepSeek-Prover** — LLM suggests Lean 4 tactics interactively. Stepwise proves 77.6% of seL4 theorems. (arXiv 2404.12534, arXiv 2603.19715)
12. **Semgrep taint + LLM loop** — taint chain from source to sink; LLM explains and ranks by exploitability. (VIVID arXiv 2505.16205)
13. **OpenGrep fork** — per-arity taint signatures, nested-function-as-lambda dataflow. Open-source alternative to Pro taint.
14. **LLM-derived pCFG for synthesis guidance** — mine LLM failure modes as signal about search space; build probabilistic CFG to guide CEGIS. (arXiv 2403.03997)

**Architecture insight:**
The Autogrep/RuleLLM pipeline (NL → Semgrep rule) and the LLM-CEGIS loop (NL → Z3 check → LLM repair) are structurally identical: LLM proposes, formal engine filters, counterexample feeds back. The missing piece: CVE description → taint rule → CEGIS-verified rule correctness before deployment. This is pact's natural position in that pipeline.

---

## THE CONNECTED ENDGAME

```
intent_gap invariant (behavioral_contract: str)
         │
         ├─→ Z3 contract_encoder.py ──────────────→ UNSAT (holds) / SAT (counterexample)
         │                                                    │
         │                                          MaxSMT repair oracle
         │                                                    │
         ├─→ Hypothesis @given(strategy_from_contract)  ←── counterexample fed back
         │           │
         │      target(exec_time) → adversarial worst case
         │
         ├─→ TLA+ PlusCal (async/stateful contracts)
         │           │
         │      μ-calculus fairness: liveness + safety + starvation freedom
         │
         ├─→ NetworkX k-core → cut vertex → blast radius
         │
         └─→ Semgrep cross-file taint → LLM-CEGIS loop → verified rule
```

Every node in this graph produces output a linter cannot produce.
Every edge is a feedback loop, not a one-way pipeline.
