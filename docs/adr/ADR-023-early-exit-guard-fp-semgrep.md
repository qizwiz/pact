# ADR-023: Early-exit guard false positive in `llm_response_unguarded` Semgrep rule

## Status

Accepted

## Context

`pact`'s `llm_response_unguarded` checker detects unguarded `.choices[0]` access on
OpenAI-compatible API responses. A companion Semgrep rule was written to provide a
second, language-agnostic pass at the same detection:

```yaml
# semgrep/llm-response-unguarded.yaml
patterns:
  - pattern: $RESPONSE.choices[0]
  - pattern-not-inside: |
      if $RESPONSE.choices:
        ...
  - pattern-not-inside: |
      if not $RESPONSE.choices:
        ...
  - pattern-not-inside: |
      if len($RESPONSE.choices) > 0:
        ...
  - pattern-not-inside: |
      $X = $RESPONSE.choices[0] if $RESPONSE.choices else ...
```

During corpus validation the rule produced false positives on the **early-exit** guard
pattern, which is common in production code:

```python
# Pattern A — early-exit with guard on separate line
if not response.choices:
    return ""
return response.choices[0].message.content   # <-- flagged (FP)
```

Semgrep's `pattern-not-inside` checks whether the `$RESPONSE.choices[0]` expression is
*syntactically nested inside* an `if` block — it cannot see that the guard on the
preceding line makes the flagged line unreachable when `choices` is empty. Because the
`return response.choices[0]...` line is a sibling statement, not a child, Semgrep treats
it as unguarded.

The `pact` Z3 / AST checker does not have this limitation: it tracks dataflow and
reachability across statement boundaries using the Fixedpoint Datalog solver, correctly
classifying early-exit guards as safe.

## Decision

1. **Keep the Semgrep rule as a best-effort, low-false-negative linter.** The rule is
   useful for quick CI scans where Semgrep is already in the pipeline. Document the
   known FP category in the rule's `message` field.

2. **Do not attempt to eliminate the FP in Semgrep.** The fix would require either
   inter-statement dataflow analysis (not available in Semgrep's pattern language) or
   rewriting the rule as a taint analysis (which would need a Semgrep Pro licence and
   would not run in OSS CI). The cost outweighs the benefit.

3. **The `pact` AST checker is authoritative.** Semgrep results that conflict with
   `pact`'s output should be resolved in `pact`'s favour. `pact scan` already handles
   early-exit guards correctly via the `_has_enclosing_guard` AST walk (introduced in
   ADR-018).

4. **Add `# nosemgrep: llm-response-unguarded-choices` suppression guidance** to the
   Semgrep rule's documentation for teams that use only Semgrep and hit this FP.

## Consequences

- Teams using only the Semgrep rule will see FPs on early-exit guard patterns; this is
  acceptable as the FP rate is low and the fix is a one-line suppression comment.
- `pact scan` users are unaffected — the AST checker has zero FPs on this pattern.
- The Semgrep rule continues to catch the majority of genuinely unguarded patterns (bare
  `response.choices[0]` with no guard at all, and inline `if choices:` blocks that omit
  the `message is None` check).

## Known limitations documented in rule message

The Semgrep rule message now states:

> **Known false positive**: if `choices` is guarded by an early-exit check on the
> *preceding line* (`if not response.choices: return`), Semgrep will still flag the
> access because the guard is a sibling statement, not a parent block. Add
> `# nosemgrep: llm-response-unguarded-choices` to suppress if the early-exit guard
> is present.

## Crash vectors covered

The checker and rule together address two distinct crash paths:

| Path | Trigger | Exception |
|------|---------|-----------|
| Empty choices list | `choices = []` | `IndexError: list index out of range` |
| Null message | `choices[0].message = None` (Gemini content-filter: `PROHIBITED_CONTENT`) | `AttributeError: 'NoneType' object has no attribute 'content'` |

The comprehensive guard covering both:

```python
if not response.choices or response.choices[0].message is None:
    return ""  # or raise / log as appropriate
```

## Related

- [ADR-018](ADR-018-ast-enclosing-stmt-guard-placement.md) — AST guard placement logic
- [ADR-036](ADR-036-pact-formal-analysis-toolkit.md) — formal analysis toolkit
- Semgrep rule: `~/src/pact-standalone/semgrep/llm-response-unguarded.yaml`
- Live reproduction: Gemini 2.5 Flash returns `choices[0].message = None` on
  `finish_reason: PROHIBITED_CONTENT` (verified 2026-05-17 against production API)
- LightRAG issue #2551: real production crash report confirming the `IndexError` path
