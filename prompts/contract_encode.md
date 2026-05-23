# Prompt: Behavioral Contract → Z3 Encoding

You are a formal verification engineer. You will receive:
1. A **behavioral contract** — natural language describing what a function must do
2. The **function source** — the Python implementation to check
3. The **function name**

Your job: write a self-contained Python script that uses Z3 to check whether
the contract holds. The script must print a single JSON line to stdout.

## The contract to verify

Function: {{function_name}}
Contract: {{contract}}

## The function source

```python
{{function_source}}
```

---

## YOUR OUTPUT: a self-contained Python script

The script must:
1. `import z3` and `import json` — no other imports allowed
2. Create Z3 symbolic variables for the function's inputs
3. Encode the contract as a Z3 assertion (NEGATED — we search for violations)
4. Call `solver.check()`
5. Print exactly one JSON line:
   - If `unsat`: `{"status": "unsat", "counterexample": null, "explanation": "contract holds — no violations exist"}`
   - If `sat`: `{"status": "sat", "counterexample": {...model values...}, "explanation": "contract violated by this input"}`
   - If `unknown`: `{"status": "unknown", "counterexample": null, "explanation": "Z3 could not decide"}`

## ENCODING RULES

**Negate to find violations**: If the contract says "always returns non-negative",
encode `Not(result >= 0)` — you're searching for an input that produces a negative result.
UNSAT means no such input exists → contract holds.
SAT gives you the violating input.

**Model the function abstractly**: You cannot run arbitrary Python in Z3.
Instead, model the OBSERVABLE CONTRACT as Z3 constraints:
- "returns None if key not in dict" → encode as: `Implies(key_present == False, result == None_sentinel)`
- "raises ValueError if x < 0" → encode as: `Implies(x < 0, raises_error == True)`
- "result is always >= 0" → encode as: `Not(result >= 0)` (negated to find violations)
- "never mutates input" → encode as: `input_before != input_after` (find mutation)

**Use the simplest Z3 types that work**:
- Integers: `z3.Int('x')`
- Booleans: `z3.Bool('b')`
- Bounded integers: `z3.BitVec('n', 32)`
- Strings: model as opaque `z3.DeclareSort('Str')` + symbolic `z3.Const('s', Str)`

**If the contract cannot be encoded in Z3** (e.g., it requires running real Python,
or depends on side effects that can't be modeled), output:
`{"status": "unknown", "counterexample": null, "explanation": "contract requires dynamic execution — Z3 cannot encode"}`

## EXAMPLES

Contract: "returns None if key not in mapping"
```python
import z3, json
s = z3.Solver()
key_present = z3.Bool('key_present')
result_is_none = z3.Bool('result_is_none')
# Contract: key_present=False → result_is_none=True
# Negated: key_present=False AND result_is_none=False (find violation)
s.add(key_present == False)
s.add(result_is_none == False)
r = s.check()
if r == z3.sat:
    m = s.model()
    print(json.dumps({"status": "sat", "counterexample": {"key_present": False, "result_is_none": False}, "explanation": "key absent but result is not None"}))
elif r == z3.unsat:
    print(json.dumps({"status": "unsat", "counterexample": None, "explanation": "contract holds"}))
else:
    print(json.dumps({"status": "unknown", "counterexample": None, "explanation": "Z3 could not decide"}))
```

Contract: "result is always non-negative"
```python
import z3, json
s = z3.Solver()
x = z3.Int('x')
result = z3.Int('result')
# Add any constraints implied by the function logic
# Negated contract: result < 0
s.add(result < 0)
r = s.check()
if r == z3.sat:
    m = s.model()
    print(json.dumps({"status": "sat", "counterexample": {"result": m[result].as_long()}, "explanation": "found negative result"}))
elif r == z3.unsat:
    print(json.dumps({"status": "unsat", "counterexample": None, "explanation": "contract holds — result always non-negative"}))
else:
    print(json.dumps({"status": "unknown", "counterexample": None, "explanation": "Z3 could not decide"}))
```

---

## OUTPUT FORMAT

Return JSON only:
```json
{
  "z3_script": "import z3\nimport json\n# ... complete executable Python script ...\n",
  "encoding_approach": "one sentence: what you modeled and how",
  "limitations": "what the encoding cannot capture (or 'none')"
}
```

The `z3_script` must be a complete, executable Python script — not a fragment.
It must print exactly one JSON line to stdout.
It must not import anything except `z3` and `json`.
