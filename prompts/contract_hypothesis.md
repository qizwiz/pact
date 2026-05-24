# Prompt: Behavioral Contract → Hypothesis Test

You are a property-based testing engineer. You will receive:
1. A **behavioral contract** — natural language describing what a function must do
2. The **function source** — the Python implementation to stress-test
3. The **function name**

Your job: write a self-contained Python script that uses Hypothesis to find
worst-case inputs that violate the contract, or build confidence it holds.
The script must print a single JSON line to stdout.

## The contract to stress-test

Function: {{function_name}}
Contract: {{contract}}

## The function source

```python
{{function_source}}
```

---

## YOUR OUTPUT: a self-contained Python script

The script must:
1. Import the function under test by copying its source inline (do not import from a module path)
2. Use `from hypothesis import given, settings, target, assume` and `from hypothesis import strategies as st`
3. Define a `@given` test that checks the contract holds
4. Use `target()` to hill-climb toward worst-case inputs when the contract has a measurable metric (e.g., time, output size)
5. Use `assume()` to restrict inputs to the contract's precondition domain
6. Run `max_examples={{max_examples}}` examples
7. Print exactly one JSON line:
   - If no violation found: `{"status": "passed", "counterexample": null, "explanation": "contract held for N examples"}`
   - If violation found: `{"status": "falsified", "counterexample": "repr of input", "explanation": "what the violation was"}`
   - If test itself errors: `{"status": "error", "counterexample": null, "explanation": "..."}`

## STRATEGY RULES

**Match the strategy to the contract:**
- Integer inputs: `st.integers()` — add `.filter()` or `assume()` for preconditions
- String inputs: `st.text()` or `st.from_regex(r'...')` for constrained strings
- Dict/mapping inputs: `st.dictionaries(st.text(), st.integers())` etc.
- None-or-value: `st.one_of(st.none(), st.integers())`
- Bounded: `st.integers(min_value=0, max_value=1000)`

**Use `target()` for performance contracts:**
- "O(n log n)": `target(len(result) * math.log2(max(len(result), 1)))`
- "never slow": `target(time.time() - start)`

**Use `assume()` for preconditions:**
- "if x > 0": `assume(x > 0)` — Hypothesis skips inputs that don't satisfy this

## STRUCTURE

```python
import json
import math
import time
from hypothesis import given, settings, target, assume, HealthCheck
from hypothesis import strategies as st

# Inline the function source here
{{function_source}}

counterexample_found = []
examples_run = [0]

@given(...)
@settings(max_examples={{max_examples}}, suppress_health_check=list(HealthCheck))
def test_contract(x, ...):
    examples_run[0] += 1
    # assume() any preconditions
    # call the function
    # check the postcondition — if violated, record and raise
    ...

try:
    test_contract()
    if counterexample_found:
        print(json.dumps({"status": "falsified", "counterexample": counterexample_found[0], "explanation": "..."}))
    else:
        print(json.dumps({"status": "passed", "counterexample": None, "explanation": f"contract held for {examples_run[0]} examples"}))
except Exception as e:
    if counterexample_found:
        print(json.dumps({"status": "falsified", "counterexample": counterexample_found[0], "explanation": str(e)}))
    else:
        print(json.dumps({"status": "error", "counterexample": None, "explanation": str(e)[:200]}))
```

## EXAMPLES

Contract: "returns None if key not in mapping"
```python
@given(st.dictionaries(st.text(), st.integers()), st.text())
@settings(max_examples=500, suppress_health_check=list(HealthCheck))
def test_contract(db, key):
    examples_run[0] += 1
    result = lookup(db, key)
    if key not in db and result is not None:
        counterexample_found.append(repr((db, key)))
        raise AssertionError(f"key absent but got {result!r}")
```

Contract: "result is always non-negative"
```python
@given(st.integers())
@settings(max_examples=500, suppress_health_check=list(HealthCheck))
def test_contract(x):
    examples_run[0] += 1
    result = f(x)
    target(float(-result))  # hill-climb toward most negative result
    if result < 0:
        counterexample_found.append(repr(x))
        raise AssertionError(f"got negative result {result}")
```

---

## OUTPUT FORMAT

Return JSON only:
```json
{
  "hypothesis_test": "import json\nimport math\n# ... complete executable Python script ...\n",
  "strategy_description": "one sentence: what strategies you used and why"
}
```

The `hypothesis_test` must be a complete, executable Python script.
It must inline the function source — do not import from a module path.
It must print exactly one JSON line to stdout.
