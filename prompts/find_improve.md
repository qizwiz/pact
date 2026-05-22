# Prompt: Find Self-Improvement

You are a prompt engineer reviewing the performance of a property-discovery prompt.
You will see: the find prompt that was used, a sample of its outputs (confirmed and
unconfirmed violations), and failure modes aggregated across all runs.

Your job is to identify systematic weaknesses in the prompt and rewrite it so that
future runs produce more runnable Hypothesis strategies and fewer unconfirmed hints.

## The find prompt that was used

{{prompt_text}}

## Sample outputs (hypothesis-confirmed violations)

```json
{{confirmed_samples}}
```

## Sample outputs (unconfirmed violations — no Hypothesis strategy or predicate)

```json
{{unconfirmed_samples}}
```

## Failure modes (aggregated)

{{failure_modes}}

---

## Evaluation rubric

Score each dimension 0–10:

**JSON_COMPLIANCE** (target: 9+)
What fraction of runs returned valid JSON starting with `{`? Score 10 if > 95% valid.
Score 0 if the model frequently writes reasoning text before the JSON object.
This is a prompt instruction failure — if it's low, the output format section must be
stronger and earlier in the prompt.

**HYPOTHESIS_STRATEGY_COMPLETENESS** (target: 8+)
What fraction of returned violations include a non-empty `hypothesis_strategy` field?
Score 10 if > 90% have runnable strategies. Score < 5 if the model frequently leaves
this blank or writes placeholder text like "st.text()". Look at unconfirmed_samples —
are the strategies missing, empty, or syntactically broken?

**COUNTEREXAMPLE_SPECIFICITY** (target: 8+)
Are `counterexample_hint` values concrete Python expressions (e.g., `f('', max_length=1)`)
or vague descriptions (e.g., "any input where max_length is small")?
Score < 5 if hints are descriptions rather than runnable calls.

**SEMANTIC_DEPTH** (target: 7+)
Do the violations represent real behavioral contracts (implicit invariants that callers
depend on) rather than obvious type errors or None inputs?
Score < 5 if most violations are "None causes AttributeError" or similar obvious cases.
The prompt instructs to skip obvious violations — if they still appear, the instruction
needs to be stronger.

**FILE_READ_DISCIPLINE** (target: 8+)
Does evidence in the violation data suggest the model actually read the function bodies
before proposing violations? Score < 5 if violations reference non-existent parameters,
wrong line numbers, or function signatures that don't match the actual source.

**PREDICATE_CORRECTNESS** (target: 7+)
Are `hypothesis_predicate` values syntactically valid lambda expressions that would
actually detect the violation? Score < 5 if predicates are pseudo-code, backwards
(returning True when violated), or reference names not in scope.

---

## Your output

1. Score each dimension.
2. For each dimension < 8: identify the systemic prompt weakness causing it.
3. Rewrite the prompt. Rules:
   - Keep the same 3-part structure (PART 0 read file, PART 1 find breaking inputs,
     PART 2 output)
   - The output section must come LAST, after all tool calls are done
   - Strengthen any part where systematic failures cluster
   - Add explicit GOOD vs BAD examples for dimensions that score < 7
   - For hypothesis_strategy completeness issues: add concrete strategy examples
     matching common violation patterns (empty string, zero length, surrogate chars, etc.)
   - For JSON compliance issues: make the output instruction more prominent; consider
     adding "If you write any text before `{`, you have failed."
   - Keep the same output JSON schema: file, properties[]
   - Each property must have: function, line, statement, why_it_matters,
     counterexample_hint, hypothesis_strategy, hypothesis_predicate, severity
   - Do not lengthen the prompt unless the length adds precision

Return JSON only:
{
  "scores": {
    "json_compliance": {"score": 0, "justification": "..."},
    "hypothesis_strategy_completeness": {"score": 0, "justification": "..."},
    "counterexample_specificity": {"score": 0, "justification": "..."},
    "semantic_depth": {"score": 0, "justification": "..."},
    "file_read_discipline": {"score": 0, "justification": "..."},
    "predicate_correctness": {"score": 0, "justification": "..."}
  },
  "weaknesses": [
    {
      "dimension": "...",
      "systemic_cause": "what in the prompt causes this failure",
      "example_failure": "quote from unconfirmed_samples that illustrates it"
    }
  ],
  "overall_score": 0.0,
  "improved_prompt": "full text of the rewritten prompt — complete, ready to use"
}
