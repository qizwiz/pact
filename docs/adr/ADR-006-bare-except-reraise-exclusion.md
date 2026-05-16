# ADR-006: Exclude `except: raise` from bare_except violations

**Status**: Accepted  
**Date**: 2026-05-15  
**Deciders**: qizwiz/pact corpus analysis

## Context

The `bare_except` checker flags two patterns:
1. `except:` — bare except without a type filter, catches `KeyboardInterrupt`, `SystemExit`, etc.
2. `except Exception: pass` — typed but silent swallow

During corpus scanning of jina-ai/serve (24k★), the checker flagged instances of:

```python
try:
    importlib.import_module(name)
except ModuleNotFoundError:
    not_python_module_paths.append(path)
except:
    raise
```

The second handler — `except: raise` — was being flagged as a bare_except violation. This is incorrect: a bare `except:` whose entire body is a lone `raise` statement (with no argument) re-raises the current exception unconditionally. Nothing is swallowed. The handler is semantically equivalent to removing the `except:` block entirely and exists only to distinguish exception types at this level of the call stack.

This pattern appears in:
- Exception-type routing (catch specific, re-raise the rest)
- Context manager protocol implementations (`contextlib.nested`)
- Frameworks that need to intercept `SystemExit` or `KeyboardInterrupt` and propagate them

## Decision

Add a guard in `_scan_file_bare_except()`: skip any `ExceptHandler` node where:
- `node.type is None` (bare `except:`)  
- `len(node.body) == 1`  
- `isinstance(node.body[0], ast.Raise)`  
- `node.body[0].exc is None` (bare `raise`, not `raise SomeError()`)

The `exc is None` condition is critical: `except: raise` re-raises the current exception (safe), while `except: raise ValueError(...)` transforms it (potentially masking the original, still flagged).

## Consequences

- **Reduced false positives** in production libraries that use exception-type routing (jina-ai/serve, celery/kombu, stdlib-compatibility shims).
- **No change to true positive rate**: `except: pass`, `except: log(); pass`, and `except: raise ValueError(...)` are all still flagged.
- **BareExcept.tla remains sound**: the spec's invariant is "critical exceptions always propagate" — this exclusion reinforces rather than weakens that property.
- **Regression test added**: `test_bare_except_reraise_not_flagged` in `test_checker.py`.

## Alternatives considered

1. **Require `# noqa`** — places burden on library authors to annotate correct code. Rejected.
2. **Flag all bare `except:` including re-raises** — maximises recall but at cost of precision. Rejected: jina-ai/serve had 24 of 25 bare_except violations from this pattern.
3. **Also exclude `except: raise SomeError`** — too broad; exception transformation can mask bugs. Rejected.
