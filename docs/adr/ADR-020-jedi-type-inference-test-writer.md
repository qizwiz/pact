# ADR-020: Jedi type inference in `test_writer` for codebase-aware tests

## Status

Accepted

## Context

`test_writer.py` (ADR-019) generates regression tests for `llm_response_unguarded` guards. The current implementation uses `MagicMock()` for every parameter including `self`, because pact does not know the types at test-generation time. This produces tests that prove the guard path is reachable but cannot exercise real class invariants.

The concrete failure this causes: if `OpenRouterLLM.__init__` requires a real config object to set `self.openai`, a `MagicMock` instance will silently satisfy any attribute access but will not catch breakage from changes to the initialization contract.

pact already depends on `jedi` in precise mode (ADR-008) for cross-file resolution. Jedi's `Script.infer()` can resolve the type of `self` at any call site to a fully-qualified class name and import path — no new dependency.

## Decision

Extend `test_writer.py` with a `_resolve_class(source, path, line, col)` function that calls `jedi.Script.infer()` to resolve the type of `self` at the violation line. If resolution succeeds and the class is importable, use `ClassName.__new__(ClassName)` instead of `MagicMock(spec_set=False)`. Fall back to `MagicMock` on any failure.

```python
def _resolve_class(source: str, path: str, line: int) -> str | None:
    """Return 'module.ClassName' if jedi can resolve self at this line, else None."""
    try:
        import jedi
        script = jedi.Script(source=source, path=path)
        # col=8 = past 'self' in a typical method body
        names = script.infer(line=line, column=8)
        for n in names:
            if n.module_path and n.name:
                return f"{n.module_name}.{n.name}"
    except Exception:
        pass
    return None
```

Generated test setup changes from:

```python
instance = MagicMock(spec_set=False)
```

to:

```python
from futureagi.agentic_eval.core.llm.openrouter import OpenRouterLLM
instance = OpenRouterLLM.__new__(OpenRouterLLM)
```

with a try/except that falls back to `MagicMock` if the import fails at test runtime.

## Design details

**Column heuristic**: Jedi needs a column position to infer from. Column 8 (one indent level) is a reasonable default for `self` in a method body. This will fail for deeply-nested or unusual indentation but the fallback handles it gracefully.

**Import path derivation**: `jedi.Name.module_path` gives the absolute file path; `jedi.Name.module_name` gives the dotted module name. The import line is `from {module_name} import {class_name}`.

**`__new__` bypass rationale**: `ClassName.__new__(ClassName)` creates the instance without calling `__init__`, which may require dependencies (DB connections, config objects) that don't exist in a test environment. The test then sets only the specific attribute under test (`instance.response = mock_response`).

**Fallback chain**:
1. Jedi resolves class → use `__new__` + real import
2. Jedi fails / class not importable → `MagicMock(spec_set=False)` (current behavior)

This means the upgrade is transparent: existing generated tests still run, new tests are more specific when Jedi succeeds.

**jedi is optional**: if `import jedi` fails (pact installed without `[precise]` extra), the fallback fires silently. No new required dependency.

## Consequences

- Generated tests exercise real class structure when Jedi succeeds
- Import errors in generated tests surface real environmental issues (missing dependencies) rather than hiding behind MagicMock
- Column-8 heuristic is imprecise; a future improvement would use AST to find the exact column of `self` in the function signature
- `__new__` instances have uninitialized slots; tests must set every attribute the function under test touches (currently only the mocked LLM response attribute)

## Alternatives considered

**pyright JSON API**: `pyright --outputjson` provides richer type information than Jedi, including inferred generics. Rejected — requires pyright installed separately, adds a subprocess, and is slower. Jedi is already a dependency.

**Full LSP protocol**: spawn a language server (pylsp) and communicate via LSP JSON-RPC. Rejected — startup cost per test-generation run is too high. Jedi's Python API gives the same type resolution without a server.

**Type stubs only**: parse `.pyi` files for the class name. Rejected — stubs may not exist, and Jedi already reads them as part of inference.

## Related

- [ADR-008](ADR-008-precise-mode-container-isolation.md) — Jedi already used in precise mode
- [ADR-019](ADR-019-write-tests-regression-generation.md) — `--write-tests` design; this ADR extends it
