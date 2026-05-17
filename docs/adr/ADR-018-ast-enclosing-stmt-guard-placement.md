# ADR-018: AST-based enclosing-statement detection for `pact fix` guard placement

## Status

Accepted

## Context

The first version of `_fix_llm_unguarded` inserted the guard directly at `ev.line` — the line number reported by the violation. This worked for single-line expressions like:

```python
choice = response.choices[0]  # guard inserted here (correct)
```

But failed for violations inside multi-line call argument lists, e.g.:

```python
with parent.span(
    output=response.choices[0].message.content,  # ev.line = 3
):
    pass
```

Inserting at line 3 produced syntactically invalid Python — the guard became a keyword argument:

```python
with parent.span(
    if not response.choices:      # ← syntax error
        return
    output=response.choices[0].message.content,
):
    pass
```

The same pattern occurs with `.append()`, `.extend()`, and any other multi-line call.

Additionally, the first version of `_fix_missing_await` added `await` unconditionally to any call matching the violation, including cases where the coroutine was already being consumed by `asyncio.run()`, `executor.submit()`, etc.:

```python
executor.submit(asyncio.run, await coro_fn())  # WRONG: sync context
```

## Decision

### Guard placement: `_build_stmt_index`

Add `_build_stmt_index(source: str) -> dict[int, int]` that uses `ast.parse()` to build a mapping from every source line number to the start line of the innermost enclosing `ast.stmt` node.

For each violation line:
1. Look up `stmt_index.get(ev.line, ev.line)` → the enclosing statement start
2. Insert the guard before that statement, not at the violation line

This correctly handles the `with` / `append` / `extend` multi-line cases: all lines inside the call argument list map back to the statement's start line, and the guard is inserted there.

Multiple violations that map to the same enclosing statement are deduplicated: at most one guard per `(var, attr)` pair per statement.

### `missing_await` coroutine-consumer detection: `_CORO_CONSUMERS_RE`

Add a regex that detects when the call is passed directly to a coroutine consumer (asyncio.run, asyncio.create_task, executor.submit, etc.) by checking a 3-line context window around the violation. If the consumer pattern is found, the fix is skipped and the violation is added to `skipped`.

```python
_CORO_CONSUMERS_RE = re.compile(
    r"\b(asyncio\.run|asyncio\.create_task|asyncio\.ensure_future"
    r"|loop\.run_until_complete|executor\.submit|ThreadPoolExecutor"
    r"|ensure_future|create_task)\s*\("
)
```

This mirrors the `_CORO_CONSUMERS` frozenset used in the checker (ADR-005) — same concept, different mechanism (the fixer works on source text, not an AST walk of the surrounding function).

## Consequences

- Guard placement is now syntactically correct for all tested multi-line patterns
- `_build_stmt_index` adds one `ast.parse()` call per file — negligible cost
- `missing_await` no longer corrupts code in sync/executor contexts
- 3 new regression tests in `test_fixer.py` (lines 175–245) document both bugs and verify the fixes
- Fixer output is always syntactically valid Python — `ast.parse(result.patched)` is asserted in the two regression tests that previously produced broken code
