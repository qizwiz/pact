# ADR-016: Import statements are probe statements in exception-handling try blocks

## Status

Accepted

## Context

Corpus batch r22-5 (evaluation stars:>100) surfaced `trycua/cua` with 34 bare_except violations, all 100% bare_except. Inspection of the violations revealed a common pattern:

```python
def is_retryable(exc):
    try:
        import litellm.exceptions as _le

        if isinstance(
            exc,
            (_le.RateLimitError, _le.ServiceUnavailableError, _le.Timeout),
        ):
            return True
    except Exception:
        pass
    msg = str(exc).lower()
    return any(k in msg for k in ("timeout", "rate limit", "503"))
```

ADR-011 Exclusion 2 (probing try/except-as-control-flow) was designed for this but failed to fire. The try body has TWO statements:

1. `import litellm.exceptions as _le` — an `ast.Import` node
2. `if isinstance(exc, (...)): return True` — an `ast.If` node

`_is_probe_stmt` only handled `Return` and `If`. The `Import` statement returned `False`, so `all(_is_probe_stmt(s) for s in try_body)` was `False`, and the probe exclusion didn't trigger. The violation was incorrectly flagged.

The pattern is: **defensive import + probe check**. The try block tests whether an optional dependency is installed (`import litellm.exceptions`), then uses it for type-based introspection. If the import fails (`ImportError`/`ModuleNotFoundError`), the `except Exception: pass` intentionally falls through to a string-based fallback. No real error is silently swallowed.

## Decision

Extend `_is_probe_stmt` to return `True` for `ast.Import` and `ast.ImportFrom` nodes:

```python
if isinstance(stmt, (_ast.Import, _ast.ImportFrom)):
    return True
```

**Rationale**: An import inside a try block with `except Exception: pass` is always one of:
1. A defensive import testing whether an optional module is available — the import itself is the "probe"
2. A lazy import inside an introspection function (common in type-checking utilities)

In both cases, if the import fails, `except: pass` is the correct response. The import does not produce observable side effects in the sense pact checks (no DB writes, no HTTP calls, no cache mutations). A module's `__init__.py` may run, but its failure is exactly what the try block is guarding against.

## Consequences

- `trycua/cua` `is_retryable()` pattern: 0 violations (was incorrectly flagged)
- `bare import probe` (standalone `try: import X except: pass`): correctly excluded
- Real bare_except violations where `import` is incidental to a larger try body: still flagged, because the other statements in the try body will not all be probe stmts
- 6/6 regression tests pass (ADR-011 through ADR-016)
