# Structural Intent Analysis: `pact-standalone`

**Generated:** 2026-05-26  **Model:** anthropic/claude-sonnet-4-5  **Modules:** 9

## Project Summary

pact is a graph-first structural verifier that finds violations at component boundaries (load-bearing joints) by treating code as a call graph where violations are path properties violating formal contracts, not AST pattern matches. It orchestrates Z3/TLA+/CrossHair/Hypothesis formal verification, LLM-driven patch synthesis with self-improving prompts, and topological analysis (SZZ bug density, Hassan entropy, Martin instability metrics) to identify which functions, if broken, collapse the most downstream behavior.

## Violations

**6 total** — 🟠 3 high, 🟡 3 medium

### `pipeline.py`

**Purpose:** run_pipeline (line 735) orchestrates multi-tool verification pipeline: reads intent JSON from pact intent analyze (line 749), prompts LLM to generate step plan (lines 762-766), executes Z3/TLA+/Hypothesis/heal tools in dependency order (lines 783-791), and auto-injects Hypothesis for Z3 violations (lines 793-813). Main entry (line 840) returns 1 if violations found, 0 if all verified (line 875), enabling CI/CD integration. _execute_z3 (line 177) calls verify_contract with optional pre-built Z3 encoding from inv_z3_index (lines 190-203), _execute_tla (line 350) generates TLA+ spec from resource obligation via _render_tla_spec (line 406) and runs TLC (line 312), _execute_hypothesis (line 242) calls stress_contract with optional Z3 counterexample seed (lines 268-272). Purpose: deterministically route intent findings to formal tools, report structured verification results, enable automatic healing for confirmed violations.

#### 🟠 `pipeline.py:0`
Contract claims single-call behavioral verification but implementation does not validate precondition (source_file exists) before calling verify_contract. If precondition violated, result is unreliable but still returned to caller.

#### 🟠 `pipeline.py:0`
Contract claims CEGIS-verified patches (counterexample-guided inductive synthesis requires oracle to verify candidate patches) but implementation allows apply=False mode where patches are not oracle-verified, violating verification guarantee.

#### 🟠 `pipeline.py:0`
OSError loading source produces warning but no exception. Empty source passed to verifiers leads to meaningless verification results (e.g., vacuous truth) counted as 'verified' in summary, violating soundness: 'verified' should mean contract checked, not 'source missing, assume true'.

#### 🟡 `pipeline.py:0`
Contract claims adversarial inputs derived from contract (via Z3 counterexample) but implementation allows z3_ce=None without warning, meaning Hypothesis may run without contract-guided seeding, violating 'adversarial inputs from contract' guarantee.

#### 🟡 `pipeline.py:0`
JSONDecodeError produces warning but no exception or error status in PipelineResult. Empty plan returned from LLM failure is indistinguishable from empty plan returned when no work needed, misleading user. Warnings may not be visible if stderr not monitored.

#### 🟡 `pipeline.py:0`
Liveness template defines EventualCompletion as <>(done=TRUE) which is trivially satisfiable (Complete action exists). TLC reports 'verified' but property does not constrain system behavior (e.g., no fairness, no stuttering prevention). Status='verified' misleads caller into thinking strong liveness property checked.
