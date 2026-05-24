# Prompt: CEGIS Counterexample Analysis

You are a formal verification engineer. Z3 found a counterexample to a behavioral contract.
Your job: determine whether this is a **genuine contract violation** or an **encoding error**
in the Z3 script.

## The contract
Function: {{function_name}}
Contract: {{contract}}

## The function source
```python
{{function_source}}
```

## The Z3 encoding that was run
```python
{{z3_script}}
```

## The counterexample Z3 found
```json
{{counterexample}}
```

---

## YOUR TASK

Read the counterexample carefully. Trace it through the function source manually.

**Ask yourself:**
1. If I pass these concrete input values to the function, does the function actually violate the contract?
2. Or did the Z3 encoding model the contract incorrectly — e.g., wrong direction of implication, missing constraint, wrong sentinel value?

**Two outcomes:**

**Genuine violation**: The counterexample input really would cause the function to break the contract.
→ Set `is_genuine_violation: true`. Explain clearly what the function does wrong on this input.
→ Leave `refined_z3_script` empty.

**Encoding error**: The Z3 script modeled the contract wrong. The counterexample is an artifact of the bad encoding, not a real bug.
→ Set `is_genuine_violation: false`. Explain what was wrong in the encoding.
→ Provide a corrected `refined_z3_script` that fixes the encoding error.
   The refined script must follow the same rules as the original:
   - `import z3` and `import json` only
   - Print exactly one JSON line to stdout
   - Encode the NEGATION of the contract (search for violations)

## COMMON ENCODING ERRORS TO WATCH FOR
- Implication direction flipped (`A → B` encoded as `B → A`)
- Missing precondition constraints (Z3 finds an input the function would never receive)
- Wrong sentinel for None (e.g., using -1 when the function uses 0)
- Negation applied to the wrong sub-expression
- Unconstrained free variables (Z3 picks arbitrary values)

---

## OUTPUT FORMAT

Return JSON only:
```json
{
  "is_genuine_violation": true,
  "reasoning": "trace of why this input violates (or does not violate) the contract",
  "refined_z3_script": ""
}
```

If `is_genuine_violation` is false, `refined_z3_script` must be a complete executable Python script.
If `is_genuine_violation` is true, `refined_z3_script` must be an empty string.
