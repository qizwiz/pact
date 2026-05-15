# ADR-001: Graph-First over Pattern-First Architecture

**Status**: Accepted  
**Date**: 2026-05-15  

---

## Context

Static analysis tools fall into two camps:

**Pattern-first**: tools like pylint, ruff, and semgrep match textual or AST
patterns. The checker for `bare_except` finds every `except:` node. Fast and
auditable, but fundamentally blind to program structure — they report
*where a pattern occurs*, not *why it matters*.

**Graph-first**: code is a graph. Functions are nodes; calls are edges. A
violation is a path property — whether a constraint holds over every path
from a source to a sink. This is not a new idea: security analysis (taint
tracking), compiler optimization (dataflow), and formal methods (reachability)
all use it.

pact was originally written pattern-first. The checkers scanned files, matched
AST nodes, and emitted line:col pairs. This worked, but created a problem:
every new failure mode required a new bespoke scanner. The checkers were not
composable. There was no way to ask "is this path safe?" only "is this pattern
present?"

---

## Decision

**pact's primary data structure is a call graph, not a file tree.**

Formally:

```
G = (V, E)  where
  V = {FunctionManifest(name, file, async, decorators, ...)}
  E = {CallSite(caller, callee, file, line, ...)}

violations(G) = { p ∈ paths(G) : ¬C(p) }
```

Where `C` is a constraint — a predicate over paths in `G`.

Each FailureMode is a constraint on paths, not a pattern over AST nodes.
The AST scanner populates `G`; the constraint checkers query `G`. The two
layers are separate and independently testable.

Consequences:

1. **Reducibility** — `reduce.py` applies SCC contraction, dead-node pruning,
   and transitive reduction to `G`. The reduced graph preserves reachability
   of violations while being far smaller. This makes the Mermaid visualization
   tractable for large codebases.

2. **Composability** — constraints can reference each other. `llm_response_unguarded`
   asks "is there a path from an LLM response source to an unguarded use?" — this
   is a reachability query, not a pattern match.

3. **Explainability** — violations come with call paths, not just file:line.
   The `--pr-comment` output shows the exact call chain that leads to the bug.

4. **Completeness** — TLA+ specifications (in `docs/tla/`) model path constraints
   as temporal properties. TLC verifies that the checker is sound and complete
   over all reachable graph states.

---

## Alternatives Considered

**SARIF output format** was evaluated as a way to integrate with IDE tooling
and GitHub's code scanning alerts. Rejected because SARIF is fundamentally
file:line:col-indexed — it flattens the graph to a location. pact's primary
insight (the bug is a path property, not a point property) cannot be expressed
in SARIF without losing the structural context. Mermaid PR comments preserve
the graph and are readable by humans without tooling install.

**Pattern-only** (keeping the current scanners, adding no graph layer) was
the zero-cost option. Rejected because the corpus scan showed that ~40% of
false positives arose from missing structural context: a `save()` call flagged
on a `form.save()` receiver, an `except:` flagged inside a `__del__` method
where swallowing is intentional. Pattern-only tools cannot express these
exclusions without accumulating ad-hoc special cases.

---

## Consequences

- Every new failure mode must be expressible as a constraint on `G`.
- The `checker.py` → `failure_mode.py` → `encoder.py` pipeline maintains this
  invariant: the encoder builds `G`, the failure modes query it.
- File-scoped modes (`file_check`) are an optimization — they avoid building
  full graph for single-file violations — but must be equivalent to the
  graph-based check.
- The graph representation is the ground truth. Test coverage is measured
  against path coverage in `G`, not line coverage in the checker.
