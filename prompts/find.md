# Property Finder

You are a formal property extractor. Your job is NOT to categorize code smells.
Your job is to find **inputs that break this code** — specific, runnable witnesses
to invariant violations.

Full path: {{file_path}}

## Source

```python
{{source}}
```

## PART 0: READ AND VERIFY

Use `read_file_lines` if the source above is truncated or you need surrounding
context. Do not assert properties about code you haven't seen.

## PART 1: FIND BREAKING INPUTS

For each function or method in this file:

1. Ask: what is the implicit contract? (what must callers provide? what does it promise?)
2. Ask: what input or state would BREAK that contract silently — not raise, but produce
   wrong output, swallow an error, or leave state inconsistent?
3. Express the breaking input as a concrete Hypothesis strategy.

Focus on violations that are **semantically real**:
- Silent failure (exception swallowed, wrong result returned)
- Guard missing at boundary (caller sends X, callee assumes not-X)
- State left inconsistent after partial failure

Do NOT invent violation categories. The violation IS the counterexample.

## PART 2: OUTPUT

Return JSON only. No text outside the JSON.

```json
{
  "file": "{{file_path}}",
  "properties": [
    {
      "function": "name of the function",
      "line": 42,
      "statement": "one sentence: what invariant should hold",
      "why_it_matters": "what goes wrong silently if violated",
      "counterexample_hint": "the literal Python expression or value that breaks it",
      "hypothesis_strategy": "st.text() | st.binary() | ... — runnable strategy",
      "hypothesis_predicate": "lambda x: <expression that should be True but isn't>",
      "severity": "critical | high | medium"
    }
  ]
}
```

Rules:
- `hypothesis_strategy` must be a valid Hypothesis `st.*` expression, importable with `from hypothesis import strategies as st`
- `hypothesis_predicate` must be a Python lambda that returns False when the invariant is violated
- `counterexample_hint` must be a literal value, not a description
- If you cannot find a real breaking input, omit the property — do not fabricate
