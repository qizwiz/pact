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
6. Run tests: `cd ~/src/ && ~/src/pact-standalone/.venv/bin/python -m pytest --import-mode=importlib --pyargs pact.test_fixer pact.test_checker pact.test_z3_engine pact.test_hypothesis_checkers pact.test_ts_checker pact.test_ts_fixer pact.test_loop pact.test_specgen pact.test_reduce -q --tb=short`
7. Must pass 521+ tests. Fix failures before committing.
8. Commit and push.

---

## CURRENT GAP SUMMARY (as of 2026-05-23)

Known intent_gap violations in pact itself (to be confirmed by self-analysis):
- `heal.py` docstring: "Verification oracle: Z3 + test suite" — actual: LLM rubric only
- `intent.py` pipeline step 4: "Violations — contradictions with module's own intent" — no formal verification
- `reduce.py`: NetworkX cut vertices computed but do not trigger contract extraction
- Hypothesis: present in test suite only, absent from user-code analysis pipeline
- Graphify rationale nodes: extracted but never fed to intent layer

**Priority order for next improvements:**
1. Z3 contract encoder — generalize `encoder.py` from Django fields to any `behavioral_contract: str`
2. NetworkX → intent trigger — when cut vertex found, automatically run intent on that file
3. Hypothesis from contracts — generate `@given` strategy from `behavioral_contract` text
4. Graphify rationale → intent — feed rationale node text as declared intent layer

---

## COMMANDS

```bash
# Tests (run from ~/src/, not ~/src/pact-standalone/)
cd ~/src/ && ~/src/pact-standalone/.venv/bin/python -m pytest --import-mode=importlib \
  --pyargs pact.test_fixer pact.test_checker pact.test_z3_engine \
  pact.test_hypothesis_checkers pact.test_ts_checker pact.test_ts_fixer \
  pact.test_loop pact.test_specgen pact.test_reduce -q --tb=short

# Lint (always in this order)
cd ~/src/pact-standalone && .venv/bin/ruff check --fix FILE && .venv/bin/black FILE

# Intent self-analysis (dogfood)
cd ~/src/pact-standalone && .venv/bin/python -m pact intent analyze . --out intent_pact_self.json --improve -v

# Heal from self-analysis violations
cd ~/src/pact-standalone && .venv/bin/python -m pact heal . --violations intent_pact_self.json --severity high -v

# Full structural check on pact itself
cd ~/src/pact-standalone && .venv/bin/python -m pact reduce . --top 20
```

## RULES

- `GITHUB_TOKEN=$(gh auth token)` env var only — never `--token` flag
- `~/src/pact-standalone` is source of truth
- `ruff check --fix FILE && black FILE` (in that order) on any .py edits
- Never use `Co-Authored-By: Claude` in commits to external repos
- Test command must be run from `~/src/` using `--pyargs pact.*`
- Use `~/src/pact-standalone/.venv/bin/python` for test command
- No new checker modes
