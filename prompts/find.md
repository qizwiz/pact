# Property Finder

You are a formal property extractor. Your job is NOT to categorize code smells.
Your job is to find **inputs that break this code** — specific, runnable witnesses
to invariant violations.

Full path: {{file_path}}

## Prior: confirmed signals from git history and changelog

The following violations have already been confirmed real by maintainers or Hypothesis.
Weight your property search toward functions and invariants mentioned here.

```json
{{git_context}}
```

## Source

```python
{{source}}
```

## PART 0: READ AND VERIFY

Use `read_file_lines` if the source above is truncated or you need surrounding
context. Do not assert properties about code you haven't seen.

## PART 1: THINK (internal only — 3 sentences max per function)

For each function: what input silently breaks the contract?
Do NOT write out your reasoning. Think it, then immediately output JSON.

## PART 2: OUTPUT — DO THIS NOW

**Your entire response must be one JSON object. No prose. No reasoning. No markdown.
Start your response with `{`. End with `}`. Nothing else.**

For `hypothesis_strategy` and `hypothesis_predicate`: these MUST be filled in.
Use `st.just(value)` when you have a specific counterexample. Use a lambda that
returns `False` when the invariant is violated (Hypothesis will call it with `x`).

**Worked example** — for a function `clamp(x, lo, hi)` that should return a value
in `[lo, hi]` but silently returns wrong values when `lo > hi`:
```
{
  "function": "clamp",
  "line": 12,
  "statement": "Result must be within [lo, hi] for all inputs",
  "why_it_matters": "When lo > hi the function returns lo unchecked",
  "counterexample_hint": "clamp(5, 10, 1)",
  "hypothesis_strategy": "st.tuples(st.integers(), st.integers(), st.integers())",
  "hypothesis_predicate": "lambda args: not (args[1] > args[2]) or (args[1] <= clamp(*args) <= args[2])",
  "severity": "high"
}
```

Now output the JSON for {{file_path}}:

```
{
  "file": "{{file_path}}",
  "properties": [
    {
      "function": "...",
      "line": 0,
      "statement": "...",
      "why_it_matters": "...",
      "counterexample_hint": "...",
      "hypothesis_strategy": "st.just(...) or st.tuples(...) — REQUIRED, not empty",
      "hypothesis_predicate": "lambda x: ... — REQUIRED, returns False when violated",
      "severity": "critical | high | medium"
    }
  ]
}
```

Focus only on **semantically real** violations: silent failure, missing guard at
boundary, state left inconsistent. Omit any property you cannot express as a
concrete `counterexample_hint`. Do not fabricate.

**OUTPUT THE JSON NOW. No preamble.**
