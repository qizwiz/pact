# ADR-012: Respect `# pragma: no cover` on except handlers as an escape hatch

## Status

Accepted

## Context

The `bare_except` checker already respects `# noqa` on the except line as a developer-supplied escape hatch. Corpus scans of pydantic/logfire (r20-9) surfaced a second standard Python annotation used for the same purpose: `# pragma: no cover`.

```python
try:
    target = urlparse(url).path
    span['attributes'] = {..., 'http.target': target}
except Exception:  # pragma: no cover
    pass
```

`urlparse()` can technically raise `ValueError` on malformed input but in practice never does on span attributes. The developer signals this with `# pragma: no cover` — the coverage tool is told the except branch is unreachable. pact was flagging it anyway.

A second bug was found simultaneously: `_is_probe_expr` returned `True` for bare `_ast.Attribute` nodes, causing `return url.path` to be classified as a "probe statement" (ADR-011 exclusion 2). This made any try block containing `return obj.attr` immune from flagging even when it shouldn't be.

## Decision

**Change 1 — `# pragma: no cover` escape hatch**: Extend the existing `# noqa` skip to also skip except handlers annotated with `# pragma: no cover` on the same line. Both annotations signal "this branch is intentional and not a bug."

```python
if 0 <= line_idx < len(source_lines) and (
    "# noqa" in source_lines[line_idx]
    or "# pragma: no cover" in source_lines[line_idx]
):
    continue
```

**Change 2 — `_is_probe_expr` precision fix**: Remove `_ast.Attribute` from the top-level True case in `_is_probe_expr`. Only `_ast.Name` (variable reference) and `_ast.Constant` (literal) are unconditionally pure. Attribute accesses (`url.path`, `obj.value`) are NOT guaranteed side-effect-free — they may be properties, descriptors, or C extensions. Attribute access that is pure introspection (e.g. `inspect.isfunction`) is already handled by the Call branch checking `func.attr in _PROBE_INSPECT_ATTRS`.

## Consequences

- pydantic/logfire `urlparse` guards annotated with `# pragma: no cover`: suppressed
- `return obj.attr` in a try body no longer incorrectly triggers the ADR-011 probe exclusion
- 5/5 regression tests pass: pragma excluded, bare-attribute flagged, issubclass probe excluded, real swallow flagged, nested handler excluded
- Existing ADR-011 exclusions are unaffected
