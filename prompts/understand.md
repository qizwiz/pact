# Prompt: Module Understanding

You are building a world model of a software project. You have already established the project's essence (below). Now you are building a deep understanding of one specific module within it.

## Project context

{{project_essence}}

## Module to analyse

File: {{filename}}
Full path: {{file_path}}

The source below may be truncated. If you need to read beyond the truncation point,
use: `read_file_lines(path="{{file_path}}", start_line=N, end_line=M)`
Only use the tool if the source is truncated AND you need to verify a claim that
falls outside the visible range. Do NOT read lines you can already see inline.

```python
{{source}}
```

{{truncation_note}}

---

## DECLARED INTENT

### Git history
```
{{git_log}}
```

### Intent signals
```
{{intent_signals}}
```

{{graphify_rationale}}

**INTENT GAP PROTOCOL**:
1. Each docstring claim not enforced by visible code → `intent_gap` invariant, confidence ≥ 0.90.
2. Each TODO/FIXME/BUG still in source → `intent_gap` invariant, confidence 0.95.
3. Each git commit "fix X" / "ensure Y" with no visible guard → `intent_gap` invariant, confidence 0.85.
4. Source truncated mid-function body → `intent_gap` invariant at confidence 0.70, quoting the last visible line, stating what logic is unverifiable.
5. If the last visible line is a bare `def` with no body at all → mandatory `intent_gap` invariant at confidence 0.70. The `truncation_audit.last_complete_unit` must be the previous complete unit, not this bare def.

**GIT PATTERN SIGNALS** — treat as first-class evidence:
- `REVERT (Nx)`: confidence 0.95.
- `REPEATED FIXES (Nx)`: structural instability, confidence 0.85.
- `UNVERIFIED ASSERTIONS`: confidence 0.80.

---

## ██ HARD STOP — READ THIS BEFORE PRODUCING A SINGLE CHARACTER ██

The downstream parser accepts EXACTLY ONE opening: `{"truncation_audit":`

If your output begins with ANY other character — `{"path":`, `{"understanding":`, `{"file":`, a space, a newline, a markdown fence — the parser DISCARDS THE ENTIRE OUTPUT. There is no recovery. The run is lost.

This is not a formatting preference. It is a binary parse gate.

**Before you type anything**: say to yourself "My first characters will be: {"truncation_audit":" — then type exactly those characters.

Do NOT type:
- `{"path":` ← this is the most common failure mode. `path` is NEVER a top-level key.
- `{"file":` ← also forbidden
- `{"understanding":` ← also forbidden
- Any markdown code fence
- Any explanatory text

---

## ABSOLUTE RULE 1 — SCHEMA

The output JSON has EXACTLY FOUR top-level keys in this order:
1. `truncation_audit`
2. `invariants`
3. `violations`
4. `understanding`

Keys named `path`, `file`, `module`, or anything else are FORBIDDEN at the top level. Their presence is a schema failure equal to producing no output.

**SCHEMA VERIFICATION**: After writing every closing `}` or `]`, ask yourself: "What top-level keys have I written so far?" If you see anything other than the four keys above, you have already failed.

---

## ABSOLUTE RULE 2 — EMISSION ORDER WITH MANDATORY GATES

Follow these steps in exact order. Do not skip or reorder.

**Step 1**: Write ONLY these characters as your absolute first output: `{"truncation_audit":`
Then fill in the truncation_audit object and close it with `}`.

**Step 2**: Immediately after the `}` that closes truncation_audit, write: `,"invariants": []`
This is a placeholder. Do NOT fill it yet. Write it literally as `[]`.

**Step 3**: Immediately write: `,"violations": []`
This is a placeholder. Do NOT fill it yet.

**Step 4**: Immediately write: `,"understanding": {`
Then fill all seven understanding fields and close with `}`.

**Step 5**: Write the final `}` to close the outer object.

**Step 6 — MANDATORY BACKFILL**: NOW go back and replace `"invariants": []` with real invariants and `"violations": []` with real violations. If your token budget is exhausted, write at minimum one invariant per non-zero pattern_count.

**FAILURE PATTERN TO AVOID**: The most common failure is writing `{"path":` or `{"understanding":` first. If you catch yourself doing this, your entire output is already invalid. The only correct first token after `{` is `"truncation_audit"`.

**BUDGET RULE**: After writing `design_intent`, count remaining fields. If you have used more than 40% of your estimated token budget, write ONE SENTENCE per remaining understanding field and close it. A closed one-sentence field is infinitely better than a truncated mid-sentence field that breaks JSON.

**CRITICAL**: A string field that ends without a closing `"` breaks the entire JSON. If you are running low on budget inside any string field, write `(omitted for budget)"` and close the field immediately. Never let a string field truncate mid-word.

---

## ABSOLUTE RULE 3 — NO FABRICATION

Before writing any claim:
1. Does a `def`, `class`, or top-level assignment line for this name appear in the source block?
2. Can you quote the exact source line?

If NO to either: write `[not visible in truncated source]`.

Four traps:
- **Docstring trap**: a name in a docstring is not defined. Only `def` lines count.
- **Specificity trap**: invented variable names or thresholds are worse than nothing.
- **Completion trap**: when source truncates mid-function, stop at the truncation point.
- **Inference trap**: do not infer a function exists because something calls it.

---

## PRE-FLIGHT PATTERN COUNT — MANDATORY AFTER WRITING `{"truncation_audit":` AND BEFORE FILLING IT

Scan the visible source and record these counts:

- `except` clauses: ___
- `timeout=` parameters: ___
- `re.compile`/`re.match`/`re.search` calls: ___
- Hard-coded numeric constants used as thresholds: ___
- `status_code` checks: ___
- Functions whose body is truncated (def line visible, body cut off): ___
- Bare `def` lines with NO body at all (source ends at the def): ___
- `return None` / `return []` / `return "main"` on non-success paths: ___

These counts create MANDATORY INVARIANT SLOTS. Each non-zero count requires at least one invariant.

Write these counts under `"pattern_counts"` inside `truncation_audit`.

**INVARIANT DEBT RULE**: After writing understanding, before closing the document, count your invariants. If `invariants.length < number of non-zero pattern_counts`, you are missing required invariants. Add them before closing.

---

## PART 0 — TRUNCATION AUDIT

Field: `truncation_audit`. Answer exactly:
1. **last_complete_unit**: The last syntactic unit 100% complete in the source. Quote its `def`/`class`/assignment line verbatim. A bare `def` with no body is NOT a complete unit.
2. **cutoff_line**: The exact last line of the source block, quoted verbatim.
3. **visible_definitions**: Every name with an actual `def`, `class`, or top-level assignment. Mark truncated bodies with `(truncated body)`. Mark bare def lines with no body as `(no body visible)`.
4. **docstring_only_names**: Names in docstrings/comments/call sites that lack a `def`/`class`/assignment in the source.
5. **pattern_counts**: The counts from the pre-flight scan above.

---

## PART 1 — UNDERSTANDING

**Gate**: Every name in every sentence must appear in `truncation_audit.visible_definitions`. If not, write `[not visible in truncated source]`.

**Sentence structure**: Every sentence must contain at least one backtick-quoted fragment that appears verbatim in the source.

**Dynamic word budget**:
- N ≤ 5 visible definitions: 120 words per field.
- N = 6–12: 70 words per field.
- N ≥ 13: 50 words per field.

**Character budget for `key_abstractions`**: N×60 characters. Stop at the limit, write `[truncated]"` and close the field.

**STRING CLOSURE RULE**: Every understanding field is a JSON string. If you approach your token limit inside a string, STOP adding content and close the string with `"`. An empty or one-sentence closed string is valid JSON. A string that ends without `"` breaks the entire document.

### purpose
What specific problem does THIS module solve, based ONLY on visible definitions? Name visible functions/classes by role. Do NOT restate the module docstring — add specificity it omits. Acknowledge truncation if source is cut off.

FORBIDDEN: Generic phrases like "handles errors gracefully", "processes data", "provides utilities".

REQUIRED: Quote a specific construct and explain its consequence.

GOOD: "`fetch_file_head` returns `lines[:n_lines]` with a hard `timeout=10`, but no except clause wraps the `session.get` call, so any `requests.Timeout` propagates to the caller uncaught — callers that do not handle it will abort the entire corpus scan."

BAD: "The module builds an import graph and provides analysis utilities."

### design_intent
Name exactly 2 specific visible design decisions. For each: quote the exact construct, state the choice made, name the rejected alternative, and explain the consequence if the alternative were used.

FORBIDDEN: "Uses optional dependency pattern" — states a fact without tradeoffs.

GOOD: "`get_default_branch` ends with bare `return \"main\"` on any non-200 response rather than raising or returning `None`. The rejected alternative — raising on 401/403 and sleeping on 429 — would surface credential problems early. The chosen approach means a rate-limited or unauthorized scan silently treats every repo as being on branch `main`, producing systematic 404s in all subsequent `fetch_file_head` calls without any error signal."

### key_abstractions
**MUST NOT be empty if any visible definitions exist.**

**CHARACTER BUDGET: N×60 characters. Stop at the limit, write `[truncated]"`, close the field.**

For every name in `visible_definitions` (and ONLY those names):
- Constants: type, value, role, any inline comments.
- Functions: signature, key branches visible in body. For truncated bodies, quote the last visible line and note truncation.
- **For every `except` clause: quote it verbatim and state the return value or side effect.**
- **For every `timeout=`: quote it verbatim and state what exception propagates if it fires.**
- **For every non-200 HTTP status check: quote the branch verbatim and state the fallback.**
- **For every bare `def` with no visible body: note it explicitly as `(body not visible — source ends at def line)`.**

Do NOT mention names from `docstring_only_names`.

### behavioral_contract
Quote ONLY exact lines from visible source that create guarantees.

**HARD RULE**: Do not end this field mid-sentence. If you are running out of budget, write `(remaining contracts omitted for budget)` and close the string with `"`. A closed shorter field is valid. A truncated field breaks JSON and is invalid.

**TRUNCATION DETECTION**: If you are more than 60% through your estimated token budget when writing this field, write two sentences maximum then close.

**SWALLOWED-EXCEPTION RULE**: For every `except Exception:` or bare `except:` that does not log or re-raise, state ALL FIVE:
1. The function/scope containing it.
2. The exact except clause verbatim.
3. What upstream trigger causes it to fire.
4. What the caller receives.
5. The user-visible symptom.

**THREE-CONDITIONS-ONE-RETURN RULE**: If three or more distinct runtime conditions all produce the same return value (e.g., `None`, `[]`, `"main"`), name each condition explicitly and state that they are indistinguishable to callers.

**UNCAUGHT-EXCEPTION RULE**: For every `timeout=` parameter with no surrounding `try/except`, state: "The `timeout=N` in `X` has no except clause; `requests.Timeout` (and network errors) propagate to the caller uncaught."

### failure_modes
**BUDGET RULE**: If near token limit, write one sentence per failure mode and close the field. Do NOT leave this field truncated.

For each try/except block: quote it verbatim from visible source.

For each `return None` / `return "main"` / `return []` on a non-success path: state what two distinct real-world conditions produce the same return value.

**TRUNCATION FAILURE MODE**: If any function body is truncated, explicitly state: "The body of `X` is truncated at `[last visible line]`; logic after that point cannot be audited."

**BARE-DEF FAILURE MODE**: If any definition ends at the `def` line with no visible body, state: "The function `X` has no visible body; its entire contract is unverifiable."

**UNCAUGHT TIMEOUT**: If `timeout=` appears without an enclosing try/except, state: "A network timeout in `X` propagates uncaught; a single slow GitHub response aborts the entire caller."

### assumptions
List implicit assumptions embedded in VISIBLE code only. For each, quote the exact expression and explain what breaks if violated.

HIGH-VALUE ASSUMPTIONS:
- **Silent branch fallback**: `return "main"` on non-200 — breaks if GitHub returns 429; all subsequent fetches use wrong branch.
- **Fixed line window**: `lines[:n_lines]` with `n_lines=60` — breaks if imports appear below line 60 (common in large files with module docstrings).
- **Top-level imports only**: `re.match(r"^import\s+...", line)` with `^` anchor after `.strip()` — breaks for indented or conditional imports.
- **Single-segment package name**: `.split(".")[0]` — correct for `foo.bar` but also strips subpackage specificity silently.

### resource_obligations
Look for: HTTP session lifecycle, `timeout=` coverage, cache file writes, rate-limit handling, global mutable state.

If none visible, write `"(none detected in visible source)"`.

---

## PART 2 — INVARIANTS

Based ONLY on `visible_definitions`. Every invariant must pass: can you quote the exact source line in `derived_from`? If not, do not list it.

**MANDATORY SOURCES** — missing any of these when the pattern is visible is a checklist failure:
1. `except Exception:` or bare `except:` not re-raised → `error_contract`.
2. `re.compile`/`re.match`/`re.search` → `data_flow` about what is missed.
3. Hard-coded numeric limit → `assumption` about silent truncation.
4. `timeout=N` with no enclosing try/except → `error_contract` about uncaught propagation.
5. HTTP status check with silent fallback → `error_contract` about conflated conditions.
6. Boolean flag set by import try/except → `guard_requirement`.
7. TODO/FIXME/BUG comment → `intent_gap`, confidence 0.95.
8. Docstring claim absent from visible code → `intent_gap`, confidence 0.90–0.95.
9. Git commit "fix X" with no visible guard → `intent_gap`, confidence 0.85.
10. Multiple conditions returning identical value → `error_contract` about indistinguishability.
11. Source truncated mid-function body → `intent_gap` at confidence 0.70, quoting last visible line.
12. Bare `def` with no visible body → `intent_gap` at confidence 0.70, stating entire contract is unverifiable.
13. `return "main"` / `return None` / `return []` as fallback for all error conditions → `error_contract`.
14. `re.match` with `^` on stripped line that misses indented imports → `data_flow`.

**CALIBRATION**: Confidence scores MUST span ≥ 0.25. Directly quoted lines → 0.90–0.97. One-inferential-step → 0.70–0.89. Plausible but indirect → 0.60–0.69. All scores within 0.10 of each other = calibration failure.

**INVARIANT QUALITY TEST**: Could this have been written from a description of the module without reading the code? If yes, rewrite it to reference a specific line, threshold, or structural choice.

**BACKFILL REMINDER**: You wrote `"invariants": []` as a placeholder. You MUST now replace that `[]` with real content. An empty invariants array when pattern_counts has non-zero entries is a hard failure. The invariants you identified in the understanding prose MUST be formalized here.

For each invariant:
- **id**: inv_NNN
- **type**: nullable_contract | async_contract | error_contract | guard_requirement | data_flow | uniqueness | cache_contract | intent_gap | other
- **statement**: must reference a specific constant, line, or structural choice
- **applies_to**: names verbatim from `visible_definitions` ONLY
- **formal**: semi-formal (∀ / always: / never:)
- **derived_from**: QUOTE the exact line from visible source. No quote = no invariant.
- **confidence**: 0.0–1.0

GOOD:
```json
{
  "id": "inv_001",
  "type": "error_contract",
  "statement": "`get_default_branch` returns the string literal \"main\" for ALL non-200 HTTP responses — 401, 403, 404, 429, 500 are indistinguishable to callers. A rate-limited token produces the same return value as a valid repo on main branch.",
  "applies_to": ["get_default_branch"],
  "formal": "always: get_default_branch(status != 200) → \"main\" with no distinguishing signal",
  "derived_from": "return \"main\"",
  "confidence": 0.95
}
```

BAD (generic):
```json
{
  "statement": "Functions should document their error return values."
}
```

Count rules: 2–8 invariants. Heavily truncated source: 2–4 maximum.

**EMPTY INVARIANTS SELF-CHECK**: Before finalising, check your pattern_counts. For each non-zero count, confirm a corresponding invariant exists. If any non-zero count has no invariant, add one before closing.

---

## PART 3 — VIOLATIONS

**CONTRACT-FIRST GATE**: A violation is only valid if it contradicts a specific claim in `behavioral_contract` or `purpose`. Before writing a violation, state internally: "The behavioral_contract says [X]. The code fails to deliver [X] because [Y]." If you cannot complete that sentence from visible code, do not emit the violation.

**BACKFILL REMINDER**: You wrote `"violations": []` as a placeholder. You MUST now replace that `[]` with real content — OR, if no violations apply, replace it with a list of explanation strings (one per invariant, each explaining why no violation applies).

For each violation:
- **invariant_id**: which invariant.
- **line**: exact line number.
- **evidence**: quote the specific code verbatim.
- **severity**: critical | high | medium | low.
- **explanation**: answer three questions:
  1. Which function/caller is affected?
  2. What does the caller receive when the invariant is violated?
  3. What is the user-visible symptom — how does this contradict what the module claims to do?

If you cannot answer all three from visible code, do not emit the violation.

GOOD:
```json
{
  "invariant_id": "inv_002",
  "line": 35,
  "evidence": "if r.status_code != 200:\n        return None",
  "severity": "high",
  "explanation": "(1) `fetch_file_head` and every caller that aggregates its results is affected. (2) A 429 rate-limit response returns `None` identically to a 404 file-not-found. (3) The module claims to rank packages by downstream exposure across the corpus; when rate-limiting silently returns None for some files, those files are excluded from the package counter with no warning, producing a systematically incomplete ranking that appears legitimate."
}
```

**SYSTEMIC VIOLATION RULE**: If multiple functions share a silent-failure return, emit one high-severity violation citing all affected functions.

**Truncation rule**: Do NOT report violations in code you cannot see.

**VIOLATIONS FORCING RULE — HARD**: If `invariants` is non-empty and `violations` is `[]`, you MUST for each invariant either:
(a) emit a violation object, OR
(b) write a one-sentence string explaining why no violation applies: e.g., `"inv_001: no violation — the fallback is documented in the module docstring."`

Do NOT silently leave `violations` as `[]` when invariants are non-empty. That is a hard failure.

---

## FINAL SELF-CHECK (verify ALL before emitting closing `}`)

1. **FIRST CHARACTERS CHECK**: Are the literal first characters of your output `{"truncation_audit":`? If you wrote `{"path":` or anything else first, your output is invalid and cannot be salvaged.
2. `truncation_audit` contains `pattern_counts`.
3. `"invariants":` key appears immediately after `truncation_audit` closes — NOT `"understanding":`.
4. `"violations":` key appears before `"understanding":`.
5. `understanding` has all seven fields, each a properly closed string — no field ends mid-sentence or mid-word.
6. `key_abstractions` is NOT empty.
7. Outer `}` closes the entire object.
8. No field references a name absent from `visible_definitions`.
9. Every `derived_from` is a verbatim quote from the source.
10. Confidence scores span ≥ 0.25 (if invariants non-empty).
11. No `understanding` field exceeds its dynamic word limit.
12. `purpose` does NOT open with a list of all visible definition names.
13. Exactly four top-level keys: `truncation_audit`, `invariants`, `violations`, `understanding`. No `path`, `file`, `module`.
14. For each non-zero pattern_count, a corresponding invariant exists.
15. No invariant/violation references a function not in `visible_definitions`.
16. Every `timeout=` has an `error_contract` invariant about uncaught propagation.
17. Every bare `def` with no body has an `intent_gap` invariant at confidence 0.70.
18. If `violations` is `[]` and `invariants` is non-empty, each invariant has an explicit reason string.
19. No `understanding` field is truncated mid-sentence.
20. `behavioral_contract` does not end mid-sentence; if budget ran out, it ends with a closing clause like `(omitted for budget)`.
21. **BACKFILL CHECK**: Neither `invariants` nor `violations` is still the placeholder `[]`. If either is still `[]` and the module has code, that is a hard failure.
22. **NO PATH KEY**: The string `"path":` does not appear as a top-level key anywhere in your output.

---

Return JSON only — no markdown fences, no explanation outside the JSON:

{
  "truncation_audit": {
    "last_complete_unit": "...",
    "cutoff_line": "exact last line quoted verbatim",
    "visible_definitions": ["..."],
    "docstring_only_names": ["..."],
    "pattern_counts": {
      "except_clauses": 0,
      "timeout_params": 0,
      "regex_calls": 0,
      "numeric_thresholds": 0,
      "status_code_checks": 0,
      "truncated_bodies": 0,
      "bare_def_no_body": 0,
      "silent_fallback_returns": 0
    }
  },
  "invariants": [],
  "violations": [],
  "understanding": {
    "purpose": "...",
    "design_intent": "...",
    "key_abstractions": "...",
    "behavioral_contract": "...",
    "failure_modes": "...",
    "assumptions": "...",
    "resource_obligations": "..."
  }
}