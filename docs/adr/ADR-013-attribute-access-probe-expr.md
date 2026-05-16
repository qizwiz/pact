# ADR-013: Attribute access is a pure probe expression; only constant returns are probe statements

## Status

Accepted

## Context

ADR-012 removed `_ast.Attribute` from the top-level True case in `_is_probe_expr` to fix a bug where `return url.path` inside a try body was incorrectly classified as a probe statement (triggering the ADR-011 probe exclusion and suppressing a real bare_except violation).

That fix was correct in principle but too broad. It broke ADR-011's probe-exclusion for the opyrator pattern found in corpus batch r21-9:

```python
def is_compatible_type(type: Type) -> bool:
    try:
        if type.__origin__ is list and issubclass(type.__args__[0], BaseModel):
            return True
    except Exception:
        pass
    return False
```

The `if` test contains `type.__origin__ is list` — a `Compare` with an `_ast.Attribute` on the left. After ADR-012, `_is_probe_expr(type.__origin__)` returned `False`, so `_is_probe_expr(Compare)` returned `False`, so `_is_probe_stmt(If)` returned `False`, and the probe exclusion was not triggered. This produced a false positive on a clearly intentional probe pattern.

The root cause of the original ADR-012 bug was not that `_ast.Attribute` was in `_is_probe_expr` — it was that `_is_probe_stmt(Return)` was too permissive:

```python
# Old (broken): any expr in Return is a probe stmt
if isinstance(stmt, _ast.Return):
    return _is_probe_expr(stmt.value)
```

`return url.path` — an attribute read — passed this check because `_ast.Attribute` was in `_is_probe_expr`. But `return url.path` in a try body means "the try exists to guard the attribute read", so swallowing the exception IS losing information.

## Decision

**Change 1 — restore `_ast.Attribute` to `_is_probe_expr`**:

Attribute accesses (`type.__origin__`, `obj.value`, `self._x`) are pure reads with no observable side effects when used in compare/boolop expressions. They belong in `_is_probe_expr` alongside `_ast.Name` and `_ast.Constant`.

```python
if expr is None or isinstance(expr, (_ast.Name, _ast.Attribute, _ast.Constant)):
    return True
```

**Change 2 — narrow `_is_probe_stmt(Return)` to constant returns only**:

A `Return` is only a probe statement when it returns a constant (`True`, `False`, `None`). Returning a computed value or attribute (`return url.path`, `return obj.value`) means the try exists to guard that computation — suppressing the exception loses information.

```python
if isinstance(stmt, _ast.Return):
    return stmt.value is None or isinstance(stmt.value, _ast.Constant)
```

This correctly handles:
- `return True` / `return False` / `return None` → probe stmt (True)
- `return url.path` → not a probe stmt (False) → try-except-pass gets flagged
- `return some_var` → not a probe stmt (False) → flagged (variable may be the result of guarded computation)

## Consequences

- opyrator `type.__origin__ is list` probe pattern: correctly excluded (12 → 0 FPs on that pattern)
- ADR-012 `return url.path` test: continues to flag (1 violation)
- ADR-011 simple issubclass probe: continues to be excluded
- ADR-012 `# pragma: no cover` escape hatch: unaffected
- 5/5 regression tests pass
