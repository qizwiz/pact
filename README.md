# pact

**Python AST Constraint Tool** — formal verification for every Python codebase.

pact finds structural bugs before they reach production: missing required arguments, unguarded `None` dereferences, bare `except` clauses, missing `await`, and more. It encodes your codebase as Z3 constraints and solves them in a single pass.

```
$ pact futureagi/
✗  pact: 1,756 violation(s)

  accounts/utils.py:220  user.save()
    .save() without update_fields re-writes every column

  sockets/session_manager.py:31  _heartbeat_loop()
    coroutine called without await — body never runs

  ...
```

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
# Scan a codebase
pact path/to/project/

# Only report violations in files changed since main
pact path/to/project/ --diff main

# Only analyze the dirty subgraph (callee changes propagate to callers)
pact path/to/project/ --incremental main --stats

# Emit results as JSON
pact path/to/project/ --json

# Suggest safe refactor targets (high violation density, low coupling)
pact path/to/project/ --suggest

# Full GitHub PR comment: call graph + reduction sequence + test coverage
pact path/to/project/ --pr-comment
```

## TLA+ spec synthesis

pact can generate a TLA+ specification skeleton from Django models and Celery tasks:

```bash
# Generate the 70% skeleton from AST extraction
pact spec gen futureagi/model_hub/models/dataset_eval_config.py
```

```tla
---------------------------- MODULE DatasetEvalConfig ----------------------------
EXTENDS Naturals, Sequences, FiniteSets, TLC

VARIABLES
  datasetevalconfigs  \* SET OF DatasetEvalConfig records

TypeInvariant ==
  /\ \A r \in datasetevalconfigs :
       /\ r.enabled \in BOOLEAN
       /\ r.debounce_seconds \in Nat
       /\ r.column_mapping \in STRING

DatasetEvalTemplateUnique ==
  \A r1, r2 \in datasetevalconfigs :
    r1 # r2 => <<r1.dataset, r1.eval_template>> # <<r2.dataset, r2.eval_template>>

INVARIANT TypeInvariant
INVARIANT DatasetEvalTemplateUnique
```

```bash
# Fill in the remaining 30% (liveness, domain invariants) via LLM
export ANTHROPIC_API_KEY=sk-...
pact spec complete futureagi/model_hub/tasks/auto_eval.py -o AutoEval.tla
```

The resulting `.tla` file is ready for [TLC model checking](https://github.com/tlaplus/tlaplus).

## What pact finds

| Failure mode | Example |
|---|---|
| `save_without_update_fields` | `user.save()` overwrites all columns |
| `optional_dereference` | `result = obj.get(k); result.field` without None check |
| `bare_except` | `except Exception: pass` silently drops errors |
| `missing_await` | `async_fn()` called without `await` |
| `required_arg_missing` | `Model.objects.create()` omits a required field |
| `mutable_default_arg` | `def f(x=[]):` — shared across calls |
| `format_arg_mismatch` | `"{} {}".format(a)` — too few arguments |
| `llm_response_unguarded` | `response.choices[0]` without length check |
| `unvalidated_lookup_chain` | `d[obj.get(k)]` — KeyError if get returns None |

Go support (via `pact-go`): `go_ignored_error`, `go_bare_recover`, `go_unchecked_assertion`, `go_goroutine_no_sync`.

## Mermaid call graphs

`--pr-comment` generates a full GitHub PR comment with:

- Violation call graph (color-coded by severity)
- Animated reduction sequence (shows which refactors eliminate the most violations)
- Test coverage graph (test → production function edges)

![pact call graph example](https://raw.githubusercontent.com/qizwiz/pact/main/docs/call_graph_example.png)

## CI integration

```yaml
- name: pact static analysis
  run: |
    pip install pact-tool z3-solver
    pact . --incremental --stats --strict
```

Or use the [GitHub Action](https://github.com/future-agi/future-agi/tree/main/.github/actions/pact).

## Architecture

```
extractor.py    AST visitor → ModelManifest, FunctionManifest, CallSite
checker.py      Z3 constraint solver → Violation list
z3_engine.py    Datalog (Z3 Fixedpoint) engine for whole-program queries
specgen.py      AST → TLA+ skeleton (70%)
speccomplete.py Anthropic API → fills TODO stubs (30%)
refactor.py     Refactor suggestion engine (violation density × coupling)
visualize.py    Mermaid call graph + reduction sequence renderer
go_checker.py   Python bridge to pact-go (Go AST checker)
cli.py          CLI entry point
```

## Contributing

pact is extracted from [future-agi/future-agi](https://github.com/future-agi/future-agi) where it was developed as a CI gate. All contributions welcome — the [issue tracker](https://github.com/qizwiz/pact/issues) is open.

The design philosophy: pact should be the formal verification layer that every AI developer adds to their CI without needing to know TLA+ or Z3. `spec gen` + `spec complete` + `tlc` = fully automated formal analysis from source code.

## License

MIT
