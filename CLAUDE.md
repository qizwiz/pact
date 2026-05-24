# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## THE VISION

pact is a **structural analysis tool**, not a linter. It treats software like a civil engineer treats a bridge: find the load-bearing joints, verify the behavioral contracts at those joints, stress-test them adversarially, and produce the minimal structural fix.

The mental model: "which configuration of popsicle sticks makes the strongest bridge?" Applied to software: which functions, if broken, collapse the most downstream behavior? Are their contracts formally verified? Do they hold under adversarial input?

**pact is a conductor, not a musician.** It orchestrates external tools. It does not reimplement them.

---

## ANTI-DRIFT CHECK (run before every improvement)

Before implementing anything, ask: **"Could a linter do this?"**

If yes — it is drift. Do not implement it. Linters detect surface patterns. pact detects structural failures.

A second check: **"Does this improvement use at least one external tool (Z3, NetworkX, Hypothesis, Graphify) in a non-trivial way?"**

If no — it is probably drift.

**Explicitly prohibited:**
- New checker modes (there are 16, that is enough)
- More semgrep rules for pattern matching
- More mypy rules as detectors
- Any feature that produces output a linter could produce

---

## EXTERNAL TOOLS (use, don't reimplement)

| Tool | Role in pact | Current status |
|------|-------------|----------------|
| **Z3** | Contract verification, counterexample generation, MaxSMT repair | Underused — only checks Django field constraints. Should verify any behavioral contract from intent output. |
| **NetworkX** | Cut vertices, betweenness, k-connectivity, SCCs | Computes topology but only sorts violations. Does not trigger contract extraction or adversarial testing. |
| **Hypothesis** | Adversarial input generation from contracts, stateful testing | Only tests pact's own detectors. Not used for user code at all. |
| **Graphify** | Call graph, community structure, rationale nodes | Annotates callers. Rationale nodes (free intent text) ignored entirely. |
| **Semgrep** | Taint analysis, dataflow tracking | Used as input source only — 2-3 rules. Taint analysis unused. |
| **Mypy** | Type information per variable | Used as input source only. Type data not fed to Z3. |
| **TLA+** | Temporal property verification | Not yet integrated. |

---

## THE CONNECTED PIPELINE (target state)

```
Graphify → NetworkX → find cut vertices
                          ↓
               pact intent → behavioral contracts at cut vertices
                          ↓
               Z3 encoder → verify contracts → counterexample (if violated)
                          ↓
               Hypothesis → adversarial inputs targeting counterexample
                          ↓
               pact heal → minimal structural fix (verified by Z3, not LLM)
                          ↓
               Structural risk report: "this joint, this contract, this input, this fix"
```

No tool in this pipeline produces output a linter could produce.

---

## THE SELF-IMPROVEMENT LOOP

pact finds its own violations via: `pact intent analyze . --out intent_pact_self.json --improve -v`

The violations in `intent_pact_self.json` ARE the improvement queue. This is self-steering: pact tells us what's wrong with pact.

**Every iteration:**
1. Run intent self-analysis (if `intent_pact_self.json` is older than 2 hours or missing)
2. Read `intent_pact_self.json` — find highest-severity violations
3. Filter for violations that require structural tools (Z3, NetworkX, Hypothesis)
4. Implement the fix for one violation
5. `ruff check --fix FILE && black FILE` on any .py edits
6. Run tests: `cd ~/src/ && ~/src/pact-standalone/.venv/bin/python -m pytest --import-mode=importlib --pyargs pact.test_fixer pact.test_checker pact.test_z3_engine pact.test_hypothesis_checkers pact.test_ts_checker pact.test_ts_fixer pact.test_loop pact.test_specgen pact.test_reduce pact.test_pipeline pact.test_heal -q --tb=short`
7. Must pass 559+ tests. Fix failures before committing.
8. Commit and push.

---

## CURRENT GAP SUMMARY (as of 2026-05-24)

Closed gaps:
- `reduce.py`: NetworkX cut vertices now trigger intent analysis via `--intent-trigger` flag
- `pipeline.py`: TLA+ specs now run TLC for real (all 4 templates verified, real verified/violated/unknown status)
- `pipeline.py`: `heal` step now calls `heal_project()` for real CEGIS-verified patches
- `heal.py`: oracle safety gap closed — `_autodetect_test_cmd` finds pytest/tox/make automatically; `oracle_warning` emitted when applying without oracle

Remaining gaps (Z3-confirmed via self-analysis 2026-05-24):
- `checker.py:599`: semgrep and mypy detectors unconditionally silenced when custom modes used (Z3 SAT confirmed)
- `_interproc_z3`: tainted_json IDB rule missing `not calls_sanitizer_G` check (Z3 SAT confirmed)
- `sheaf_summary`: h1_topological always 0, guard_deficit never surfaces topological gaps (Z3 SAT confirmed)
- `_improve_context_prompt`: silently drops prompt-rewrite failures when verbose=False (Z3 SAT confirmed)
- Hypothesis: present in test suite only, absent from user-code analysis pipeline
- Graphify rationale nodes: extracted but never fed to intent layer

**Priority order for next improvements:**
1. NetworkX → intent trigger — when cut vertex found, automatically run intent on that file
2. Graphify rationale → intent — feed rationale node text as declared intent layer
3. `pact pipeline` TLC execution — add Java/TLC invocation to actually verify TLA+ specs (currently generates spec but doesn't run TLC)
4. `pact pipeline --auto` flag — run pipeline automatically after `pact intent analyze` without separate invocation

---

## COMMANDS

```bash
# Tests (run from ~/src/, not ~/src/pact-standalone/)
cd ~/src/ && ~/src/pact-standalone/.venv/bin/python -m pytest --import-mode=importlib \
  --pyargs pact.test_fixer pact.test_checker pact.test_z3_engine \
  pact.test_hypothesis_checkers pact.test_ts_checker pact.test_ts_fixer \
  pact.test_loop pact.test_specgen pact.test_reduce pact.test_pipeline pact.test_heal -q --tb=short

# Lint (always in this order)
cd ~/src/pact-standalone && .venv/bin/ruff check --fix FILE && .venv/bin/black FILE

# Intent self-analysis (dogfood)
cd ~/src/pact-standalone && .venv/bin/python -m pact intent analyze . --out intent_pact_self.json --improve -v

# Heal from self-analysis violations
cd ~/src/pact-standalone && .venv/bin/python -m pact heal . --violations intent_pact_self.json --severity high -v

# Full structural check on pact itself
cd ~/src/pact-standalone && .venv/bin/python -m pact reduce . --top 20

# Pipeline: route intent findings to formal tools
cd ~/src/pact-standalone && .venv/bin/python -m pact pipeline intent_pact_self.json -v
```

## RULES

- `GITHUB_TOKEN=$(gh auth token)` env var only — never `--token` flag
- `~/src/pact-standalone` is source of truth
- `ruff check --fix FILE && black FILE` (in that order) on any .py edits
- Never use `Co-Authored-By: Claude` in commits to external repos
- Test command must be run from `~/src/` using `--pyargs pact.*`
- Use `~/src/pact-standalone/.venv/bin/python` for test command
- No new checker modes
