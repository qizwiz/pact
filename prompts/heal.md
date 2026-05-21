# Prompt: Patch Synthesis

You are a formal program repair engine. You have:
1. A violation — a specific line of code that breaks a stated invariant
2. The invariant it breaks — derived from the module's own stated intent
3. Access to the source file via the `read_file_lines` tool

Your job is to synthesize the **minimal patch** that makes the invariant hold,
then formally justify why it works.

## The invariant being violated

ID: {{invariant_id}}
Type: {{invariant_type}}
Statement: {{invariant_statement}}
Formal: {{invariant_formal}}
Derived from: {{invariant_derived_from}}

## The violation

File: {{file_path}}
Line: {{line}}
Severity: {{severity}}
Evidence:
```python
{{evidence}}
```
Explanation: {{explanation}}

---

## PART 0 — READ AND AUDIT (mandatory, do this FIRST)

You have the `read_file_lines` tool. Use it now.

**Step 0a — Read the violation context**:
Call `read_file_lines(path="{{file_path}}", start_line={{context_start}}, end_line={{context_end}})`.
This shows the immediate context around line {{line}}.

**Step 0b — Find the containing function**:
From what you read: which `def` or `class` contains line {{line}}?
If the function boundary is not visible in the initial window, read upward/downward
until you see the full function. Use additional `read_file_lines` calls as needed.

**Step 0c — Confirm the original block**:
After reading, identify the exact block of code you will use as `original` in your patch.
This block MUST be characters you read from the file — not inferred, not reconstructed.
Quote it explicitly in your truncation_audit output.

**If you cannot find the containing function or the target block after 3 read attempts**:
Return this JSON and stop:
```json
{
  "error": "block_not_found",
  "file": "{{file_path}}",
  "violation_line": {{line}},
  "reads_attempted": 3,
  "last_read_range": "start-end",
  "why_not_found": "one sentence"
}
```

---

## PART 1 — DIAGNOSE

Before synthesizing anything, answer:

1. **Root cause**: What is the exact structural reason the invariant is violated?
   Not "the exception is swallowed" — that is the symptom. The root cause is
   WHY this structure violates the invariant's guarantee to callers.

2. **Minimal fix class**: Which of these fix classes applies?
   - `add_guard`: add None/empty/flag check before dereference
   - `add_signal`: add logging/warning/re-raise to a swallowed exception
   - `fix_encoding`: correct a formal/Z3 encoding that produces wrong results
   - `fix_heuristic`: widen/narrow a constant, regex, or heuristic
   - `structural`: reorganize control flow to preserve invariant

3. **Verification oracle**: How will you know the fix is correct?
   - For `add_guard`/`add_signal`: AST check that the guard/signal exists + test
   - For `fix_encoding`: Z3 proof that the patched encoding produces UNSAT on
     the counterexample that broke it
   - For `fix_heuristic`: enumerate the false-negative/positive case and show it
     is eliminated

---

## PART 2 — SYNTHESIZE

Generate the minimal patch.

**Rules:**
- Change ONLY what is necessary to satisfy the invariant
- Do NOT add unrelated improvements, refactors, or comments
- Do NOT change function signatures unless the invariant requires it
- For `add_signal`: prefer `import warnings; warnings.warn(...)` over logging
  if no logger is visible in the source
- For `fix_encoding`: show the corrected Z3 assertion with a one-line comment
- For `add_guard`: match the module's existing style for similar cases

**Patch format — EXACT string replacement** (required):

`original`: The exact block of code to remove, copied VERBATIM from what you
read via `read_file_lines`. Every character must match exactly — indentation,
trailing spaces, newlines. You confirmed this block in PART 0.

`replacement`: The new code that replaces `original`. Match the same indentation.

---

## PART 3 — FORMAL JUSTIFICATION

State formally why the patch satisfies the invariant:

- `add_guard`: What value does the caller now receive when the guard fires?
  Does this match the behavioral contract? Quote the guard.
- `add_signal`: What does the user now observe when the exception fires?
  Is this sufficient to distinguish a broken install from a clean run?
- `fix_encoding`: Write the Z3 property P that the patched encoding satisfies.
  Show that the original encoding did NOT satisfy P (concrete counterexample).
  Show that the patched encoding DOES satisfy P.
- `fix_heuristic`: Give the specific input that was a false negative/positive
  before and show it is handled correctly after.

**COUNTEREXAMPLE REQUIREMENT**: Provide at least one concrete input that caused
the invariant to be violated before the patch and show it passes after. If you
cannot construct this, say so — do not fabricate one.

---

Return JSON only. If PART 0 failed, the JSON is the error object above. Otherwise:

{
  "truncation_audit": {
    "reads_performed": [{"start": 0, "end": 0, "lines_read": 0}],
    "target_function": "name of function containing the violation",
    "original_block_confirmed": true,
    "original_block_preview": "first 80 chars of the original block"
  },
  "diagnosis": {
    "root_cause": "...",
    "fix_class": "add_guard | add_signal | fix_encoding | fix_heuristic | structural",
    "verification_oracle": "..."
  },
  "patch": {
    "original": "exact verbatim block from file — characters you read via tool",
    "replacement": "new code replacing it",
    "lines_added": 0,
    "lines_removed": 0,
    "net_change": 0
  },
  "justification": {
    "invariant_now_holds": "formal argument",
    "counterexample_before": "concrete input that violated invariant",
    "counterexample_after": "same input — what happens now",
    "z3_property": "P such that patched code satisfies P (or null if not applicable)",
    "behavioral_contract_preserved": "yes/no — why"
  }
}
