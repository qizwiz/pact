# pact findings in LangChain

[langchain-ai/langchain](https://github.com/langchain-ai/langchain) is the most widely used
Python framework for building LLM applications: 136k stars, 2,478 Python files across
`libs/core`, `libs/langchain`, and partner integrations (OpenAI, Anthropic, Ollama, Groq,
HuggingFace, Qdrant, and 20+ others).

## Summary (2026-05-13)

```
$ pact /tmp/langchain-core/libs/
✗  pact: 438 violation(s) in 2,478 files
```

| Mode | Count | What it catches |
|------|------:|-----------------|
| `missing_await` | 256 | Async coroutine called without `await` — body never runs |
| `required_arg_missing` | 121 | Call omits a required positional argument |
| `optional_dereference` | 43 | `.get()` result used without `None` check |
| `bare_except` | 9 | `except Exception: pass` — silent error suppression |
| `llm_response_unguarded` | 4 | `response.content[0]` without length check |
| `save_without_update_fields` | 4 | `.save()` overwrites all columns |
| `unvalidated_lookup_chain` | 1 | `dict.get()` result used as dict key |

## Star finding: `langchain_anthropic` reads `response.content[0]` without a guard

pact caught LangChain's own Anthropic integration using unguarded LLM response access in
production code:

```
libs/partners/anthropic/langchain_anthropic/llms.py:285  [llm_response_unguarded]
  'response.content[0]' without a length/None check —
  LLM APIs return empty lists on content filtering, errors, or streaming edge cases

libs/partners/anthropic/langchain_anthropic/llms.py:321  [llm_response_unguarded]
  'response.content[0]' without a length/None check
```

The safe pattern is `response.content[0] if response.content else default`.

## Why `missing_await` dominates (256)

LangChain's architecture is deeply async — every provider integration implements both sync
and async paths, and the async path is typically a wrapper that calls the sync implementation
or streams results. Several partner implementations call `_astream()` without `await`, which
returns a coroutine object that is immediately discarded — the stream body never runs.

Selected examples:

```
libs/partners/openai/langchain_openai/chat_models/base.py:3358
  _astream_responses() called without await — body never runs

libs/partners/ollama/langchain_ollama/chat_models.py:1161
  _aiterate_over_stream() called without await — body never runs

libs/partners/anthropic/langchain_anthropic/llms.py:301
  _astream() called without await — body never runs

libs/partners/huggingface/langchain_huggingface/chat_models/huggingface.py:777
  _astream() called without await — body never runs

libs/partners/groq/langchain_groq/chat_models.py:638
  _astream() called without await — body never runs
```

The pattern repeats across every provider that ships an async streaming path.

## Graph-theoretic refactoring suggestions (`--suggest`)

pact's `--suggest` flag uses the call graph to rank functions by
`score = violation_count ÷ caller_coupling`. Functions with high violation density and
low coupling are cheapest to fix first — the minimum spanning tree of the remediation path.

```
$ pact /tmp/langchain-core/libs/ --suggest

  test_create_agent_diagram  [langchain_v1/tests/unit_tests/agents/...test_diagram.py:13]
    violations=12  callers=0  score=12.00  Z3-safe ✓
    modes: required_arg_missing

  test_rename_parameter  [core/tests/unit_tests/_api/test_deprecation.py:429]
    violations=10  callers=0  score=10.00  Z3 n/a
    modes: missing_await, required_arg_missing

  ChatModelIntegrationTests.test_usage_metadata  [standard-tests/...chat_models.py:1238]
    violations=4  callers=0  score=4.00  Z3-safe ✓
    modes: optional_dereference
```

`Z3-safe ✓` means pact's Z3 solver verified the refactor cannot introduce new constraint
violations — it is provably safe at the call-graph level.

## Production hotspot: `langchain_core/tools/base.py`

```
libs/core/langchain_core/tools/base.py:1502  [unvalidated_lookup_chain]
  'field_name' came from .get() (line 1501) but is used as a key in 'annotations'
  without 'field_name in annotations' guard — KeyError if absent
```

This is in the production tool-binding code, not a test — `field_name` is fetched with
`.get()` at line 1501 and then used as a subscript at line 1502 without a guard.

## Graph reduction (`--reduce`)

Beyond violations, `--reduce` identifies structural moving pieces — nodes and edges you can
eliminate to reduce fragility. Fewer moving pieces means fewer paths for bugs to propagate.

```
$ pact /tmp/langchain-core/libs/ --reduce

⬡ pact --reduce: 1,378 simplification targets  (showing top 20)

  PASSTHROUGH  Runnable.pick  [langchain_core/runnables/base.py:710]
    in=1 caller  out=1 callee — pure hop; inline to collapse 1 node + 2 edges
    reduction_potential=3  violations=0  score=3.0

  HUB  Runnable._atransform_stream_with_config  [langchain_core/runnables/base.py:2537]
    fan-out=24 (calls 24 functions) — split into 6 cohesive groups to reduce fan-out to ≤4
    reduction_potential=4  violations=0  score=4.0

  HUB  Runnable._abatch_with_config  [langchain_core/runnables/base.py:2364]
    fan-out=19 (calls 19 functions) — split into 5 cohesive groups to reduce fan-out to ≤4
    reduction_potential=3  violations=0  score=3.0
```

The `Runnable` base class in `langchain_core` concentrates structural complexity: pass-through
delegation methods in the class hierarchy (each adds a hop with no logic) and fan-out hubs
that call 10–24 functions (each additional edge is a potential failure path). `--reduce`
scores these by `structural_savings + violation_urgency` so the highest-value targets appear
first.

The three structural anti-patterns `--reduce` finds, with their graph-theory names:

| Anti-pattern | Graph-theory name | What it costs | How to fix |
|--------------|-------------------|---------------|------------|
| Mutual call cycles | Strongly connected component (SCC) | Can't change one function without potentially affecting all others | Break the cycle: extract shared state, flip a dependency direction |
| Pure delegation hops | Degree-2 node (pass-through) | Adds a call frame and mental model hop with no logic | Inline into the caller |
| Massive fan-out | High out-degree hub | Understanding the function requires understanding all N callees | Split by responsibility into K cohesive sub-functions |

Full findings: run `pact /path/to/langchain/libs/ --reduce --reduce-limit 50` for more targets.
