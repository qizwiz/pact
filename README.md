# pact

[![CI](https://github.com/qizwiz/pact/actions/workflows/ci.yml/badge.svg)](https://github.com/qizwiz/pact/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pact-tool)](https://pypi.org/project/pact-tool/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://pypi.org/project/pact-tool/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Formal analysis for the codebases AI builds.**

LLM-generated code has a signature failure profile: unawaited coroutines, unguarded nullable dereferences, silent exception swallowing, race conditions in ORM writes, unguarded LLM response reads. Tests don't catch them — they only run the paths you thought of. pact encodes each failure mode as a Z3 constraint over your call graph and finds them all, then generates a TLA+ spec you can model-check.

Python and Go supported. More languages follow the same pattern.

```bash
pip install pact-tool
pact .
```

```
✗  pact: 14 violation(s)

  tasks/auto_eval.py:89  [missing_await]
    coroutine 'trigger_evaluation' called without await —
    the evaluation never runs; this is a silent no-op

  model_hub/views.py:203  [optional_dereference]
    'api_key' assigned from .get() at line 201 but passed to
    LLM client without None check — AttributeError in production

  evaluations/tasks.py:156  [save_without_update_fields]
    .save() re-writes every column; concurrent request at line 201
    overwrites the status you just set

  utils/cache.py:44  [bare_except]
    except Exception: pass — the Redis timeout that caused
    your 3am incident is silently swallowed here
```

---

## Why pact?

The name started as "Python AST Constraint Tool" — a backronym built around the Z3 constraint engine at its core. It stuck because it means something: a pact is a formal agreement, a contract. That's exactly what pact enforces — the implicit contracts your code makes with itself that no linter checks and no test covers unless you already knew to write it.

The Python-specific origin is now a detail. The constraint engine works on any language with an AST.

---

## What makes pact different

Most linters catch style. Most type checkers catch types. pact catches **structural bugs** — the ones that only appear under concurrency, at scale, or when an LLM response is shorter than you expected.

Each failure mode is encoded as a Z3 constraint over your call graph, not a regex. That means:
- **Cross-file reasoning**: the bug in `tasks.py` that was introduced by a change in `models.py`
- **Context-aware**: `.get("key", default)` is safe; `.get("key")` is not — pact knows the difference
- **Formally grounded**: violations are Z3-satisfiable, not heuristic guesses

## Real-world findings

**[home-assistant/core](https://github.com/home-assistant/core)** — 87k stars, 14,096 files:
```
$ pact /tmp/ha-core/
✗  pact: 34,701 violation(s) in 14,096 files  (2m 43s)

  optional_dereference  22,458   nullable state pervasive across 3,000+ integrations
  required_arg_missing  11,148   plugin pattern omits positional args at call sites
  missing_await          1,068   async polling called without await — body never runs
```

**[langchain-ai/langchain](https://github.com/langchain-ai/langchain)** — 136k stars:
```
$ pact /tmp/langchain/libs/
✗  pact: 438 violation(s)

  missing_await    256   _astream() unawaited across every provider
  llm_response_unguarded   4   response.content[0] without length guard — crashes on content-filtered responses
```

**[future-agi/future-agi](https://github.com/future-agi/future-agi)** — production AI platform:
```
$ pact futureagi/
✗  pact: 6,931 violation(s)
```
See [`examples/future-agi/findings.md`](examples/future-agi/findings.md).

---

## Install

```bash
pip install pact-tool
```

For TLA+ spec generation (requires Anthropic API key):
```bash
pip install "pact-tool[llm]"
```

## Usage

```bash
# Scan your project
pact path/to/project/

# CI mode — only files changed since main
pact . --incremental main --strict

# Ranked refactoring targets (highest violation density × lowest coupling)
pact . --suggest

# Structural analysis: cycles, pass-through hops, fan-out hubs
pact . --reduce

# JSON for downstream tooling
pact . --json
```

## Failure modes

| Mode | What it catches |
|------|-----------------|
| `optional_dereference` | `.first()` / `.get()` result used without `None` check |
| `missing_await` | Async function called without `await` — body never runs |
| `bare_except` | `except Exception: pass` — silent error suppression |
| `save_without_update_fields` | `.save()` overwrites all columns, races concurrent writes |
| `unvalidated_lookup_chain` | `d.get(k)` result used as dict key without guard |
| `required_arg_missing` | Call omits a required argument |
| `mutable_default_arg` | `def f(x=[]):` — shared state across calls |
| `llm_response_unguarded` | `response.choices[0]` without length check |
| `model_constraint` | Django model created missing a required field |

Go support via `pact-go`: `go_ignored_error`, `go_bare_recover`, `go_unchecked_assertion`, `go_goroutine_no_sync`.

## CI integration

```yaml
- name: pact
  uses: qizwiz/pact/.github/actions/pact@main
  with:
    path: .
    incremental: "true"
    strict: "true"
```

Or directly:
```yaml
- run: pip install pact-tool z3-solver && pact . --incremental main --strict
```

## TLA+ spec synthesis

pact extracts a TLA+ spec from your Python source — 70% mechanical from the AST, 30% filled in by an LLM (liveness properties, domain invariants).

```bash
# Generate skeleton
pact spec gen path/to/models.py

# Fill in liveness + domain invariants
export ANTHROPIC_API_KEY=sk-...
pact spec complete path/to/tasks.py -o MySpec.tla

# Model-check with TLC
java -jar tla2tools.jar -config MySpec.cfg MySpec.tla
```

The formal spec for pact itself is at [`docs/tla/Pact.tla`](docs/tla/Pact.tla), verified under TLC in CI.

## Graph reduction

`--reduce` finds **structural fragility** — call cycles, pass-through hops, and fan-out hubs — ranked by `reduction_potential + violations × 0.5`:

```
$ pact . --reduce

⬡  TANGLE  payments.charge → payments.validate → payments.charge
     cycle of 3 — break to make this subgraph a DAG
     score=4.0  violations=4

⬡  PASSTHROUGH  api.route_and_forward
     1 caller → 1 callee — pure hop; inline to collapse 1 node + 2 edges
     score=3.5  violations=1
```

## How it works

```
extractor.py    AST → ModelManifest, FunctionManifest, CallSite
failure_mode.py FailureMode plugin layer (per-call + file-level checks)
z3_engine.py    Z3 Fixedpoint Datalog — whole-program queries
checker.py      Orchestration: extraction → Z3 → dedup → Violation list
refactor.py     Suggestion engine: violation density ÷ caller coupling
specgen.py      AST → TLA+ skeleton (70% mechanical)
speccomplete.py Anthropic API → fills TODO stubs (30%)
go/checker/     Go AST checker (Go codebase support)
cli.py          Entry point
```

pact encodes each failure mode as a Z3 constraint over the call graph. The incremental engine BFS-propagates changes through the call graph, so only the dirty subgraph is re-analyzed.

## Architecture decisions

Design rationale is in [`docs/adr/`](docs/adr/). Start with [ADR-036](docs/adr/ADR-036-pact-formal-analysis-toolkit.md) — why Z3 Fixedpoint over traditional dataflow, and why TLA+ over property testing alone.

## License

MIT
# pact call graph demo
