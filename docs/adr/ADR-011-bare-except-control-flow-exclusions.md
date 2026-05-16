# ADR-011: Exclude `except Exception: pass` in nested last-resort handlers and probing try blocks

## Status

Accepted

## Context

The `bare_except` checker flags `except Exception: pass` as a silent error suppressor. Two corpus batches surfaced two categories of false positives:

**Pattern 1 — Nested last-resort handler** (found in pydantic/logfire r19-9):

```python
except Exception:          # outer — real error handler
    try:
        log_error()        # try to report the error
    except Exception:
        pass               # ← flagged, but this is intentional
```

The inner `except Exception: pass` is inside the *body* of an outer `ExceptHandler`. The outer handler already guards the error path. The inner pass is a "last resort" — if even logging the error fails, there is nothing left to do. Observability SDKs (logfire, OpenTelemetry wrappers) use this pattern pervasively to ensure instrumentation never crashes the caller.

**Pattern 2 — Probing try/except-as-control-flow** (found in ml-tooling/opyrator r19-9):

```python
def is_compatible_type(type: Type) -> bool:
    try:
        if issubclass(type, BaseModel):
            return True
    except Exception:
        pass               # ← flagged, but issubclass() raises TypeError on non-class args

    try:
        if type.__origin__ is list and issubclass(type.__args__[0], BaseModel):
            return True
    except Exception:
        pass               # ← same pattern
    return False
```

Here exceptions are flow control, not swallowed errors. `issubclass()` raises `TypeError` when passed a non-class argument; `inspect.getdoc()` and `__name__` access can raise `AttributeError` on unusual callables. The try body consists entirely of **pure introspection expressions** — `issubclass`, `isinstance`, `getattr`, `hasattr`, `inspect.*` calls. No side effects are lost. The function has an explicit `return False` fallback.

Both patterns were causing 100% false-positive rates in their respective repos (16/16 and 13/13 violations were FPs).

## Decision

Add two exclusions to `_scan_file_bare_except` in `failure_mode.py`:

**Exclusion 1 (nested handler)**: Skip `except Exception: pass` when its parent `Try` node is itself inside another `ExceptHandler`'s body. Detected via a parent-map built from `ast.iter_child_nodes`.

**Exclusion 2 (probing try block)**: Skip `except Exception: pass` when the parent `Try` body is ≤ 3 statements and every statement is a pure introspection expression — specifically, `Return` or `If` nodes whose expressions consist only of calls to `{issubclass, isinstance, getattr, hasattr, type, vars, dir}` or `inspect.*` methods.

The `real bug` case — a multi-statement try body with meaningful side-effecting calls (`requests.get`, DB writes, cache updates) — continues to be flagged.

## Consequences

- pydantic/logfire (16 bare_except FPs), ml-tooling/opyrator (13 bare_except FPs): eliminated
- Hypothesis tests (11/11) continue to pass — no regressions on confirmed real violations
- The exclusions are conservative: they only fire on narrow structural patterns, not on any `except Exception: pass` with a comment
- `noqa` annotation remains the escape hatch for cases the heuristic misses
