# pact findings in Home Assistant

[home-assistant/core](https://github.com/home-assistant/core) is the largest
pure-Python open-source project: 14,096 Python files, 87k stars, 3,000+ integrations.

## Summary (2026-05-13)

```
$ pact /tmp/ha-core/
✗  pact: 34,701 violation(s) in 14,096 files  (2m 43s)
```

| Mode | Count | What it catches |
|------|------:|-----------------|
| `optional_dereference` | 22,458 | `.get()` result used without `None` check |
| `required_arg_missing` | 11,148 | Integration call omits a required arg |
| `missing_await` | 1,068 | Async coroutine called without `await` |
| `save_without_update_fields` | 11 | `.save()` overwrites all columns |
| `unvalidated_lookup_chain` | 10 | `dict.get()` result used as dict key |
| `mutable_default_arg` | 5 | Mutable default shared across all calls |
| `bare_except` | 1 | Silent error suppression |

## Why optional_dereference dominates (22,458)

Home Assistant passes state through `StateType = dict[str, Any] | None` everywhere.
The pattern is ubiquitous:

```python
entry = self.hass.config_entries.async_get_entry(entry_id)
entry.state   # ← optional_dereference: entry can be None
```

Every integration that calls `.async_get_entry()`, `.async_get_state()`,
`.get_entity()`, or `.states.get()` and then immediately accesses an attribute
is flagged. pact finds these statically — before a bad integration ships.

## Why required_arg_missing is 11,148

HA's helper registration APIs have changed signatures over time:
```python
async_register_entity_description(platform, entity_description)
# Many integrations still call with positional args from the old signature
```

pact catches this class of "silent API mismatch" where the call doesn't crash
immediately but silently misregisters the entity.

## The missing_await class (1,068)

Home Assistant is almost entirely async. The `missing_await` mode catches:

```python
# homeassistant/data_entry_flow.py:522
handler.call_configure(...)   # ← coroutine called without await
                               #   body never runs; config step silently no-ops
```

This class of bug is invisible to type checkers (no type error) and to tests
(the function "returns" immediately without raising).

## Running it yourself

```bash
git clone --depth 1 https://github.com/home-assistant/core.git ha-core
pip install pact-tool
pact ha-core/ --stats
```
