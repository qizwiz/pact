# ADR-005: `_CORO_CONSUMERS` Frozenset Design for Missing-Await Detection

**Status**: Accepted  
**Date**: 2026-05-15  

---

## Context

The `missing_await` checker detects calls to `async` functions that are made
without `await`, creating a coroutine object that is never executed:

```python
async def fetch_user(uid): ...

# Bug: creates coroutine object, never runs it
result = fetch_user(uid)
```

However, some patterns are deliberately designed to consume coroutines without
`await`. A coroutine consumer is a function that accepts a coroutine as its
argument and handles scheduling/execution internally:

```python
asyncio.ensure_future(fetch_user(uid))   # schedules on event loop
background_tasks.add_task(fetch_user, uid)  # FastAPI BackgroundTasks
```

If the checker does not know about these consumers, it produces false positives
on correct code that uses task-scheduling patterns.

---

## Decision

**The set of known coroutine consumers is encoded as a `frozenset[str]` named
`_CORO_CONSUMERS` and a companion `frozenset[tuple[str,str]]` named
`_CORO_CONSUMERS_QUALIFIED`.**

```python
_CORO_CONSUMERS = frozenset({
    "ensure_future", "create_task", "gather", "run",
    "add_task", "spawn", "submit", "shield", ...
})

_CORO_CONSUMERS_QUALIFIED = frozenset({
    ("asyncio", "ensure_future"),
    ("loop", "run_until_complete"),
    ("background_tasks", "add_task"),
    ...
})
```

The check:
```python
if fname in _CORO_CONSUMERS:
    return []  # consumer present — not a missing-await violation
if (receiver, fname) in _CORO_CONSUMERS_QUALIFIED:
    return []
```

The `frozenset` design has four properties that matter:

### 1. O(1) lookup

`in` on a `frozenset` is O(1). The checker is called once per call site in
the codebase — for a 1M-line codebase, this is called ~50,000 times. A list
scan would be O(n) per check; frozenset is constant regardless of set size.

### 2. Immutability as a contract

`frozenset` cannot be mutated at runtime. This prevents accidental
modification by monkey-patching or framework code that might instrument
the checker. If the set needs a new member, the source code must change —
there is no runtime escape hatch. This makes the set auditable: the source
is the ground truth.

### 3. TLC verifiability

`MissingAwait.tla` models `_CORO_CONSUMERS` as the TLA+ constant `ConsumedSites`.
TLC verifies:

```tla
ConsumedSitesPermanentlyClean ==
    [](violations ∩ ConsumedSites = {})
```

The frozenset semantics (immutable, membership-testable) map directly to the
TLA+ set constant. If consumers were mutable (a list or dict), the TLA+ model
would need a variable, complicating the proof.

### 4. Extensibility via TLA+ spec, not just code

When a new consumer framework emerges, the process is:
1. Add the name to `_CORO_CONSUMERS` or `_CORO_CONSUMERS_QUALIFIED`
2. Verify the TLA+ spec still holds (the `ConsumedSites` constant in the
   config gains a new member; TLC re-checks)
3. Add a regression test

This is the five-step methodology in micro: the set change is a data change
verified by a formal model, not just a code change verified by tests alone.

---

## Alternatives Considered

**`set` (mutable)** — would allow runtime extension but breaks the
"source is ground truth" invariant and complicates TLC verification.

**Plugin registry pattern** — consumers register themselves at import time.
Flexible, but: import order matters, plugins can conflict, and the set is
no longer statically knowable at analysis time.

**AST pattern matching** — infer consumers from usage patterns in the codebase
being analyzed. Too slow (requires a pre-pass) and unreliable (cannot
distinguish a consumer from a function that happens to accept callables).

**Qualified names only** (`_CORO_CONSUMERS_QUALIFIED`, no bare names)** —
would require knowing the import alias (`loop.create_task` vs.
`event_loop.create_task`). The bare-name set handles the common case where
the alias is not statically determinable; qualified names add precision for
the most common frameworks.

---

## Consequences

- `_CORO_CONSUMERS` is the single authoritative list of coroutine-consuming
  patterns. No runtime extension. New consumers require a PR.
- The `gen_tlc_model.py` script extracts `_CORO_CONSUMERS` via AST and
  generates TLC config entries automatically — the spec and the code stay
  in sync without manual maintenance.
- `MissingAwait.cfg` is regenerated from source, not hand-edited.
