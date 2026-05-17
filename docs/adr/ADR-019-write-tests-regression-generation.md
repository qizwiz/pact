# ADR-019: `--write-tests` — regression test generation alongside `pact fix`

## Status

Accepted

## Context

`pact fix --apply` inserts guards into source files but produces no verification artifact. Without a test, the guard can be removed in a future refactor without anyone noticing the regression. The guard is the fix; the test is the proof the fix is load-bearing.

For `llm_response_unguarded` violations, the bad input is concretely known: the OpenAI API returns `choices=[]` on content-filtered or rate-limited responses. A regression test can simulate this exactly by patching the response object.

## Decision

Add `--write-tests` flag to `pact fix`. When `--apply` and `--write-tests` are both set, write a `test_pact_guards_{stem}.py` file alongside each patched file.

The test generator (`test_writer.py`) operates on the already-patched source to find enclosing functions and generate:

```python
def test_{func}_guards_empty_{attr}():
    """pact regression: file.py:LINE — Guard: if not var.attr: return"""
    instance = MagicMock(spec_set=False)
    mock_response = MagicMock()
    mock_response.choices = []          # simulate content-filtered response
    instance.response = mock_response
    result = instance.{func}(MagicMock(), ...)
    assert result is None
```

The test proves: when the guard fires (empty list), the function returns `None` rather than raising `IndexError`. This is the minimal falsifiable claim.

## Design details

**AST enclosing function detection** — `_find_enclosing_function(source, line)` walks the AST, finds the tightest `FunctionDef`/`AsyncFunctionDef` spanning the violation line, and returns a `_FuncContext` with `(name, is_async, is_method, class_name, params)`. It uses `end_lineno` (Python 3.8+) to check containment.

**`__new__` bypass (abandoned)** — Initial design used `ClassName.__new__(ClassName)` to bypass `__init__`. Abandoned because it requires importing the actual class, which requires the full project on `sys.path`. Replaced with `MagicMock(spec_set=False)`, which works universally.

**MagicMock for all params** — `_dummy_call_args` generates `MagicMock()` for every non-`self`/`cls` param. This is explicitly a limitation: the test does not exercise real class behavior. It only proves the guard is reachable and returns `None` on empty input. Better: use `jedi.Script.infer()` to resolve param types and import real classes where possible. Tracked as future work.

**Dry-run parity** — Without `--apply`, `--write-tests` prints `# --- would write {path} ---` followed by the test source. This preserves the diff-only contract of dry-run mode.

**Deduplication** — `seen_tests: set[str]` prevents duplicate test functions when the same enclosing function has multiple violations at different lines. Test name is `test_{func}_guards_empty_{attr}`.

**Only `llm_response_unguarded`** — Only this mode generates tests. `missing_await` guards cannot be usefully tested with MagicMock alone (the coroutine must actually be awaited). This may be extended in future.

## Consequences

- Every `pact fix --apply --write-tests` run produces a falsifiable regression artifact
- Tests are minimal: they prove the guard path, not the full function semantics
- Tests require `pytest` and `pytest-asyncio` for async functions
- Test files are written next to source files, not in a test directory — may conflict with project conventions; users may need to move them
- Limitation: `MagicMock` for all params means tests cannot exercise real class invariants

## Alternatives considered

**Import-real-class approach**: use `importlib` to import the actual class, fall back to MagicMock. Rejected for this version — requires the project on `sys.path`, which pact cannot guarantee. Deferred to v2 with jedi/LSP integration.

**Property-based tests (Hypothesis)**: generate random inputs instead of fixed MagicMock. Rejected — pact's test writer runs at patch time, not development time. Fixed MagicMock is deterministic and auditable.

## Related

- [ADR-017](ADR-017-pact-fix-patch-generation.md) — `pact fix` patch generation
- [ADR-018](ADR-018-ast-enclosing-stmt-guard-placement.md) — AST enclosing statement detection
