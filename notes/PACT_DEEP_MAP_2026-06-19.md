# Pact — Deep Architectural Mindmap

*Reference doc for JH and any AI collaborator working on pact. Companion to the plumbline deep map.*

---

## TL;DR (one paragraph)

Pact is a **structural verification tool for AI-written code** — it treats a codebase as a bridge, finds the load-bearing joints (cut vertices, model/call boundaries, async/await edges, optional-deref sites), and verifies contracts at those joints with a layered formal stack (Z3 Datalog + Z3 SMT + Semgrep + Mypy + Hypothesis + TLA+ + Halmos for Solidity + Lean as root-of-trust). The shipping engine targets **Python + TypeScript + Go** at scale (14,148 violations across 1,254 repos in the corpus; 12 upstream PRs merged), with a parallel **Solidity audit chain** (`audit.py` / `audit_in_place.py` / `hybrid_audit.py` / `broad_backtest.py`) that emits Halmos harnesses and uses an `intended()` precision gate to reject invariants the contract doesn't actually promise. Two autonomous-improvement loops already exist — `pact_loop.PactLoop.run()` (4-phase, fitness-driven, TLA-backed) and `co_improve.round_eval()` (gate/body attribution for Solidity) — both share `prompt_improve.improve_if_weak` and emit ADRs. The honest scope (per `ENGINE_ASSESSMENT_2026-05-30.md`) is **verifiable-local at ~85% precision** for the shipping engine; deep cross-frame understanding lives in JH's manual harness, not yet productized.

---

## Core architectural pattern

### Joints-not-functions verification

Pact's epistemology (from `CLAUDE.md`): a linter pattern-matches code text; pact understands code meaning **at structural joints**. The joints are the `ModelManifest`/`CallSite` pair (Django model + `Model.objects.create()` call), the `FunctionManifest`/`CallSite` pair (signature + invocation), the def-use chain (`defuse.TaintFlow` from source to sink across function boundaries), and the call-graph cut vertex (`reduce.py` finds articulation points). A bug at a joint is **a contract mismatch between two structural pieces**, not a pattern inside one of them.

### Plugin layer of FailureModes

`failure_mode.py` exposes 18 `FailureMode(name, description, check, file_check)` quads in `DEFAULT_MODES`. Each FailureMode is **declarative**: it says what constraint to check at call sites or files. `checker.check_codebase()` is generic — it iterates DEFAULT_MODES and runs each handler. **New constraints = new FailureMode instances, no checker-code changes.** This is the same pattern as a database query engine: declarative WHAT, generic HOW.

### Precision-ordered verifier cascade with proof certificates

Every `Violation` carries a `spec_id` indicating its proof source: `None` (AST heuristic) → `"semgrep:<rule>"` (structural pattern) → `"mypy"` (type-system confirmation) → `"z3:datalog"` (formally proved by Fixedpoint Datalog). Higher tiers cross-validate lower tiers; `check_codebase` deduplicates on `(file, line, context, call)` and **annotates AST violations with the highest-confidence proof source available**. Hypothesis adds a parallel tier: confirmed (0.95) vs unconfirmed (0.7) confidence on properties extracted by LLM.

---

## pact vs plumbline (the elephant in the room)

JH built both. They share an architectural template (LLM proposes + formal oracle decides + prompt files self-rewrite on grounded signal) but target fundamentally different bug economies.

| Axis | pact | plumbline |
|------|------|-----------|
| **Target language** | Python + TypeScript + Go (polyglot, AST/tree-sitter) | Solidity only (Foundry + Halmos) |
| **Bug model** | 18 syntactic/structural FailureModes + LLM-extracted behavioral contracts | Smart-contract financial invariants (conservation, no-zero-share, principal) |
| **Verifier stack** | Z3 Fixedpoint + Z3 SMT + Semgrep + Mypy + Hypothesis + TLA+/TLC + CrossHair + Lean (root) | Halmos (primary oracle) + forge + sol_score + Lean trust kernel |
| **Fitness signal** | Composite [0,1]: violation_rate 25% + heal_accept 20% + oracle 15% + find_confirm 15% + topo 10% + prompt 10% + sheaf 5% | Recall / precision vs PLANTED truth per corpus rung (clean ↔ buggy pair) |
| **Attribution** | `co_improve.py` confusion-matrix routing (gate vs body) | `_maybe_improve` rewrites weakest rung's prompt |
| **Loop orchestrator** | `pact_loop.PactLoop.run()` (1350 lines, 4-phase, TLA-spec-backed) | `flywheel.iteration()` (~30-line sweep, 235 lines total) |
| **Formal backing** | `.pact_loop/PactLoop.tla` auto-emitted, proves OracleSafety + Termination | Lean kernel (extracted from pact) backs FV-summarization mulDiv axioms |
| **Corpus persistence** | `corpus/spec_gaps.jsonl`, `corpus/auto_pr_state.json`, `.pact_stages/*.json` | `reps.jsonl`, `prompts/*.md` (versioned prompt files) |
| **Deployment surface** | CLI (`pact .`) + MCP server (`pact-mcp`, 5/8 tools wired) + GitHub Actions auto-PR + cron | Flask app at `localhost:5050` + CLI |
| **Market position** | "Structural verification for AI-written code" — 14,148 corpus violations, 12 PRs merged | "Grounded self-improving auditor" — FV-gated 4-layer architecture for scabench |

**Overlap**: both are CEGIS loops; both treat prompts as learned weights (`prompt_improve.improve_if_weak` is shared); both use `adaptive_harness.py` (each has its own copy of the same file). Cross-pollination is real and one-directional historically: **plumbline → pact** (per JH's memory `project_pact_recall_loop`, pact's `recall_loop.py` was inspired by plumbline's mutation-recall grounding).

**Orthogonality**: pact targets Python codebases at scale (auto-PR on 1,254 repos); plumbline targets Solidity audits with FV gate. Same architectural template, two different verifier stacks for two different bug economies.

---

## The 10 subsystems

### 1. CLI + Entrypoints (`cli.py`, `__main__.py`, `mcp_server.py`)
- **Purpose**: Unified entry for humans, CI, and agents.
- **Entry points**: `pact [DIR]` with 12+ subcommands (`fix`, `spec`, `metrics`, `adrs`, `intent`, `heal`, `find`, `context`, `loop`, `pipeline`, `preflight`, `reduce`), `pact-mcp` exposes 8 tools over stdio (5 wired, 3 dormant).
- **I/O**: dir + git base branch → violations (text/JSON), patches, ADRs, PR-comment bodies.
- **Depends on**: `extractor`, `checker`, `z3_engine`, optional `anthropic`, `tree-sitter`, `pydriller`.
- **Used by**: CI/CD (`--incremental main --strict`), pre-commit, GitHub PR integration, Claude Code via MCP.
- **Gotchas**: `audit.py` (flattened) vs `audit_in_place.py` (in-project) are SEPARATE chains — flatten breaks on solmate; in-place is robust but requires Foundry structure. `_changed_files_on_branch()` in `cli.py:36` powers `--diff`/`--incremental`.
- **Pattern**: subcommand delegation; precision gates everywhere; `KNOWN_UPSTREAM_VIOLATIONS` table ranks packages by leverage (violations × log(stars) × downstream).

### 2. Finders (`find.py`, `extractor.py`, `bugspec.py`, `enrich.py`, `defuse.py`, `disguise.py`)
- **Purpose**: Bug detection across two paths — AST-structural (extractor → checker) and LLM-proposed (find → Hypothesis confirm).
- **Entry points**: `find_violations(path, model, api_key, ...)`, `extract_from_codebase(root)`, `gather(root)` (intent), `build_defuse(source)` (taint).
- **I/O**: source + git + GitHub → `FailureEvidence` list + `IntentContext` (Tornhill metrics) + `TaintFlow` chains.
- **Depends on**: Python `ast`, `beniget` (def-use), Z3, Hypothesis, `pydriller` (optional), `gh` CLI (optional).
- **Used by**: `pact check`, `pact find`, `pact intent`, `pact pipeline`.
- **Gotchas**: max 8 LLM tool-use rounds; Hypothesis 30s timeout; intent coverage levels 0=inferred → 3=ADR-backed (1.5 added only if test signals exist); transitive taint follow capped at 5 hops with 0.8 confidence decay; mutable-default detection only flags actual mutations (append/extend/__setitem__).
- **Pattern**: regex is RARE — bug detection is semantic (AST + Z3 + beniget); `disguise.py` rewrites contracts via non-Claude LLM to camouflage bugs while preserving the PoC, ensuring discovery benchmarks measure recall on realistic code.

### 3. Checker + Semgrep + Mypy overlay (`checker.py`, `doctor.py`, `semgrep/*.yaml`)
- **Purpose**: Orchestrate AST + Z3 + Semgrep + Mypy + tree-sitter into a unified deduplicated Violation stream.
- **Entry points**: `check_codebase(root, modes)`, `check_codebase_incremental(root, changed_files, modes)`, `_run_semgrep(root)`, `_run_mypy(root)`.
- **I/O**: root + optional changed_files → sorted `list[Violation]` with `spec_id` proof certificates.
- **Depends on**: `extractor` (Python manifests), `z3_engine.PactEngine` (optional), `ts_checker` (optional), `semgrep` CLI (optional), `mypy` (optional).
- **Used by**: `cli.check`, `fix.py`, `heal.py`, `intent.py`.
- **Gotchas**: Duplicate function names (defined in >1 file) excluded from call-site checks (FP→FN tradeoff). Semgrep deduplicates by `(file, context, call)` ignoring line. Vendor dirs (`_vendor/`, `/vendor/`, `/site-packages/`) skipped. `noqa` respected by Semgrep + Mypy + AST file_checks (but NOT by call-site `.check()`). `dmypy` avoided in favor of plain `mypy` to prevent cross-directory cache leaks.
- **Pattern**: multi-layer cascade with `seen` set dedup; incremental BFS marks files dirty upward through call graph; `@lru_cache` on file_check results, cleared by `heal.py` between iterations.

### 4. Formal verification (`constraint_graph.py`, `encoder.py`, `contract_encoder.py`, `contract_templates.py`, `z3_engine.py`, `lean/SummaryObligation.lean`, `docs/tla/*.tla`)
- **Purpose**: Two Z3 modes for two problem classes — Fixedpoint Datalog for relational facts, SMT Solver for behavioral contracts.
- **Entry points**: `verify_contract(contract, function_source, ...)` (CEGIS), `PactEngine.violations()`, `analyze_function(file, qualname, ...)` (DAG topology), `render_z3_template(kind, params)`.
- **I/O**: Python source + NL contract + Z3 templates + Semgrep YAML + TLA+ specs → `ContractVerificationResult` (sat/unsat/unknown), constraint DAG analysis.
- **Depends on**: Z3 Prover, optional CrossHair, Semgrep CLI, Mypy, Lean (for `SummaryObligation.lean` only), TLA+ (for `docs/tla/` only).
- **Used by**: `pact check`, `pact pipeline`, `pact heal`, `pact intent`.
- **Gotchas**: **Two Z3 engines, same library** — `PactEngine.Fixedpoint` (Datalog) vs `contract_encoder.Solver` (SMT). `_PurityLifter` AST transformer replaces I/O calls with symbolic stubs so CrossHair can analyze impure functions. CEGIS loop iterates when Z3 finds SAT — LLM analyzes whether counterexample is genuine or encoding error.
- **Pattern**: Lean is **root-of-trust** for Solidity summaries (z3 hits exponential bit-blast wall at 16+ bits; Lean proves width-independently). TLA+ specs in `docs/tla/` formally specify the checker's expected behavior, not the analyzed code. Constraint DAG betweenness ranks load-bearing constraints structurally.

### 5. Adaptive loop / self-improvement (`pact_loop.py`, `co_improve.py`, `broad_backtest.py`, `fixer.py`, `prompt_improve.py`, `audit.py`, `hybrid_audit.py`, `adaptive_harness.py`)
- **Purpose**: Two autonomous loops — `PactLoop` (polyglot, fitness-driven) and `co_improve` (Solidity, gate/body attribution).
- **Entry points**: `PactLoop.run() → LoopResult`, `co_improve.round_eval(clean_src)`, `adaptive_harness.plan(name, src)`, `broad_backtest.run_broad(name, src)`, `compute_fitness(state, initial_violations)`.
- **I/O**: target + test_cmd + api_key + max_iters → LoopResult (PROVED_CLEAN / CONVERGED / STUCK / TIMEOUT) + `.pact_loop/PactLoop.tla` + ADRs + healed source.
- **Depends on**: `checker`, `pact_interproc`, `pact_sheaf`, `z3_engine`, `pact_tda`, `reduce`, `find`, `heal`, `fixer`, `invariant_agent`, `prompt_improve`.
- **Used by**: `pact loop <target>` CLI.
- **Gotchas**: **Oracle safety is a TLA+ invariant**: `patches_applied ⊆ oracle_passed` — patches never applied unless `test_cmd` confirms. **Revert-vacuity teeth** in `broad_backtest`: empty Halmos verdict (no PASS/FAIL) triggers repair feedback (early pact swallowed vacuity as fake "0/0" pass). **Grounded attribution in co_improve**: discriminating invariant rejected by gate is a GATE error, NOT a body error — the misattribution trap.
- **Pattern**: 4-phase iteration (MEASURE → HEAL → IMPROVE → CHECK); convergence on |Δfitness|<ε for 3 iters (not zero violations); STUCK gate at 0 patches for 2 iters; ADR auto-generation; `SpecGapRecord` seeded on tool-loop exhaustion.

### 6. Multi-language support (`go_checker.py`, `go/checker/main.go`, `ts_checker.py`, `ast_utils.py`)
- **Purpose**: Modular per-language readers feeding the unified `Violation` schema.
- **Entry points**: `run_go_checker(paths, binary)`, `check_ts_files(root)`, `find_enclosing_function(file, line)` (Python or TS via extension dispatch).
- **I/O**: `.py` + `.ts/.tsx/.js/.jsx` + `.go` files → unified `Violation` list with language-agnostic dedup key `(file, line, context, call)`.
- **Depends on**: Python `ast`, `tree-sitter-typescript` (optional wheels), Go toolchain or pre-compiled `pact-go` binary (optional).
- **Used by**: `cli.py` via `check_codebase`.
- **Gotchas**: Go integration is **OPTIONAL** — silently returns `[]` if binary unavailable; TS integration is **OPTIONAL** — gracefully skips with RuntimeWarning if tree-sitter wheels missing. **No interprocedural analysis across language boundaries** — Python-calls-TS-via-subprocess is invisible.
- **Pattern**: Go is a separate Go binary (`pact-go`) for distribution outside Python stack; Python wrapper converts JSON to `FailureEvidence`. Tree-sitter abstraction in `ast_utils.find_enclosing_function` dispatches by extension.

### 7. LLM integration + prompts (`prompts/*.md`, `llm.py`, `intent.py`, `heal.py`, `fixer.py`)
- **Purpose**: 5-step intent pipeline (gather → triage → understand → extract invariants → violations) with self-improving prompts.
- **Entry points**: `extract_project_intent(root, model, ...)`, `make_client(api_key)`, `fix_file(path, violations)`, `_call_with_tools(prompt, model, key, ...)`.
- **I/O**: project root + git + docs + GitHub → `ProjectIntent` (JSON + markdown) + healed files + improved prompts.
- **Depends on**: `anthropic` SDK, `enrich.py` (intent gathering), prompt templates `prompts/*.md`, Z3 (verification oracle), `pydriller` (optional), `gh` CLI (optional).
- **Used by**: `pact heal`, `pact intent`, downstream tools (import-linter, testing frameworks).
- **Gotchas**: Triage cached by file listing + model hash; per-module cache keyed on file content + model. `fixer.py` does NOT apply LLM-synthesized patches — only ~16 mechanical AST modes; LLM patches applied by `heal.py` via CEGIS. **Tool-use without truncation**: `read_file_lines` lets LLM read source on-demand; max 6 tool rounds. MCP backend in `llm.py` can delegate to host via `sampling/createMessage`.
- **Pattern**: **Self-improving prompts** — every prompt has an `_improve` variant scoring on 6 dimensions (specificity, groundedness, calibration, completeness, actionability, non-obviousness); if score < 8, prompt is auto-rewritten. Staged caching in `.pact_stages/` (auto-gitignored).

### 8. Corpus + specs + states (`corpus/auto_pr_state.json`, `corpus/spec_gaps.jsonl`, `corpus/batch_state.txt`, `specs/*.pact`, `states/roadblock_cases.jsonl`, `scripts/auto_pr.py`, `scripts/seed_corpus.py`)
- **Purpose**: Persistent data architecture — violation corpus, formal specs modeling checker behavior, autonomous PR state.
- **Entry points**: `pact_loop()`, `auto_pr.process_one(target, token)`, `auto_pr._scan_repo(repo_dir, repo_slug)`.
- **I/O**: target dir + test_cmd + `GITHUB_TOKEN` + `auto_pr_state.json` → updated state + `spec_gaps.jsonl` + ADRs in `docs/adr/` + PRs filed upstream.
- **Depends on**: Z3 (CEGIS verification), Anthropic API, GitHub API + `gh` CLI, pytest, TLA+ tools (TLC model checker).
- **Used by**: CI/CD pipelines, autonomous repair agents, formal verification pipeline.
- **Gotchas**: `auto_pr_state.json.filed` tracks repos NOT PR URLs (PR #37516 langchain auto-closed). `batch_state.txt` is a single integer (unclear semantics). `spec_gaps.jsonl` records may have `verdict: MISSES_BUG` meaning TLA+ refinement hasn't caught the pattern yet. Z3 verification in auto_pr only applies to `llm_response_unguarded`/`sheaf_llm_unguarded` modes.
- **Pattern**: **Specs as executable API contracts** — `specs/anthropic-messages.pact` and `specs/openai-chat.pact` document real production crash sites (OpenAI empty `choices[]`, Anthropic filtered responses) with safe patterns.

### 9. Documentation + project status (`CLAUDE.md`, `AGENTS.md`, `IDEAS.md`, `ENGINE_ASSESSMENT_2026-05-30.md`, `CHANGELOG.md`, `README.md`, `intent_pact_self.md`)
- **Purpose**: Capture design philosophy, capability corpus (wired vs fake), research ideation queue, and honest scope assessment.
- **Entry points**: reference material — no runtime invocation. AI collaborators read `CLAUDE.md` session-start checklist before any work.
- **I/O**: design decisions + agent capabilities + research backlog → AI-collaborator guidance + marketing narrative + version history + self-improvement queue.
- **Depends on**: Actual implementation files (`z3_engine.py`, `pact_interproc.py`, `reducer.py`, `extractor.py`, `checker.py`, `pipeline.py`, `heal.py`, `intent.py`, `enrich.py`), MCP server, prompt files, GitHub workflows, external tools.
- **Used by**: AI collaborators, GitHub workflows, intent self-improvement loop, PR reviewers, marketing/sales, future implementers.
- **Gotchas**: **Verification oracle discipline** (`halmos --ast` non-negotiable) load-bearing in `CLAUDE.md`. **ENGINE_ASSESSMENT reveals scope mismatch**: README "93% accuracy" is METHOD's number (JH's agents), shipping engine is `~85%` confirmed-local-only. **TLC still fake**: `validate_refinement()` does LLM symbolic replay, not subprocess. 3 MCP tools (pact_tda, pact_sheaf, pact_spec_learn) not exposed.
- **Pattern**: **Anti-drift check**: "Could a linter do this? If yes, don't implement." Every IDEAS.md item requires Z3/NetworkX/Hypothesis/TLA+/Graphify non-trivially. Wired-vs-fake inventory in AGENTS.md is the truth table for any session.

### 10. Examples + recent evidence (`examples/z3/`, `examples/langchain/`, `examples/future-agi/`, `examples/home-assistant/`, `examples/python/gan/`, `examples/solidity/`, `batch-*.log`, `seen_repos.txt`)
- **Purpose**: Concrete demonstration artifacts + evaluation corpus + batch-processing evidence.
- **Entry points**: `auto_pr.py` queue, `seed_corpus.py` SpecGapRecord seeder, findings reports per target.
- **I/O**: Git repo URLs → findings.md per target + `corpus-batch-*.jsonl` (raw) + `corpus-batch-llm-*.jsonl` (LLM-filtered) + `auto_pr_state.json` + `spec_gaps.jsonl`.
- **Depends on**: AST/Solidity parser, Z3, call graph builder, TLC, GitHub API, git infrastructure.
- **Used by**: `pact` CLI, evaluation dashboards, autonomous PR filer, spec learner CEGIS loop.
- **Gotchas**: JSONL not array (stream-load to avoid OOM). Batch log format changed mid-run (`batch-*.log` → `batch-llm-*.log`). Historical disk errors (`Errno 28`) in May 2026 logs not indicative of current state.
- **Headline findings**: Z3.py mutable_default_arg in quantifier constructors (28 sites, silent state corruption); Home Assistant 22,458 optional_dereference; LangChain 256 missing_await; future-agi 873 save_without_update_fields.

---

## Verifier stack

| Verifier | Bug class | Verdict types | Speed | Cost | Composes with |
|----------|-----------|---------------|-------|------|----------------|
| **Semgrep** (`semgrep/*.yaml`) | structural patterns (bare-except, llm-unguarded, json-loads-unguarded, optional-deref, timeout-not-set) | match / no-match | fast (~seconds) | free | AST (dedup by `(file, context, call)`), post-filtered by `_sibling_guarded`, `_guard_func_called_before`, noqa |
| **Z3 Fixedpoint (Datalog)** (`z3_engine.PactEngine`) | model_constraint (Django required-field violations) | violation witness from `get_answer()` DNF | medium | free | AST (annotates `spec_id="z3:datalog"`), only fires if AST flagged `model_constraint` |
| **Z3 SMT (Solver)** (`contract_encoder.verify_contract`) | behavioral contracts (ordering, lifecycle, accumulation, flag_invariant, nullable, conservation) | sat (counterexample) / unsat / unknown / encoding_failed | medium-slow (30s timeout) | LLM call for NL→Z3 translation | CEGIS loop with LLM refinement on SAT |
| **Mypy** (`_run_mypy`) | optional_dereference (union-attr, attr-defined) | type error / clean | medium | free | AST (confirms optional_deref where heuristics miss); annotates `spec_id="mypy"` |
| **Hypothesis** (`find._confirm_with_hypothesis`) | LLM-proposed properties | counterexample found (0.95 confidence) / no counterexample (0.7) | medium (200 examples, 30s timeout) | subprocess | confirms LLM finds from `find.py` |
| **Halmos** (Solidity, `audit.py` / `audit_in_place.py`) | financial invariants, reentrancy, access control | PASS / FAIL / VACUOUS (revert) | slow | symbolic execution | requires `forge build --ast`; revert-vacuity teeth in `broad_backtest` |
| **TLA+ / TLC** (`docs/tla/*.tla`, 12 specs) | loop liveness, oracle safety, abstraction gaps | property holds / violation | slow | TLC subprocess | **CURRENTLY FAKE** — `validate_refinement()` does LLM symbolic replay, not `subprocess.run(java -jar tla2tools.jar)` |
| **CrossHair** (`constraint_graph.analyze_function`) | symbolic dynamic testing | violation list / clean | medium | free | `_PurityLifter` makes impure functions analyzable |
| **Lean** (`lean/SummaryObligation.lean`) | mulDiv floor axioms, width-independent | theorem (proved) / open | slow (interactive) | offline | root-of-trust for Solidity summaries; Z3 can't reach |
| **NetworkX + sheaf cohomology** (`reduce.py`, `pact_sheaf.py`) | structural risk (cut vertices, Ȟ¹ rank, blast radii) | metrics (not verdicts) | fast | free | priority-orders heal targets via `_topology_priority` (PageRank + betweenness) |
| **LLM-fixer** (`heal.py`, `fixer.py`) | patch synthesis | applied / oracle rejected | LLM call + test_cmd | API cost | CEGIS gate — patches verified by Z3 + test_cmd before application |

---

## Data flow for one `pact .` run

Concrete call chain on a polyglot project:

1. **ENTRY** — `cli.py:main()` (line 752) parses argv, dispatches to `check` default or subcommand. For `--incremental main`, `_changed_files_on_branch(base)` (line 36) shells `git diff --name-only`.
2. **EXTRACTION** — `checker.check_codebase(root)` (line 67) calls `extractor.extract_from_codebase(root)`. AST walk of every `.py` produces `ModelManifest` (Django field constraints), `FunctionManifest` (signatures + required-arg flags), `CallSite` (provided_kwargs, positional_count, model_name).
3. **PER-LANGUAGE READERS** — checker fans out:
   - Python: iterate `DEFAULT_MODES` (18 FailureMode instances), run `mode.check(call_site, models, funcs)` per call site + `mode.file_check(path)` per file.
   - TypeScript: `ts_checker.check_ts_files(root)` via tree-sitter-typescript wheels (gracefully no-ops if missing).
   - Go: `go_checker.run_go_checker(paths)` invokes `pact-go` binary (or `go run` fallback), parses JSON, lifts to `FailureEvidence`.
   Each layer's `FailureEvidence` lifted via `_to_violation` (line 19) to unified `Violation` schema.
4. **Z3 DATALOG OVERLAY** — `checker.py:171` imports `z3_engine.PactEngine`, calls `engine.load(root)` then `engine.violations()`. Fixedpoint runs two stratified rules (`site_req` join, `violation = site_req AND NOT site_provides`). AST violations matching Z3 witnesses get `spec_id="z3:datalog"`; Z3-exclusive violations appended.
5. **SEMGREP OVERLAY** — `_run_semgrep(root)` (line 290) subprocess-calls `semgrep --config pact/semgrep/ --json root`. Python post-filters: `_sibling_guarded`, `_guard_func_called_before`, inline-ternary detection, noqa respect. Dedupes against AST via `(file, context, call)` ignoring line.
6. **MYPY OVERLAY** — `_run_mypy(root)` (line 536) runs `mypy --output json` on discovered source dirs; extracts `union-attr` and `attr-defined` errors → optional_dereference with `spec_id="mypy"`.
7. **RETURN** — sorted, deduplicated `list[Violation]` (file, line, call, missing, context, spec_id). CLI prints text/JSON, exits 1 on `--strict` if any violations, or pipes downstream to `fix.fix_file()` / `heal.heal_project()`.

`--diff` / `--incremental` shortcut at step 3: `check_codebase_incremental` (line 684) BFS-propagates dirtiness up the call graph; only runs `mode.check()` on dirty call sites; returns `skip_ratio` stats to stderr.

---

## Adaptive loop in pact (what already exists)

Pact has **two complete autonomous loops** that share substrate but don't share orchestration.

### Loop A — `pact_loop.PactLoop` (1350 lines, polyglot, fitness-driven)

- `measure()` (line 669) — 7 analyzers in sequence: `_measure_checker` (AST FailureModes), `_measure_interproc` (Z3 CHC), `_measure_sheaf` (Ȟ¹ rank), `_measure_z3` (Datalog), `_measure_tda` (β₁), `_measure_blast_radii` (NetworkX), `_measure_find` (LLM property finder).
- `heal()` (line 763) — CEGIS: `_topology_priority` (PageRank + betweenness) + `_z3_optimal_heal_order` (Z3.Optimize max-coverage) → `heal_project()` → oracle (`test_cmd`) gate.
- `compute_fitness()` (line 150) — composite [0,1]: violation_rate (0.25) + heal_accept (0.20) + oracle (0.15) + find (0.15) + topo (0.10) + prompt (0.10) + sheaf (0.05).
- Termination: `_converged` (|Δfitness|<ε for 3 iters), `_stuck` (0 accepted for 2 iters), `PROVED_CLEAN` (violations==0 AND sheaf_rank==0), `TIMEOUT`.
- `_record_heal_failure` (line 712) seeds `corpus/spec_gaps.jsonl` with `SpecGapRecord` on CEGIS exhaustion → trains TLA+ spec refinement.
- `generate_adr()` (line 844) auto-emits ADRs per significant iteration.
- `generate_tla_spec()` (line 950) writes `.pact_loop/PactLoop.tla` with OracleSafety + Termination invariants.

### Loop B — `co_improve.round_eval` (174 lines, Solidity-focused, attribution-routing)

- Per round: build Halmos scaffold → fill body → `audit.intended()` gate → run on clean contract → run on buggy contract → measure discrimination.
- **Confusion-matrix routing**:
  - gate REJECT + discriminates → improve gate prompt (`sol_invariant_intent`)
  - gate KEEP + proves-both + reward-dep → improve body (`sol_body_fill`)
  - gate KEEP + FP on clean → improve gate
  - BUILD_FAIL → structural, do NOT route to prompt
- Calls `prompt_improve.improve_if_weak(prompt_name, score=0.0, signal, agent._ask)`.
- **Misattribution trap**: discriminating invariant rejected by gate is a GATE error, NOT a body error. Per JH's global rule: "self-improvement fixes SEMANTICS, not STRUCTURE — structure belongs in the deterministic generator/macro."

### Shared substrate
- `prompt_improve.improve_if_weak` (both loops use it) — same prompt-rewriting primitive plumbline uses.
- `adaptive_harness.plan()` — shared with plumbline.
- `body_loop.py`, `body_catch_loop.py` — body-fill iteration (`co_improve.py` imports `B = body_catch_loop`).
- `broad_backtest.run_broad` — free-form harness generation with revert-vacuity detection (the "teeth" preventing fake PASS).
- `prompts/*.md` — every prompt is a file with `{{placeholders}}`; improvement = file rewriting; convergence = stable prompt.

### Comparison to plumbline
- `plumbline.flywheel.iteration` (160) ≈ `pact_loop.run` — but plumbline iterates over CORPORA (puppy-raffle, t-swap, thunder-loan, boss-bridge) with per-corpus recall/precision; pact_loop iterates over codebase scans with composite fitness.
- `plumbline.sol_flywheel.flywheel(rungs, iters)` ≈ `co_improve.main` — plumbline iterates over rungs (a curriculum) with `_score_rung`; co_improve iterates over rounds with discrimination signal.
- `plumbline.reps.jsonl` ≈ `pact.corpus/spec_gaps.jsonl` — both evidence corpora driving prompt evolution.

**The loops are already there. What's missing is the GLUE**: pact_loop and co_improve don't share state. co_improve runs only on Solidity; pact_loop runs on polyglot. No overlord routes by language.

---

## Connection diagram

```
                          ┌─────────────────────────────┐
                          │   cli.py / __main__.py      │
                          │   pact-mcp (5/8 wired)      │
                          └──────────────┬──────────────┘
                                         │
                          ┌──────────────▼──────────────┐
                          │  checker.check_codebase     │◀── extractor.py
                          │  (orchestrator)              │    (Python AST only)
                          └──┬─────┬─────┬─────┬─────┬──┘
                             │     │     │     │     │
                ┌────────────┘     │     │     │     └────────────┐
                ▼                  ▼     ▼     ▼                  ▼
       ┌────────────────┐  ┌─────────┐ ┌────────┐ ┌──────────┐  ┌────────┐
       │ FailureModes   │  │  Z3     │ │Semgrep │ │  Mypy    │  │  TS/Go │
       │ (18 declarative│  │ Fixed-  │ │YAML    │ │ types    │  │readers │
       │  constraint    │  │ point   │ │rules   │ │          │  │        │
       │  classes)      │  │ Datalog │ │        │ │          │  │        │
       └────────────────┘  └─────────┘ └────────┘ └──────────┘  └────────┘
                                         │
                            All emit Violation(spec_id=...)
                                         │
                                         ▼
                          ┌──────────────────────────────┐
                          │ fix.py (mechanical AST) +    │
                          │ heal.py (CEGIS + LLM patch)  │
                          └──────────────┬───────────────┘
                                         │
                          ┌──────────────▼───────────────┐
                          │   pact_loop.PactLoop.run()    │◀── pact_sheaf, pact_tda
                          │  (4-phase fitness-driven)     │◀── reduce.py (NetworkX)
                          │  TLA+ OracleSafety invariant  │◀── find.py + Hypothesis
                          └──────────────┬───────────────┘
                                         │
                          .pact_loop/PactLoop.tla    docs/adr/ADR-NNN-iter-M.md
                          corpus/spec_gaps.jsonl     corpus/auto_pr_state.json
                                         │
                                         ▼
                            ┌────────────────────────┐
                            │ scripts/auto_pr.py     │
                            │ (GitHub PRs upstream)  │
                            └────────────────────────┘

  Parallel Solidity track (co_improve loop):
  audit.py / audit_in_place.py → intent() gate → Halmos → broad_backtest revert teeth
                            │
                            ▼
                  co_improve.round_eval (gate/body attribution)
                            │
                            ▼
                  prompt_improve.improve_if_weak (shared with pact_loop)
                            │
                            ▼
                  prompts/sol_*.md (versioned, self-rewriting)

  Root of trust: lean/SummaryObligation.lean (mulDiv width-independent)
  Formal models: docs/tla/*.tla (12 specs; TLC subprocess STILL FAKE)
```

---

## Gaps + redundancies

### Gaps

- **TLA+ TLC integration is FAKE.** CHANGELOG v0.2 claims "four templates run TLC for real via subprocess." `ENGINE_ASSESSMENT_2026-05-30.md` confirms `validate_refinement()` does LLM symbolic replay, not `subprocess.run(java -jar tla2tools.jar)`. No `java` in CI. `docs/tla/tla2tools.jar` is in the repo. spec_learner records exist but can't truly self-verify.
- **3 MCP tools never exposed**: `pact_tda`, `pact_sheaf`, `pact_spec_learn`. Capability built and used in `pact_loop.measure()` — only MCP exposure missing.
- **spec_learner corpus dormant**: 1 record (cache_opacity). `improve()` needs ≥2 MISSES_BUG records to fire. Dormant until bare_except_swallowed, optional_dereference, save_without_update_fields seeded.
- **Honest scope mismatch**: Shipping `find.py` deliberately excludes cross-frame bugs (strategy-skip rule, max-5-per-file). Confirmed precision ~85%, unconfirmed ~50%. The "deep cross-frame understanding" README claim lived in JH's manual harness, not in productized engine.
- **`subcaseB_mgt30_split` analog risk** (from Rule30 project): an axiom only verified over a small range can be globally false. spec_learner doesn't catch this class of gap because TLA+ models aren't really executed.
- **No cross-language taint**: `extractor.py` only walks `.py`. Go/TS violations are local-only. A Python-calls-TS-via-subprocess interface is invisible to the Z3 Datalog overlay.
- **No `doctor.check` for halmos --ast discriminative behavior**: discipline documented in `CLAUDE.md` but enforced only by convention. A `doctor` check confirming halmos discriminates known-good vs known-bad contracts is missing.

### Redundancies

- **TWO autonomous loops** in pact-standalone — `pact_loop.PactLoop` (4-phase, 1350 lines, fitness-driven, TLA-spec-backed) AND `co_improve.round_eval` (4-round, gate/body attribution, 174 lines, Solidity-focused). Don't talk. Both use `prompt_improve`; orchestration diverges.
- **`body_loop.py` and `body_catch_loop.py`** — sibling Solidity-body-evolution loops; `co_improve` imports `body_catch_loop` as B. Unclear separation; likely iteration artifact.
- **5 GAN-style files** at top level: `gan.py`, `gan_py.py`, `gan_py_open.py`, `gan_subtle.py`, `gan_symbolic.py`. AGENTS.md/IDEAS.md don't disambiguate which is canonical.
- **5 Solidity harness paths**: `audit.py` (flattened), `audit_in_place.py` (in-project), `hybrid_audit.py`, `broad_backtest.py`, `adaptive_harness.py`. Converge on different points of precision/coverage frontier but share enough logic that consolidating reduces semantic surface area.
- **Z3 in three roles**: `z3_engine.PactEngine` (Fixedpoint, model_constraint), `contract_encoder.verify_contract` (SMT, behavioral), `constraint_graph.analyze_function` (DAG topology). None share a common z3-session abstraction; each rebuilds the solver.
- **pact-standalone (clean) vs pact (scratch with 257 batch logs)**: production snapshot and iteration scratch space diverging. Risk of evidence loss if wrong one is cleaned up.

### Overlap with plumbline (consolidation opportunity)

- `prompt_improve.improve_if_weak` is shared substrate — already unified.
- `adaptive_harness.py` exists in BOTH repos as separate copies. Should be a shared lib.
- `co_improve.round_eval` (gate/body attribution) is **more sophisticated than `plumbline._maybe_improve`** (which just picks weakest rung). Lift co_improve's confusion-matrix routing into plumbline.
- Evidence corpora (`spec_gaps.jsonl`, `reps.jsonl`, `auto_pr_state.json`) have no unified dashboard. ~100 lines of jinja2 would surface "which prompts improving, which corpora stuck, which TLA spec has most gaps."

---

## High-leverage next moves

Ranked by EV using existing infrastructure (per JH's calibrated-max-EV framing).

1. **WIRE the 3 dormant MCP tools** (`pact_tda`, `pact_sheaf`, `pact_spec_learn`) into `mcp_server.py`. Capability is built and used in `pact_loop.measure()`; only MCP exposure missing. **~30 lines, no new design.** Unblocks agent access to topology metrics. AGENTS.md flags this.
2. **MAKE TLC actually run.** Biggest false-claim in pact. `docs/tla/tla2tools.jar` already in repo. **~50 lines**: `subprocess.run(['java', '-jar', tla2tools.jar, '-config', cfg, tla])` with timeout. Makes the 12 `.tla` files load-bearing instead of decorative.
3. **SEED spec_learner corpus** with 2-3 more `MISSES_BUG` records (optional_dereference, bare_except_swallowed, save_without_update_fields). `improve()` activates at ≥2 records. Currently dormant after 1. **~20 lines via `scripts/seed_corpus.py`.** Unlocks self-improving TLA+ specs that already exist as templates.
4. **FUSE `pact_loop` and `co_improve` into one orchestrator.** Both share `prompt_improve`, both consume oracle verdicts, both emit ADRs. co_improve's confusion-matrix attribution is more sophisticated than pact_loop's heal-prompt rewrite. Lift co_improve's attribution INTO pact_loop's IMPROVE phase. **~100 lines.**
5. **ALIGN README with ENGINE_ASSESSMENT.** Shipping engine is `~85%` confirmed-local-only; README claims `93%` deep cross-frame. ENGINE_ASSESSMENT (2026-05-30) is honest. Update README scope language (or be explicit: "productized engine vs full method"). Calibrates outreach (per `feedback_calibrate_outreach`).
6. **DOGFOOD: nightly `pact intent analyze . --improve` on pact-standalone itself.** `intent_pact_self.json` EXISTS. CLAUDE.md contract: "violations ARE the queue." Cron updates the file + opens issues for new violations. Turns the project into its own customer. **No new code; one workflow file.**
7. **CONSOLIDATE 5 GAN files + 3+ audit chains** into ONE adversarial-discovery module with strategy params. Surface-area reduction = easier for contributors. Per JH's global rule: "Don't improve-then-intervene — build the STRUCTURE."
8. **ENFORCE halmos --ast as doctor check.** Highest-confusion oracle gap. `doctor.py` has 6 checks; add a 7th running a known-good and known-bad Solidity test, confirming halmos discriminates. **Zero new files; one decorated function.**
9. **ADD CrossHair as 5th overlay layer** in `check_codebase()`. `constraint_graph._PurityLifter` already built; CrossHair already used in `analyze_function`. Move into the main pipeline (AST → Z3 → Semgrep → Mypy → CrossHair) with `spec_id='crosshair'`. Already imported; just wire it in.
10. **BUILD a single corpus-evidence dashboard** (HTML or markdown) reading `reps.jsonl` (plumbline) + `spec_gaps.jsonl` (pact) + `auto_pr_state.json` (pact). Three different self-improvement loops, no unified view. **~100 lines jinja2.** Lets JH see at a glance which prompts are improving, which corpora are stuck, which TLA spec has the most gaps. Per JH's `user_interaction_insights`: persistent UI is the right answer for repeated questions.

---

## Anti-patterns to avoid

Specifically about pact + plumbline coexistence and pact internal duplication.

- **DO NOT build a third autonomous loop in pact.** Two already exist (`pact_loop` and `co_improve`). Fuse them; don't add a third. Per JH's "Build the STRUCTURE for self-improvement, don't improve-then-intervene" — adding orchestrators is intervening at the wrong layer.
- **DO NOT clone `adaptive_harness.py` again.** It already exists in both pact and plumbline as separate copies. Promote to a shared lib (or accept the duplication as deliberate and DOCUMENT it). Stop the third copy before it happens.
- **DO NOT duplicate `prompt_improve` improvements.** Both loops use the same primitive. Improvements (better scoring, better rewrite gates) should land ONCE.
- **DO NOT add a Solidity bug class to pact when plumbline already covers it.** pact's Solidity audit chain (`audit.py`, `hybrid_audit.py`) and plumbline's `sol_flywheel` overlap on intent. Decide: pact for *codebase-scale auto-PR* on polyglot, plumbline for *audit-quality FV-gated deep dive* on Solidity. Don't grow Solidity in pact past auto-PR scope.
- **DO NOT add Python AST checks to plumbline.** plumbline is Solidity. The temptation when pact's Python loop is slow is to "just check this one thing in plumbline." Resist. Keep the language split clean.
- **DO NOT claim TLC is real until subprocess is wired.** ENGINE_ASSESSMENT exposes this. Marketing drift here is the highest-cost lie because formal-verification claims are JH's competitive moat. Either wire TLC (move #2 above) or downgrade the claim.
- **DO NOT add MCP tools that wrap existing CLI subcommands without thinking.** 5/8 MCP tools wired; 3 dormant. The dormant 3 are right-sized (pact_tda, pact_sheaf, pact_spec_learn — capabilities not reachable from CLI). Don't add MCP for things that are already CLI-accessible.
- **DO NOT extend `pact-standalone` (clean) and `pact` (scratch) in parallel.** Decide which is canonical. Per JH's `feedback_full_dir_backup_before_delete`: untracked ≠ recoverable. The 257 batch logs in `pact/` are evidence; the clean snapshot in `pact-standalone/` is the shipping engine. If you must keep both, document the role of each at the top of each repo's README.
- **DO NOT add a new verifier layer until existing layers compose cleanly.** Z3 appears 3 times (Fixedpoint, SMT, DAG) with no shared session. The right move is a `z3_session.py` abstraction, NOT a 4th Z3 usage.
- **DO NOT add to `IDEAS.md` items that fail the anti-drift check.** "Could a linter do this? If yes, don't." Every item must use Z3/NetworkX/Hypothesis/TLA+/Graphify non-trivially. Drift here is fast and silent.
- **DO NOT report success without verification oracle confirmation.** CLAUDE.md hard rule: halmos without `--ast` silently runs stale artifact = fake PASS. Every "PROVED" must confirm the oracle ran YOUR target (`[PASS]` for your contract name, "Running N tests" not skipped). This was multi-session waste in the past.

---

*End of mindmap. Reference companion to `~/.claude/notes/PLUMBLINE_DEEP_MAP_2026-06-19.md`. Cross-link: both projects share `prompt_improve` and `adaptive_harness` — improvements to either propagate. JH's framing: "same architectural template, two different verifier stacks for two different bug economies."*