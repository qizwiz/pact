# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## THE VISION

pact is a **structural analysis tool**, not a linter. It treats software like a civil engineer treats a bridge: find the load-bearing joints, verify the behavioral contracts at those joints, stress-test them adversarially, and produce the minimal structural fix.

The mental model: "which configuration of popsicle sticks makes the strongest bridge?" Applied to software: which functions, if broken, collapse the most downstream behavior? Are their contracts formally verified? Do they hold under adversarial input?

**pact is a conductor, not a musician.** It orchestrates external tools. It does not reimplement them.

---

## DUAL OUTPUT: JSON + MARKDOWN EVERYWHERE

Every stage of the pipeline produces **two artifacts**:

- **JSON** — machine-readable, composable, pipes to the next stage, feeds programmatic tools
- **Markdown** — LLM-readable narrative, first-class artifact, not a pretty-print of the JSON

Markdown is not a view over JSON. It tells a story. JSON carries structure. Both are required.

**Why:** The pipeline must be LLM-drivable end-to-end. An LLM orchestrator reading
`pact enrich . --format markdown` should get everything it needs to reason about the codebase.
An LLM reading `pact intent analyze . --format markdown` should get a violation narrative it
can act on directly. Markdown is the language LLMs operate in natively.

**Implementation rule:** Every command accepts `--format json|markdown|both` (default: json).
Every data structure that has a `.render()` method produces its markdown artifact.
The JSON schema always includes a `"narrative"` or `"summary"` field with the markdown form
so downstream LLM tools don't need a separate call.

**The pipeline as LLM score:**
```
pact enrich . --format markdown          → context document for LLM
pact intent analyze - --format markdown  → violation narrative for LLM
pact z3 - --format markdown              → formal proof / counterexample narrative
pact adrs - --format markdown            → draft ADR documents
pact heal - --format markdown            → patch explanation + rationale
```

---

## THE DEEPER THESIS

There is a tension between real engineering (bridges, circuits — systems with formal structural constraints and failure modes) and software engineering (which historically lacks the same discipline).

**Graph theory + TDA + formal verification + intent gap analysis are the bridge between them.**

In the AI-writes-all-code era: AI produces locally-correct but globally-incoherent code. A human can no longer review every line. What they *can* do is verify structural integrity:
- **Graph theory**: find load-bearing joints, cut vertices, connectivity
- **TDA** (β₁, persistent homology): find topological invariants in call graphs and dependency structures
- **Formal verification** (Z3, TLA+): prove the contracts at critical joints hold
- **Intent gap analysis**: verify the built system matches stated intent

Together these are a civil engineer's structural review, applied to software.
pact is the tool for that review.

---

## INTENT IS STRUCTURAL TOO

The intent layer (`enrich.py`) follows the same structural principle as the code layer.

Issues, PRs, ADRs, commits, and files are not text blobs — they are **nodes in a graph**.
Edges connect them: a PR *modifies* a file, an issue *references* a module, an ADR *covers* a contract, a commit *explains* a change.

**The structural position of a file in the intent graph determines its coverage level:**
- High degree (many intent edges) = well-understood, violations are high-confidence
- Low degree (intent-isolated) = inferred-only, violations need more scrutiny
- Cut vertex in the intent graph = bridges multiple intent threads; changes here are high-risk

This means:
- Use **NetworkX** to build the intent graph, not lookup tables
- Use **Graphify** to serialize it so the knowledge graph is queryable
- Coverage is not binary (ADR or nothing) — it is a structural property of the graph
- A file with 3 open issues and 5 PR reviews has explicit intent even without ADRs

The spectrum: ADR (L3) → issue/PR mentions (L2) → commit bodies (L1) → inferred (L0)

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

## CURRENT GAP SUMMARY (as of 2026-05-25)

Closed gaps:
- `reduce.py`: NetworkX cut vertices now trigger intent analysis via `--intent-trigger` flag
- `pipeline.py`: TLA+ specs now run TLC for real (all 4 templates verified, real verified/violated/unknown status)
- `pipeline.py`: `heal` step now calls `heal_project()` for real CEGIS-verified patches
- `heal.py`: oracle safety gap closed — `_autodetect_test_cmd` finds pytest/tox/make automatically; `oracle_warning` emitted when applying without oracle
- `checker.py:599`: semgrep/mypy now run regardless of custom modes; de-duplication via seen set (commit 83b63af)
- `sheaf_summary`: guard_deficit now uses call-graph β₁ (tda_beta1_max) not site-graph β₁ (always 0) (commit 83b63af)
- `_improve_context_prompt`: RuntimeWarning now emitted regardless of verbose flag (commit 83b63af)
- `z3_engine.py`: UNKNOWN fixedpoint result now emits RuntimeWarning instead of silent proved_safe (commit 330a757)
- `extractor.py`: SyntaxError/OSError now emit RuntimeWarning instead of silent return (commit 330a757)
- `cli.py`: bare except → specific exceptions + RuntimeWarning; RuntimeError → Exception in _spec_cmd (commit 330a757)

Already closed (but not in list above):
- Graphify rationale → intent: rationale nodes wired into intent layer prompt (commit 4ee9393)
- `_interproc_z3` try_wraps guard: `Not(try_wraps_json_rel(_F))` added to tainted_json rule (commit 4146bd4)
- `_interproc_z3` api_key_unchecked: full Z3 taint chain added (commit 4146bd4)

Also closed (commits 0434e67, 570b57b, 6f1e663):
- `_interproc_z3` _BITS overflow: RuntimeWarning added when N > 65536 (commit 0434e67)
- `_interproc_z3` call resolution: conservative name→list[func_id] mapping prevents missed taint edges (commit 0434e67)
- `z3_engine.py` async LLM detection: _LLM_CALL_ATTRS expanded (acreate, agenerate, stream, __call__, etc.); await-unwrapping in visit_Assign for async assignments (commit 570b57b)
- `checker.py` duplicate-name false negatives: RuntimeWarning emitted listing excluded function names (commit 6f1e663)
- `pipeline.py` + `hypothesis_generator.py`: Hypothesis wired to user code — Z3 counterexample seeds Hypothesis search via `stress_contract(z3_counterexample=...)`; auto-injection ensures Hypothesis runs for every Z3 violation regardless of LLM plan; uses `sys.modules` lookup to avoid importlib module-identity split
- `pact_interproc.py`: `_interproc_z3` call resolution upgraded to file-qualified `stem::func` keys — explicit imports resolve to the specific source file; same-file definitions preferred over cross-file; conservative fallback preserved. Eliminates false-positive taint edges when unrelated same-named functions exist in other files.

Remaining gaps:
- No known structural gaps in the connected pipeline. Run `pact intent analyze . --out intent_pact_self.json --improve -v` for the current dogfood queue.

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

## SESSION START CHECKLIST

Before writing any code, do these three things:

1. **Check CI** — `gh run list --limit 5`. If red, fix before anything else.
2. **Audit reduce.py vs CLI** — `grep -n "^def [a-z]" reduce.py` vs `grep -n "argv\[0\]" cli.py`. Any public function not reachable from a subcommand is an orphaned capability. Wire it or note it.
3. **Check dogfood freshness** — `ls -la intent_pact_self.json`. If older than 2 hours, run `pact intent analyze . --out intent_pact_self.json --improve -v` before proceeding. The dogfood queue is the improvement queue.

---

## MCP SERVER NOTES

**Claude Code 2.1.153 does not support `sampling/createMessage`.**
The client's `initialize` capabilities are `{"roots": {}, "elicitation": {}}` — no `"sampling"`.
LLM tools (`pact_heal`, `pact_find`, `pact_context`, `pact_loop`, `pact_spec_learn`) work via
MCP only when the host supports sampling. Until then, run them via CLI with `ANTHROPIC_API_KEY`.
API billing resets 2026-06-01. The zero-LLM tools (`pact_topology`, `pact_metrics`,
`pact_z3_verify`, `pact_check`, `pact_sheaf`, `pact_tda`) work fully via MCP today.

---

## HARD-LEARNED PATTERNS

**LLM output quality = evidence quality, not prompt quality.**
Generic ADR output ("add behavioral contracts") means the evidence block is missing topology. Before tuning any prompt, print the exact dict reaching the LLM. If callers/callees/betweenness are absent, fix evidence collection upstream. Give the model the graph, not just the node list.

**Argparse defaults must be `None` for any arg that participates in an env var chain.**
`default="claude-haiku-..."` silently overrides `PACT_LLM_MODEL` because argparse fills it even when the flag is absent. `None` means "not set" — let `resolve_model()` / `resolve_key()` own the resolution. This applies to `--model` and `--api-key` in every subcommand.

**Filter first, `--top N` last.**
`compute_metrics(root, top_n=N)` slices before filters run, making `--top 5 --zone pain` return fewer than 5 results. Always: compute all → apply predicates → `results[:args.top]`. N means "N results after all filters."

**Disk-scanning functions are authoritative counters — never add offsets.**
`_next_adr_number()` scans the filesystem. After writing a file, re-scan; the disk already advanced. Never compute `_next_adr_number() + len(written)` — that double-counts.

**Loop termination counters must increment unconditionally.**
`if p: count += 1` with `count >= top_n` as exit condition → infinite loop on persistent failure. Increment always; use a second counter for successes if needed.

**Optional deps need `pytest.importorskip`, not silent failures.**
Tests for gudhi/optional heavy deps pass locally, fail CI. Every test that requires an optional dep must start with `pytest.importorskip("dep")`. Check the CI install manifest before writing such tests.

**`D=1.0` is noise for `Ca=0, Ce=0` modules.**
The R.C. Martin formula gives D=1.0 for any uncoupled module — mathematically correct, architecturally meaningless. Filter to `Ca+Ce > 0` before surfacing zone-of-pain results. Isolated scripts aren't painful; they're just disconnected.

---

## RULES

- `GITHUB_TOKEN=$(gh auth token)` env var only — never `--token` flag
- `~/src/pact-standalone` is source of truth
- `ruff check --fix FILE && black FILE` (in that order) on any .py edits
- Never use `Co-Authored-By: Claude` in commits to external repos
- Test command must be run from `~/src/` using `--pyargs pact.*`
- Use `~/src/pact-standalone/.venv/bin/python` for test command
- No new checker modes
