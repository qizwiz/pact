# ADR-022: Custom coroutine runner exclusion for `missing_await`

## Status

Accepted

## Context

`pact`'s `missing_await` checker flags any call to an `async def` function that is not preceded by `await`. The `_CORO_CONSUMERS_RE` pattern (ADR-005, ADR-018) excludes the standard library runners:

```python
_CORO_CONSUMERS_RE = re.compile(
    r"\b(asyncio\.run|asyncio\.create_task|asyncio\.ensure_future|"
    r"loop\.run_until_complete|executor\.submit|ThreadPoolExecutor|"
    r"ensure_future|create_task)\s*\("
)
```

When scanning `crewAIInc/crewAI` (`lib/crewai/src/crewai/utilities/file_store.py`), pact reported 7 `missing_await` violations — all of the form:

```python
def store_files(execution_id, files, ttl):
    _run_sync(astore_files(execution_id, files, ttl))   # flagged
```

`_run_sync` is a project-local coroutine runner that calls `asyncio.run(coro)` internally (with a fallback to thread-pool when an event loop is already running). The coroutine IS executed; `await` is simply replaced by a sync wrapper. These are **false positives**.

## Decision

Extend `_CORO_CONSUMERS_RE` to also match any call of the form `_run_sync(...)` and the broader pattern of functions whose name ends in `_sync` or `run_sync`:

```python
_CORO_CONSUMERS_RE = re.compile(
    r"\b(asyncio\.run|asyncio\.create_task|asyncio\.ensure_future|"
    r"loop\.run_until_complete|executor\.submit|ThreadPoolExecutor|"
    r"ensure_future|create_task"
    r"|_run_sync|run_sync|\w+_sync)\s*\("   # custom sync runners
)
```

Additionally, check whether the unawaited coroutine call is passed **as a positional argument** to any enclosing function call. If the coroutine is an argument (not a stand-alone expression statement), it is presumptively consumed by the caller and should be excluded from `missing_await` flagging.

### Argument-position heuristic

```python
def _is_coroutine_arg(source: str, line: int) -> bool:
    """Return True if the call at `line` appears as an argument to an outer call."""
    src_line = source.splitlines()[line - 1].strip()
    # Pattern: outer_func(...inner_async_func(...)...)
    # The line both contains a call and is not the outermost statement's call
    return bool(re.search(r"\w+\s*\(.*\w+\s*\(", src_line))
```

This heuristic eliminates `outer(inner_async())` patterns without requiring full type analysis of `outer`. It may over-exclude in cases like `list(gen())` where `gen` is async, but those are rare and the user can suppress with `# pragma: no cover`.

## Consequences

- crewAI's 7 false positives are eliminated
- Any project using a `*_sync()` wrapper to run coroutines will also be excluded
- The argument-position heuristic may under-flag real violations where a coroutine is accidentally passed to a non-runner (e.g., `print(my_async_func())`) — acceptable, as these are caught by Python's own `RuntimeWarning: coroutine 'x' was never awaited`
- Regression test added to `test_fixer.py`: `test_missing_await_skipped_inside_custom_sync_runner`

## Alternatives considered

**Type-inference approach**: use Jedi to verify that the enclosing function's parameter type annotation includes `Coroutine`. Rejected for this ADR — requires Jedi resolution at check time (not just fix time), adds latency to the core checker. Deferred to a future ADR when Jedi integration is extended to the checker layer.

**Explicit allowlist**: let users add `# pact: coroutine-runner` to mark functions as runners. Rejected — too much user burden. The `*_sync` naming convention is conventional enough to cover the common case automatically.

## Related

- [ADR-005](ADR-005-coro-consumers-frozenset.md) — original `_CORO_CONSUMERS` frozenset design
- [ADR-018](ADR-018-ast-enclosing-stmt-guard-placement.md) — `_CORO_CONSUMERS_RE` for fix-time exclusion
- crewAI corpus finding: `lib/crewai/src/crewai/utilities/file_store.py` lines 190, 202, 211, 226, 238, 247, 265
