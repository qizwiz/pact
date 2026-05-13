# pact findings in future-agi

[future-agi](https://github.com/future-agi/future-agi) is an open-source AI agent
observability platform — OpenTelemetry ingestion, 50+ evaluation metrics, LLM routing.
It's pact's primary dogfood target.

## Summary (as of 2026-05-13)

```
$ pact futureagi/
✗  pact: 9,280 violation(s)
```

| Mode | Count | What it catches |
|------|------:|-----------------|
| `optional_dereference` | 7,855 | `.first()` / `.get()` result used without `None` check |
| `save_without_update_fields` | 873 | `.save()` overwrites every column, clobbers concurrent writes |
| `bare_except` | 240 | `except Exception: pass` — silent error suppression |
| `required_arg_missing` | 99 | Call site omits a required argument |
| `missing_await` | 77 | `async def` called without `await` — body never runs |
| `unvalidated_lookup_chain` | 65 | `dict.get()` result used as key without guard |
| `model_constraint` | 56 | Django model instantiation missing a required field |
| `llm_response_unguarded` | 14 | `response.choices[0]` without length check |
| `mutable_default_arg` | 1 | Mutable list default shared across all calls |

## Selected findings

### Missing await — body never runs

```
futureagi/sockets/simulation_consumer.py:58  [missing_await]
  coroutine '_subscribe_redis' called without await —
  returns a coroutine object that is immediately discarded;
  the function body never runs
```

A WebSocket consumer calls an async Redis subscription method without `await`.
The method silently no-ops on every connection.

### save() without update_fields — concurrent write clobber

```
futureagi/accounts/utils.py:220  [save_without_update_fields]
  .save() without update_fields re-writes every column;
  use save(update_fields=[...]) to prevent clobbering concurrent writes
```

873 instances across the codebase. Each one is a race condition window.

### Unvalidated lookup chain — KeyError at runtime

```
futureagi/accounts/user_onboard.py:156  [unvalidated_lookup_chain]
  'original_metric_source_id' came from .get() (line 154) but is used as a
  key in 'image_eval_metrics_map' without guard — KeyError if absent
```

A value retrieved with `dict.get()` (which returns `None` on miss) is immediately
used as a subscript key in another dict — a two-hop failure path that type checkers
and linters miss.

### LLM response unguarded

```
futureagi/integrations/tests/test_fi_sdk_e2e.py:435  [llm_response_unguarded]
  'response.choices[0]' without a length/None check — LLM APIs can return
  empty lists on error, content filtering, or streaming edge cases
```

14 call sites across the integration layer assume `choices[0]` always exists.
OpenAI and compatible providers return empty `choices` on content filter hits,
streaming errors, and rate-limit truncations.

## Running pact yourself

```bash
pip install pact-tool
git clone https://github.com/future-agi/future-agi
pact future-agi/futureagi/ --stats
```

Or against just the files changed on a branch:

```bash
pact future-agi/futureagi/ --diff main
```

## CI integration (GitHub Actions)

```yaml
- uses: actions/checkout@v4
- run: pip install pact-tool
- run: pact futureagi/ --strict --diff main
```
