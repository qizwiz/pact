# pact

**Python AST Constraint Tool** — Z3 + TLA+ formal verification for every Python codebase.

I built pact because AI systems need formal guarantees, not just tests. Tests verify specific paths; formal methods verify all paths. pact makes that accessible: point it at any Python codebase and it finds structural bugs that type checkers, linters, and test suites miss — then generates a TLA+ spec you can model-check.

## What it finds

Two real-world benchmarks:

**[future-agi/future-agi](https://github.com/future-agi/future-agi)** — production AI observability platform, ~120k lines:

```
$ pact futureagi/
✗  pact: 9,280 violation(s)

  tracer/socket.py:55  [optional_dereference]
    'filter_config' assigned from .get() at line 54 but used without None check

  accounts/utils.py:220  [save_without_update_fields]
    .save() without update_fields re-writes every column;
    use save(update_fields=[...]) to prevent clobbering concurrent writes

  sockets/simulation_consumer.py:58  [missing_await]
    coroutine '_subscribe_redis' called without await —
    body never runs

  accounts/user_onboard.py:156  [unvalidated_lookup_chain]
    'original_metric_source_id' came from .get() but used as dict key
    without guard — KeyError if absent
```

Full findings: [`examples/future-agi/findings.md`](examples/future-agi/findings.md)

**[home-assistant/core](https://github.com/home-assistant/core)** — largest pure-Python open-source project, 87k stars, 14,096 files:

```
$ pact /tmp/ha-core/
✗  pact: 34,701 violation(s) in 14,096 files  (2m 43s)

  optional_dereference  22,458   StateType is pervasively nullable across 3,000+ integrations
  required_arg_missing  11,148   integration plugin pattern omits positional args at call sites
  missing_await          1,068   async device polling called without await — body never runs
```

Full findings: [`examples/home-assistant/findings.md`](examples/home-assistant/findings.md)

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
# Full scan
pact path/to/project/

# Only violations in files changed since main (fast CI mode)
pact path/to/project/ --diff main

# Graph-aware incremental analysis — callee changes propagate to callers
pact path/to/project/ --incremental main --stats

# Suggest safe refactor targets (high violation density, low coupling)
pact path/to/project/ --suggest

# Full GitHub PR comment: call graph + reduction sequence + test coverage
pact path/to/project/ --pr-comment

# JSON output for downstream tooling
pact path/to/project/ --json
```

## TLA+ spec synthesis

pact synthesizes a TLA+ spec from your Python source — mechanically extracting 70% from the AST, then using an LLM to fill in the remaining 30% (liveness, domain invariants).

```bash
# Generate skeleton from Django models + Celery tasks
pact spec gen futureagi/model_hub/models/dataset_eval_config.py
```

```tla
---------------------------- MODULE DatasetEvalConfig ----------------------------
VARIABLES
  datasetevalconfigs

TypeInvariant ==
  /\ \A r \in datasetevalconfigs :
       /\ r.enabled \in BOOLEAN
       /\ r.debounce_seconds \in Nat

DatasetEvalTemplateUnique ==
  \A r1, r2 \in datasetevalconfigs :
    r1 # r2 => <<r1.dataset, r1.eval_template>> # <<r2.dataset, r2.eval_template>>
```

```bash
# Fill in liveness + domain invariants via LLM
export ANTHROPIC_API_KEY=sk-...
pact spec complete futureagi/model_hub/tasks/auto_eval.py -o AutoEval.tla

# Model-check with TLC
java -jar tla2tools.jar -config AutoEval.cfg AutoEval.tla
```

The formal spec for pact itself lives at [`docs/tla/Pact.tla`](docs/tla/Pact.tla), verified under TLC in CI.

## Failure modes

| Mode | What it catches |
|------|-----------------|
| `optional_dereference` | `.first()` / `.get()` result used without `None` check |
| `save_without_update_fields` | `.save()` overwrites all columns, races concurrent writes |
| `bare_except` | `except Exception: pass` — silent error suppression |
| `missing_await` | Async function called without `await` — body never runs |
| `required_arg_missing` | Call omits a required argument |
| `unvalidated_lookup_chain` | `d.get(k)` result used as dict key without guard |
| `model_constraint` | Django model instantiation missing required field |
| `llm_response_unguarded` | `response.choices[0]` without length check |
| `mutable_default_arg` | `def f(x=[]):` — shared state across all calls |

Go support via `pact-go`: `go_ignored_error`, `go_bare_recover`, `go_unchecked_assertion`, `go_goroutine_no_sync`.

## CI integration

```yaml
- name: pact static analysis
  run: |
    pip install pact-tool z3-solver
    pact . --incremental --stats --strict
```

pact also runs against future-agi on every push (see [dogfood.yml](.github/workflows/dogfood.yml)).

## How it works

```
extractor.py    AST visitor → ModelManifest, FunctionManifest, CallSite
failure_mode.py FailureMode plugin layer (per-call-site + file-level checks)
z3_engine.py    Z3 Fixedpoint Datalog engine for whole-program queries
checker.py      Orchestration: extraction → Z3 → deduplication → Violation list
refactor.py     Suggestion engine: violation density ÷ caller coupling
specgen.py      AST → TLA+ skeleton (70% mechanical)
speccomplete.py Anthropic API → fills TODO stubs (30%)
visualize.py    Mermaid call graph + reduction sequence
go/checker/     Go AST checker (Go codebase support)
cli.py          Entry point
```

pact encodes each failure mode as a Z3 constraint over the call graph. The incremental engine performs BFS from changed files through the call graph, so only the dirty subgraph is re-analyzed — unchanged subtrees are cached.

## Formal verification layers

pact uses three verification layers on itself:

1. **Z3** — constraint satisfiability for per-call-site checks
2. **Hypothesis** — property-based tests for the extraction and analysis pipeline
3. **TLA+** — model-checked spec for the checker's termination, coverage, and monotonicity properties

```bash
# Run all three layers
pytest . -q                    # Z3 + Hypothesis
java -jar tla2tools.jar \
  -config docs/tla/Pact.cfg \
  -deadlock docs/tla/Pact.tla  # TLC
```

## Architecture decisions

Design rationale is in [`docs/adr/`](docs/adr/). Start with
[ADR-036](docs/adr/ADR-036-pact-formal-analysis-toolkit.md) — why Z3 Fixedpoint
over traditional dataflow analysis, and why TLA+ over property testing alone.

## License

MIT
