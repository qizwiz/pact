# ADR-003: TLA+ as Semantic Layer for Constraint Specifications

**Status**: Accepted  
**Date**: 2026-05-15  

---

## Context

Each pact `FailureMode` is a constraint: a predicate over program execution
that must hold to avoid a class of bugs. These constraints need to be:

1. **Precise** — exactly what program states trigger the violation?
2. **Sound** — no false positives: compliant code is never flagged.
3. **Complete** — no false negatives: every violation is detected.
4. **Auditable** — the constraint is independently understandable.

Without formal specification, constraints live only in the Python checker code.
A reader wanting to understand "exactly when does `save_without_update_fields`
fire?" must trace through `_scan_file_save_without_update_fields()`, its
helper functions, the `_SAFE_SAVE_RECEIVER_KINDS` frozenset, the
`_new_object_save_lines()` cache, and the Z3 satisfiability call. This is not
auditable by a human unfamiliar with the codebase.

---

## Decision

**Every FailureMode has a corresponding TLA+ module in `docs/tla/`.**

The TLA+ module specifies:
- The **state space**: variables representing the lifecycle of the artifact
  being checked (model instance, exception handler, function signature, etc.)
- The **transitions**: actions that move between states (fetch, modify, save;
  raise, handle, propagate)
- The **temporal property**: the □/◇ formula that must hold

The TLC model checker verifies the property over all reachable states in a
representative finite model. This is not "just documentation" — TLC runs in
CI and would catch a regression where a property no longer holds.

### Example

`BareExcept.tla` specifies:
```tla
NoCriticalExceptionSilenced ==
    [](
        (swallowed ∩ BaseOnlyExceptions ≠ {})
        ⟹ handler_flagged
    )
```

"In every reachable state: if a BaseException-only exception (KeyboardInterrupt,
SystemExit) has been swallowed, the handler was flagged." This is the precise
semantic of the bare_except checker, independent of the Python implementation.

---

## TLA+ is the semantic layer, not the implementation

A key distinction: TLA+ does not drive the checker. The checker is a Python
AST scanner. TLA+ is the *specification* of what the checker should do.

This separation has value because:
- The checker can be re-implemented (tree-sitter, Rust, WASM) without breaking
  the spec
- The spec can be reviewed by someone who doesn't know Python
- Discrepancies between spec and implementation are bugs (TLA+ is the oracle)

The Z3 layer (`z3_engine.py`) handles satisfiability queries about specific
program states — "given these known kwargs, which required args are missing?"
Z3 operates at the level of individual call sites. TLA+ operates at the level
of the full FailureMode lifecycle. They are complementary, not redundant.

---

## Alternatives Considered

**Docstrings only** — English prose describing what the checker does. Rejected:
ambiguous, not machine-verifiable, tends to drift from implementation.

**Coq/Isabelle formal proofs** — Full mechanized proof that the checker is
correct. Too heavyweight: requires proving properties about Python ASTs and
the checker implementation itself. TLA+ model-checks representative models,
which catches the majority of specification errors without the full proof burden.

**Z3 for both layers** — Using Z3 for both the individual call-site checks
and the overall lifecycle. Rejected: Z3 is a SAT/SMT solver, not a temporal
logic model checker. It cannot express □/◇ properties over execution traces.

---

## Current coverage

| FailureMode               | TLA+ spec                        | TLC verified |
|---------------------------|----------------------------------|--------------|
| missing_await             | MissingAwait.tla                 | ✓            |
| save_without_update_fields| SaveWithoutUpdateFields.tla      | ✓            |
| bare_except               | BareExcept.tla                   | ✓            |
| optional_dereference      | (next)                           | —            |
| mutable_default_arg       | (next)                           | —            |
| llm_response_unguarded    | (next)                           | —            |

---

## Consequences

- New FailureModes require a TLA+ spec before being considered complete.
- The spec is written first (TLA+ → ADR → Z3 → Hypothesis → integration probe)
  to force precision before implementation.
- TLC model-checking runs as part of the docs build (or can be added to CI
  for the `docs/tla/` directory).
- The `docs/tla/Pact.tla` umbrella module captures the full checker
  as a composition of per-FailureMode modules.
