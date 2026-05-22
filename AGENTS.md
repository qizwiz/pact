# AGENTS.md — pact capability corpus

This file is the **undriftable spec** for everything pact can do as an autonomous agent.
Read it at the start of every session to know what exists, what's wired, and what isn't.

---

## Agent entrypoints

| Entrypoint | Where | What it does |
|---|---|---|
| `python -m pact.mcp_server` | `mcp_server.py` | MCP stdio server — exposes 4 tools to any MCP-capable agent (Claude Code, Cursor, etc.) |
| `python -m pact` | `cli.py` | CLI: check, find, heal, loop, tda, sheaf, interproc, scan-github, spec-learn |
| `pact-mcp` | pyproject entry point | Same as mcp_server, installed as a binary |
| GitHub label `pact-auto` | `.github/workflows/claude-agent.yml` | Triggers `anthropics/claude-code-action@beta` on issues/PRs |
| `@claude` mention | `.github/workflows/claude-agent.yml` | Same — Claude reads the thread and acts |
| Cron `0 */2 * * *` | `.github/workflows/pact-auto-pr.yml` | Autonomous PR filer — runs auto_pr.py every 2h |
| CI push/PR | `.github/workflows/ci.yml` | Test suite + pact dogfood on itself |
| Nightly | `.github/workflows/nightly-corpus.yml` | Corpus expansion — scan_github → new targets |

---

## MCP server tools (mcp_server.py)

Four tools currently exposed. Any agent that speaks MCP (Claude Code, etc.) can call these.

| Tool | Input | Output | Status |
|---|---|---|---|
| `pact_check` | `path` | violations JSON | ✅ Wired |
| `pact_find` | `file_path`, `use_context`, `improve` | violations + counterexamples | ✅ Wired |
| `pact_heal` | `violations_json`, `project_root`, `test_cmd` | patches + oracle_confirmed | ✅ Wired |
| `pact_context` | `file_path`, `repo_root` | git/changelog intent signals | ✅ Wired |
| `pact_loop` | — | convergence loop result | ❌ **Not exposed** |
| `pact_tda` | `path` | β₁ Betti topology scores | ❌ **Not exposed** |
| `pact_sheaf` | `file_path` | Ȟ¹ cohomological violations | ❌ **Not exposed** |
| `pact_spec_learn` | — | TLA+ gap report | ❌ **Not exposed** |

**Gap**: Add `pact_loop`, `pact_tda`, `pact_sheaf` to the MCP tool registry so Claude Code can trigger full autonomous analysis.

---

## Z3 capability (z3_engine.py, pact_interproc.py, pact_sheaf.py)

### Currently wired

- `z3.Solver` + `Bool/And/Or/Not/Implies` — model_constraint and LLM-response safety guards
- `z3.BitVec` — integer overflow detection in loop bounds
- `z3.Fixedpoint` / Datalog — transitive call-graph reachability (`pact_interproc._interproc_z3`)
- `z3.Solver` in sheaf — `_z3_check_guarded()` verifies LLM output-guard propagation across call sites

### Not used — concrete gaps

| Z3 feature | What it would enable | File to add it |
|---|---|---|
| `z3.Optimize` | Minimum-cardinality fix set — heal fewest files to clear most violations | `pact_loop.py` |
| `unsat_core()` | Diagnosis: which preconditions caused a proof failure | `z3_engine.py::PactEngine` |
| `z3.Array` sorts | Model dict/list state across call chain (currently we use Bool sets) | `pact_interproc.py` |
| Universal quantifiers `ForAll` | Property-level proofs ("for ALL inputs, X holds") | `z3_engine.py::LLMResponseEngine` |
| `z3.Tactic` / tactic combinators | Faster proofs with `then(simplify, solve-eqs, smt)` | `pact_cfg_proof.py` |
| Proof objects `z3.set_param(proof=True)` | Certificate generation for ADRs ("we proved X") | `z3_engine.py` |

**Highest-impact next**: `z3.Optimize` in pact_loop — instead of healing violations in checker-output order, solve for the minimum set of files whose fix reduces total violations most. This is a set-cover instance that Z3 Optimize solves exactly.

---

## Hypothesis capability (test_hypothesis_checkers.py, test_loop.py)

### Currently wired

- `@given` + `@settings` — fuzz test individual checker invariants
- `st.sampled_from` — draw from known violation types
- `st.lists`, `st.integers`, `st.floats` — basic type generators

### Not used — concrete gaps

| Hypothesis feature | What it would enable | File to add it |
|---|---|---|
| `RuleBasedStateMachine` | Stateful test of pact_loop — model Measure→Heal→Check as a state machine; find divergences | `test_loop.py` |
| `st.from_type(T)` | Auto-generate IterationState, MeasureResult, LoopResult instances from their type annotations | `test_loop.py` |
| `st.builds(cls, ...)` | Composite object generation for complex checker inputs | `test_hypothesis_checkers.py` |
| `target()` (coverage-guided) | Maximize fitness function exploration — finds edge cases in compute_fitness | `test_loop.py` |
| `assume()` + `reject()` | Filter invalid combos in multi-field generators (e.g. heal_accepted ≤ heal_attempted) | `test_hypothesis_checkers.py` |
| `@reproduce_failure` | Shrinking replay for CI failures | global conftest |

**Highest-impact next**: `RuleBasedStateMachine` for pact_loop — model the 4-phase loop as a state machine, use Hypothesis to find sequences that violate FitnessMonotone or get stuck when they shouldn't.

---

## TLA+ / TLC capability (docs/tla/, spec_learner.py, gen_tlc_model.py)

### Currently wired

- `PactLoop.tla` — full formal model with OracleSafety, Termination, FitnessMonotone, CacheFreshInMeasure, EpochMonotone
- `SimulateCLI.tla`, `SimulateCLIHuman.tla` — models for fi-simulate CLI
- `spec_learner.py` — ML pipeline: analyze_gap → propose_refinement → validate_refinement → improve
- `gen_tlc_model.py` — generates `.tla` + `.cfg` for pact_loop's convergence math

### Not used / fake

| Gap | Status | Fix |
|---|---|---|
| **TLC actually runs** | ❌ `validate_refinement()` does LLM symbolic replay, not TLC. `tlc_matches_prediction` is always `None` | Add `subprocess.run(["java", "-jar", "tla2tools.jar", ...])` in `validate_refinement()`; set `tlc_matches_prediction` from TLC stdout |
| **TLC in CI** | ❌ No `java` in CI action | Add `uses: actions/setup-java@v4` + download `tla2tools.jar` in `ci.yml` |
| **spec_learner self-improves** | ❌ Needs ≥2 bad records in corpus to fire `improve()`; currently 1 record | Build corpus from other known pact bugs (see below) |
| **Loop failures → spec_learner** | ❌ When CEGIS "Tool loop exhausted", the failure is never recorded | In `pact_loop.py::_heal()`, on `ToolLoopExhausted`, call `spec_learner.analyze_gap()` with the stuck context |

**spec_learner corpus building**: Every known pact bug class should have a gap record: bare_except (solved), json_loads_unguarded (solved), sheaf_llm_unguarded false positive (solved), cache_opacity (solved). Next: add records for `optional_dereference` and `save_without_update_fields` — these are the two largest violation classes in the dogfood run and are NOT yet in the spec_learner corpus.

---

## graphify + TDA capability (graphify_graph.py, pact_tda.py)

### Current state

- `graphify-out/graph.json` — **populated**: 1360 nodes, 2378 links (955 `calls` edges, 797 `contains`, 315 `rationale_for`, 171 `method`, 92 `uses`, 44 `imports_from`, 4 `inherits`)
- `CallGraph.load()` — works: 612 function nodes indexed, 476 out-edges, 210 in-edges
- `pact_tda.py` — `score_violations()`, `neighborhood_edges()`, `_beta1()` all implemented and correct

### Wiring gap

`pact_loop._measure_tda()` calls `CallGraph.load(target)` but `target` is the project being analyzed (e.g., `~/src/future-agi`), not pact-standalone itself. For external targets there is no `graphify-out/graph.json` — so `topo_score` is always 0.0 in every fitness calculation.

**Fix**: Before running `_measure_tda()`, check if `target/graphify-out/graph.json` exists. If not, run graphify on the target (via `graphify <target>` CLI or the graphify skill). Only then score.

### networkx gaps (import_graph.py, graph.py)

| Function | What it enables | Status |
|---|---|---|
| `nx.betweenness_centrality` | Find structural bottlenecks — nodes that, if fixed, break the most violation chains | ❌ Not used |
| `nx.pagerank` | Rank violations by propagation risk (high-PageRank violators infect many callers) | ❌ Not used |
| `nx.minimum_spanning_tree` | Minimal repair tree — fewest edges to cut to isolate all violation clusters | ❌ Not used |
| `nx.k_core` | Dense violator cores — sub-graphs where violations are densely interconnected | ❌ Not used |
| `nx.girvan_newman` | Community detection across violation clusters | ❌ Not used |

**Highest-impact next**: `nx.pagerank` on the `calls` subgraph — rank violations by PageRank score of their enclosing function. Heal high-PageRank violations first (they have most transitive impact). Wire into `pact_loop._measure()` alongside `topo_score`.

---

## tree-sitter capability (ts_checker.py)

### Currently wired

- TypeScript / JavaScript AST parsing via `tree-sitter-typescript` and `tree-sitter-javascript`
- Detects: bare `catch`, empty catch, `JSON.parse` unguarded, `Promise` rejection unhandled

### Not wired

| Language | Grammar package | Target repos that need it |
|---|---|---|
| Go | `tree-sitter-go` | agentcc-gateway, vllm (some Go glue code) |
| Rust | `tree-sitter-rust` | vllm tokenizers, sglang |
| Python | `tree-sitter-python` | Would replace regex-based checker.py with proper AST |
| Java | `tree-sitter-java` | Spring boot services in external corpus |
| YAML | `tree-sitter-yaml` | CI/CD config drift detection |
| Bash | `tree-sitter-bash` | `set -e` / exit code checks in shell scripts |
| Dockerfile | `tree-sitter-dockerfile` | `RUN` exit code patterns |

**Highest-impact next**: Go grammar — agentcc-gateway is in this repo and has unchecked error returns; `go_checker.py` currently uses regex. Replace with tree-sitter for structural correctness.

---

## scan_github + auto_pr pipeline

### Currently wired

- `scan_github.py::scan_repo()` — searches GitHub code for pact violation patterns, returns raw hits
- `scan_github.py::search_repos()` — finds Python repos by star count / topic
- `scripts/auto_pr.py` — files PRs for hardcoded queue of repos

### Disconnected wires

| Gap | Fix |
|---|---|
| `scan_github` output never feeds `auto_pr` | `auto_pr.py` uses a hardcoded `QUEUE`. Make it read `corpus/scan_github_*.jsonl` files for next unfiled repo |
| `nightly-corpus.yml` output never persisted | The nightly workflow runs `scan_github` but doesn't commit results to corpus/ |
| Queue is exhausted | All hardcoded repos have been filed. Need dynamic queue from scan_github corpus |
| No deduplication with filed PRs | `auto_pr_state.json` tracks filed repos but `scan_github` doesn't check it before adding to corpus |

---

## spec_learner training corpus (corpus/spec_gaps.jsonl)

### Current state (1 record)

| gap_name | verdict | invariants discovered |
|---|---|---|
| `cache_opacity` | CATCHES_BUG | `CacheFreshInMeasure`, `EpochMonotone`, `StaleResultsExcluded` |

### Should have records for (but doesn't)

| Gap | Source bug | TLA+ invariant to add |
|---|---|---|
| `bare_except_swallowed` | bare_except in pact itself (historical) | `ExceptionMustPropagate` |
| `optional_dereference` | largest violation class (699 in future-agi) | `OptionMustBeCheckedBeforeUse` |
| `save_without_update_fields` | 820 violations in future-agi | `DjangoSaveHasUpdateFields` |
| `tool_loop_exhausted` | CEGIS loop runs out of rounds | `HealMustTerminateOrFail` |

---

## claude-agent.yml custom_instructions gap

The current `claude-agent.yml` instructions reference old module names:

```
# In claude-agent.yml:
- pact_sheaf.py: sheaf-cohomological LLM response checker (check_file, sheaf_summary)
- pact_cfg_proof.py: AST→CFG→Z3 proof engine (prove_loop_guard)
- pact_synth.py: synthesis pipeline (full_pipeline → fix + test + Z3 cert)
- failure_mode.py + fixer.py: original pact scanner and fixer
```

Missing from the agent instructions: `pact_loop`, `pact_interproc`, `pact_tda`, `spec_learner`, `scan_github`, `auto_pr`. The Claude agent in GitHub issues doesn't know about the convergence loop, TDA, or spec_learner. **Update `custom_instructions` to list all current tools.**

---

## Implementation queue (priority order)

1. **Expose `pact_loop` + `pact_tda` + `pact_sheaf` as MCP tools** — 3 new entries in `mcp_server.py::_TOOLS` + dispatch functions. Unblocks Claude Code calling the full loop.

2. **nx.pagerank → heal priority** — In `pact_loop._measure()`, compute PageRank of `calls` subgraph, sort violations by enclosing-function PageRank. 20-line change; immediate improvement to heal ordering.

3. **Wire scan_github → auto_pr dynamic queue** — In `auto_pr.py`, replace hardcoded `QUEUE` with a scan of `corpus/scan_github_*.jsonl`; skip repos in `auto_pr_state.json`. Queue never runs dry.

4. **Wire loop failures → spec_learner** — In `pact_loop._heal()`, on `ToolLoopExhausted`, call `spec_learner.analyze_gap(gap_context)`. Closes TLA+ learning loop.

5. **Make TLC actually run** — In `spec_learner.validate_refinement()`, add `subprocess.run(["java", "-jar", TLA2TOOLS, ...])` and parse output. Set `tlc_matches_prediction` from ground truth.

6. **z3.Optimize for minimum fix set** — In `pact_loop`, after `_measure()`, solve a set-cover instance: which k files cover the most violations? Apply heal in that order.

7. **RuleBasedStateMachine for pact_loop** — New test class in `test_loop.py` using `hypothesis.stateful`. States: measure, heal, check. Rules: `do_measure()`, `do_heal(oracle_ok)`, `do_check(new_v, new_f)`. Invariant: fitness never decreases when converging.

8. **Update claude-agent.yml custom_instructions** — Rewrite to list all current pact tools and their APIs. Add pact_loop, pact_interproc, pact_tda, spec_learner, scan_github, auto_pr.

9. **Go tree-sitter grammar** — Replace regex in `go_checker.py` with `tree-sitter-go` for structural correctness. First new language after TS/JS.

10. **Build spec_learner corpus** — Run `spec_learner.analyze_gap()` on `optional_dereference` and `save_without_update_fields`. Need 2+ CATCHES_BUG records before `improve()` activates.
