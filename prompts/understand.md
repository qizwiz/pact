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

## DECLARED INTENT (what the code was supposed to do — compare against implementation)

### Git history (recent commits to this file — shows how intent evolved)
```
{{git_log}}
```

### Intent signals (module/function docstrings + TODO/FIXME/HACK/BUG comments)
```
{{intent_signals}}
```

**INTENT GAP PROTOCOL** — Before reading source details, scan the declared intent above:
1. For each docstring claim: is it enforced by visible code? If NO → `intent_gap` invariant, confidence ≥ 0.90.
2. For each TODO/FIXME/BUG still present in source: the code self-reports a deficiency → `intent_gap` invariant, confidence 0.95.
3. For each git commit saying "fix X" or "ensure Y": is X/Y enforced in visible code? If NO → `intent_gap` invariant, confidence 0.85.

Intent gaps are the MOST ACTIONABLE findings: the developer stated what was needed; the code failed to deliver it.

**NOT intent gaps** (these are code quality findings — belong in `failure_modes` or `assumptions`, not `violations`):
- A variable that is uninitialised or conditionally populated due to environment (e.g., Python version)
- A heuristic constant that might produce false positives
- A code path that is unreachable in the current runtime but reachable in another

*(This protocol informs Part 2 invariants only — do not produce separate output here.)*

---

## THE ONE UNRECOVERABLE FAILURE: FABRICATION

The source block above may be truncated. **Writing anything about code you cannot see is the only unrecoverable failure.** A short honest analysis of 2 visible functions is worth more than a complete-looking analysis that invents 8 more.

**Before writing any claim, apply this two-part test:**
1. Does the function/class/constant name appear as an actual `def`, `class`, or assignment in the source block?
2. Can you quote the exact source line that supports the claim?

If NO to either: write `[not visible in truncated source]`. Do NOT describe its behavior, parameters, logic, or purpose under any circumstances.

**Four fabrication traps — memorize these:**
- **Docstring trap**: A name mentioned in a docstring or module-level comment is NOT defined. A `def` line must exist.
- **Specificity trap**: Writing specific-sounding invented details (invented variable names, invented except clauses, invented call patterns, invented thresholds) is worse than writing nothing.
- **Completion trap**: When source truncates mid-function, do NOT infer what the rest of the function does. Stop analysis at the truncation point and say where it stops.
- **Inference trap**: Do NOT infer a function exists because the module docstring describes a pipeline step, or because a dataclass field implies a method. Only `def` lines create definitions.

---

## CRITICAL OUTPUT REQUIREMENT: COMPLETE VALID JSON

**An unclosed JSON object is a critical failure equal to fabrication.**

**A missing top-level key is a critical failure equal to fabrication.**

**An extra top-level key (e.g., `"path"`, `"file"`, `"module"`) is a structural failure equal to fabrication.**

### SCHEMA LOCK

The output JSON object has EXACTLY FOUR top-level keys, in this order:
1. `truncation_audit`
2. `invariants`
3. `violations`
4. `understanding`

### PRE-FLIGHT: COMPUTE YOUR BUDGET BEFORE WRITING ANYTHING

Before writing a single JSON character, do this silently:
1. Count the names you will place in `visible_definitions`. Call this N.
2. If N ≤ 5: each `understanding` field ≤ 120 words. If 6–12: ≤ 70 words. If 13+: ≤ 50 words.
3. Mentally confirm: "I have N visible definitions. My per-field budget is W words. I will emit invariants/violations placeholders before opening understanding."
4. If you skip this step, you will over-write `understanding` and truncate invariants — which has happened before and produces a broken output.

### MANDATORY WRITE ORDER — DO NOT DEVIATE

**STEP 1**: Open the outer `{`.

**STEP 2**: Emit `"truncation_audit": { ... }` — close the object with `}`.

**STEP 3 — EMIT PLACEHOLDERS NOW**: Write these two lines literally:
```
"invariants": [],
"violations": [],
```
This guarantees both keys exist even if the model exhausts tokens inside `understanding`. **Do not skip this step. Do not defer it. Skipping this step has produced broken outputs in the past.**

**STEP 4**: Open `"understanding": {` — write each field as a SHORT, COMPLETE string within your pre-computed budget. If you are mid-sentence and approaching the word limit, close the sentence immediately and close the field string. Never let a field string run open.

**STEP 5**: Close `"understanding": }` then replace `"invariants": []` with the real array.

**STEP 6**: Replace `"violations": []` with the real array.

**STEP 7**: Emit the closing `}` of the outer object.

**BUDGET EMERGENCY PROTOCOL**: If at any point you estimate fewer than 300 tokens remain:
1. Immediately close the current field string with `[budget reached]"` if inside a string.
2. Close `understanding` with `}`.
3. Emit whatever invariants and violations you have assembled, even if the arrays are short.
4. Emit closing `}`.

**Self-check before emitting the final `}`**:
1. Every `{` has a matching `}`
2. Every `[` has a matching `]`
3. All FOUR top-level keys are present in order: `truncation_audit`, `invariants`, `violations`, `understanding`
4. NO extra top-level keys
5. `truncation_audit.visible_definitions` is a non-empty array (or `["(none visible)"]` if source is empty)
6. No `understanding` field string is unclosed
7. Confidence scores span at least 0.25 of range (if invariants array is non-empty)
8. `key_abstractions` field is NOT empty — if any visible definitions exist, this field must contain content
9. `invariants` array is non-empty if ANY `except`, `None` check, guard, or constant is visible in the source — an empty invariants array when such patterns exist is a failure
10. `violations` array is non-empty if ANY bare `except Exception:` or `except:` block is visible — swallowed exceptions are always violations

---

## PART 0 — TRUNCATION AUDIT

JSON field: `truncation_audit`

Answer exactly:
1. **last_complete_unit**: The last syntactic unit (function, class, or statement) that is 100% complete in the source block. Quote its `def`, `class`, or assignment line verbatim.
2. **cutoff_line**: The exact last line of the source block, quoted verbatim, character for character.
3. **visible_definitions**: Every name that has an actual `def`, `class`, or top-level assignment in the source block. **Include module-level constants** (e.g., `_SKIP_DIRS = frozenset(...)`, `_HAS_TS = True`) — these are definitions too. This list GATES all subsequent analysis.
4. **docstring_only_names**: Names mentioned in module docstrings or comments that lack a `def`, `class`, or assignment line in the source block.

**Partial definition rule**: A function whose `def` line is visible but whose body is truncated belongs in `visible_definitions` AND must have its analysis stop at the truncation point, named explicitly.

**After writing `truncation_audit`, IMMEDIATELY emit the two placeholders** — `"invariants": []` and `"violations": []` — before opening `understanding`.

Example:
```json
{
  "last_complete_unit": "_SKIP_DIRS = frozenset({...}) — complete assignment ending with })",
  "cutoff_line": "       ",
  "visible_definitions": ["_HAS_TS", "_HAS_JS", "_SKIP_DIRS", "_KNOWN_ASYNC_APIS", "_KNOWN_ASYNC_METHODS"],
  "docstring_only_names": ["missing_await", "optional_dereference"]
}
```

---

## PART 1 — UNDERSTAND

**Gate check before writing each sentence**: Is every name in this sentence from `truncation_audit.visible_definitions`? If not, replace with `[not visible in truncated source]`.

**Sentence structure requirement**: Every sentence in `understanding` must contain at least one backtick-quoted code fragment that appears verbatim in the source block. Generic sentences with no quoted code fail this requirement.

**FORBIDDEN preamble**: Do NOT begin `purpose` with a list of all visible names.

**Dynamic word budget**: Count N = number of names in `visible_definitions`. If N ≤ 5: ≤ 120 words per field. If 6–12: ≤ 70 words per field. If 13+: ≤ 50 words per field.

### purpose
What specific problem does THIS module solve, based ONLY on visible definitions? Group visible functions/classes by their role. **Do not restate the module docstring** — add specificity the docstring omits. Acknowledge truncation explicitly.

GOOD: "`_HAS_TS` and `_HAS_JS` are set inside `try/except Exception:` blocks — if tree-sitter imports fail, both are `False` and all downstream analysis silently returns empty results. `_KNOWN_ASYNC_APIS` explicitly excludes `setTimeout` (comment: 'return timer IDs, NOT Promises') and `request` (comment: 'too ambiguous') — these exclusions are load-bearing design decisions."

BAD: "The module checks TypeScript files for constraint violations" — restates the docstring without code-specific detail.

### design_intent
Name exactly 2 specific visible design decisions. For each: state the exact construct (quote it), the choice made, and the alternative that was rejected. Explain WHY and what breaks if the alternative were used.

REQUIRED DEPTH: A design decision must explain the specific consequence of the choice. "Uses frozenset for performance" scores zero.

GOOD: "`_KNOWN_ASYNC_METHODS` includes `'get'` — this means any `.get(...)` call on any object (including plain dict `.get(key, default)`) will match the async method heuristic. The alternative of using a qualified name like `axios.get` was rejected in favor of split root/method matching, but the split creates false-positive risk for Map/dict `.get()` calls. If a codebase uses `myMap.get(key)` heavily, `_scan_missing_await` will flag every such call."

BAD: "Uses frozenset for O(1) lookup" — too shallow.

### key_abstractions
**This field must NOT be empty if any visible definitions exist.**

For every name in `visible_definitions`, provide: (a) for constants — their type, value, and role including any inline comments explaining exclusions; (b) for booleans set by try/except — what they gate and what happens when False; (c) for functions — their signature and key local variables/branches; (d) for truncated definitions — describe the visible portion and quote the exact last visible line.

**TRUNCATION RULE FOR THIS FIELD**: If the source truncates mid-analysis, close the field with `[source truncated at: <last_visible_line>]"` — never leave the string open.

GOOD: "`_HAS_TS`: bool, set `True` inside `try` importing `tree_sitter` and `tree_sitter_typescript`; `False` on any `Exception` — gates all TS/TSX parsing. `_SKIP_DIRS`: `frozenset` of 18 directory names including `node_modules`, `.next`, `vendor` — mirrors extractor._SKIP_DIRS per comment. `_KNOWN_ASYNC_APIS`: `frozenset` of 9 names; inline comment explains `setTimeout`/`request` exclusions. `_KNOWN_ASYNC_METHODS`: `frozenset` containing `'get'`, `'json'`, `'query'`, `'save'` among others — [source truncated at: '       ']"

BAD: `""` — empty field is always wrong.

### behavioral_contract
Quote ONLY exact lines from the visible source that create guarantees. If no behavioral enforcement is visible, say so explicitly.

**SWALLOWED-IMPORT CONTRACTS**: When a top-level `try/except` sets a boolean flag (`_HAS_TS = False`), this creates an implicit contract: all functions that check this flag will silently no-op when the import failed. Name this contract explicitly — it is invisible to callers.

**CONSTANT-AS-CONTRACT**: When a `frozenset` constant explicitly excludes values (visible in inline comments), quote the comment — it documents an intentional behavioral boundary.

GOOD: "`_HAS_TS = False` on `except Exception:` — any caller of analysis functions receives empty results with no error signal when tree-sitter is absent. The `_KNOWN_ASYNC_APIS` comment `# removed from _KNOWN_ASYNC_APIS: setTimeout/setInterval — return timer IDs, NOT Promises` is a behavioral contract: these names will never trigger missing_await violations regardless of context."

BAD: Describing contracts for functions with no visible `def` line.

**WORD LIMIT ENFORCEMENT**: At dynamic limit minus 10 words, close the current sentence, add `[field budget reached]`, and close the string.

### failure_modes
Quote each try/except block verbatim from the VISIBLE source. If none exist, say so. For each None/empty check, quote it and state what edge case it guards.

**SWALLOWED-EXCEPTION RULE — THIS IS MANDATORY**: Any `except Exception:` or bare `except:` that does not log or re-raise MUST be called out. For EACH such block, provide ALL FIVE of these:
1. The function/scope containing it (top-level, or function name)
2. The exact except clause quoted verbatim
3. What upstream trigger causes it to fire (e.g., missing pip package)
4. What the caller receives instead (e.g., _HAS_TS=False)
5. What false output the user sees (e.g., zero violations on all TS files with no warning)

Failing to call out a visible swallowed exception is treated as fabrication.

GOOD: "Top-level import guard: `except Exception: _HAS_TS = False` (lines ~19-20) — fires when `tree_sitter` or `tree_sitter_typescript` pip package is missing or version-incompatible; all TS/TSX analysis functions receive `_HAS_TS=False`; user sees zero violations on TypeScript files with no warning that analysis was skipped. Second guard: `except Exception: pass` in the JS import block — identical failure mode for JS/JSX files; `_HAS_JS` remains False."

BAD: Ignoring visible `except Exception:` blocks.

### assumptions
List implicit assumptions embedded in the VISIBLE code only. For each, quote the exact expression that reveals the assumption and explain what breaks if violated.

**THE MOST VALUABLE ASSUMPTIONS** for this type of module:
- **False-negative assumptions**: What values in constants are assumed to be exhaustive? (e.g., `_KNOWN_ASYNC_APIS` assumes the listed names cover all promise-returning APIs — any unlisted async API produces false negatives)
- **False-positive assumptions**: What values in constants assume non-collision? (e.g., `_KNOWN_ASYNC_METHODS` containing `'get'` assumes `.get()` is always async — dict/Map `.get()` calls will be false positives)
- **Import-success assumptions**: Code after the try/except blocks assumes `_HAS_TS`/`_HAS_JS` will be checked before use — what breaks if a function forgets the check?

**HARD BUDGET RULE**: This field has caused truncation in previous outputs. You MUST close this field's string value within the dynamic word limit. If you reach the limit mid-sentence, write `[field budget reached]` and close the string immediately.

GOOD: "`_KNOWN_ASYNC_METHODS` includes `'get'` without qualification — assumes any `.get(...)` call is async; `dict.get(key, default)` and `Map.get(key)` are sync and will produce false positives in `_scan_missing_await`. `_SKIP_DIRS` comment says 'mirrors extractor._SKIP_DIRS' — assumes both stay in sync; if extractor adds a skip dir without updating this module, that directory gets analyzed unnecessarily. The bare `except Exception: pass` for JS assumes a missing JS parser is non-fatal — if the user expects JS analysis, they get no error."

BAD: Assumptions about functions not in `visible_definitions`.

---

## PART 2 — INVARIANTS

Based ONLY on `visible_definitions`. Every invariant must pass the fabrication test: you must be able to quote the exact source line in `derived_from`.

**MANDATORY INVARIANT SOURCES**: The following patterns ALWAYS produce invariants — if you see them and produce no invariant, that is a failure:
1. **bare `except Exception:` or `except:`** that sets a flag or passes → error_contract invariant about silent failure
2. **`frozenset` constant with inline exclusion comments** → data_flow invariant about what is intentionally excluded
3. **Two constants that cover the same domain** (e.g., `_KNOWN_ASYNC_APIS` + `_KNOWN_ASYNC_METHODS`) → invariant about their interaction or collision risk
4. **Boolean flag set by import try/except** → guard_requirement invariant that all callers must check the flag
5. **Any TODO/FIXME/BUG/HACK comment in visible source** → `intent_gap` invariant: the code self-reports a known deficiency; confidence 0.95
6. **Any docstring claim not enforced by visible code** → `intent_gap` invariant: declared behavior without implementation; confidence 0.90–0.95
7. **Git commit says "fix X" or "ensure Y" but visible code has no guard for X/Y** → `intent_gap` invariant; confidence 0.85

**intent_gap confidence calibration**:
- 0.95: TODO/FIXME/BUG still in source (self-reported), or docstring says "raises/returns X" with no visible enforcement
- 0.90: docstring claims a property that visible code lacks
- 0.85: git history claims a fix was applied but no evidence visible in source

**INVARIANT QUALITY TEST**: Ask "Could this invariant have been written from a description of the module, without reading the actual code?" If yes, rewrite it to reference a specific line, threshold, type annotation, or structural choice visible in the code.

For each invariant:
- **id**: inv_NNN
- **type**: nullable_contract | async_contract | error_contract | guard_requirement | data_flow | uniqueness | cache_contract | intent_gap | other
- **statement**: plain English — what must always be true
- **applies_to**: names verbatim from `visible_definitions` ONLY
- **formal**: semi-formal (∀ / always: / never:)
- **derived_from**: QUOTE the exact line from the visible source. If you cannot quote it, do not list the invariant.
- **confidence**: 0.0–1.0

**Calibration rules**:
- 0.90–1.00: structurally enforced by a visible line you are quoting
- 0.70–0.89: strongly implied by a visible pattern requiring one inferential step
- 0.60–0.69: plausible from visible structure but enforcement mechanism not directly quoted
- Below 0.60: do not list
- **Scores MUST span at least 0.25 of range.** All scores within 0.10 of each other = calibration failure.

**Count rules**: 2–8 invariants. Heavily truncated source: 2–4 maximum. Do not pad by inventing invariants about invisible code. If you cannot produce 2 from visible code, write `[]`.

GOOD INVARIANT (swallowed-exception, high confidence):
```json
{
  "id": "inv_001",
  "type": "error_contract",
  "statement": "If tree_sitter or tree_sitter_typescript fails to import, _HAS_TS is silently set False and all TS/TSX analysis returns empty results with no error signal to the caller",
  "applies_to": ["_HAS_TS"],
  "formal": "always: import failure → _HAS_TS=False → downstream analysis no-ops silently",
  "derived_from": "except Exception:\n    _HAS_TS = False",
  "confidence": 0.97
}
```

GOOD INVARIANT (false-positive risk, moderate confidence):
```json
{
  "id": "inv_002",
  "type": "data_flow",
  "statement": "_KNOWN_ASYNC_METHODS contains 'get' with no object-type qualifier — synchronous dict.get() and Map.get() calls will match the async heuristic, producing false-positive missing_await violations",
  "applies_to": ["_KNOWN_ASYNC_METHODS"],
  "formal": "always: any .get(...) call matches _KNOWN_ASYNC_METHODS regardless of object type",
  "derived_from": "\"get\",",
  "confidence": 0.72
}
```

BAD INVARIANT — generic, could be written from a description alone:
```json
{
  "statement": "The module skips certain directories during file scanning",
  "derived_from": "_SKIP_DIRS = frozenset({...})"
}
```

---

## PART 3 — VIOLATIONS

**CONTRACT-FIRST GATE — apply before writing any violation:**

A violation entry is only valid if it contradicts a specific claim in the module's `behavioral_contract` or `purpose`. Before writing a violation, state internally: *"The behavioral_contract says [X]. The code fails to deliver [X] because [Y]."* If you cannot complete that sentence from visible code, do not emit a violation — the finding is a code quality note, not an intent gap.

**LINTER BOUNDARY**: The following are NOT violations regardless of visibility — a linter can catch them; pact should not:
- A variable that is set but conditionally populated (depends on Python version, platform, or environment)
- A constant that might collide with a non-target pattern (false-positive risk in heuristics)
- Two constants that might drift out of sync with no enforcement

These may belong in `failure_modes` or `assumptions` of the understanding, but not in violations.

**WHAT IS A VIOLATION**: A violation is evidence that the module fails to deliver its stated purpose — its docstring makes a claim the code does not honour, a git commit says "ensure X" but X is absent, or a TODO/BUG names a missing capability the module is supposed to have.

For each violation:
- **invariant_id**: which invariant
- **line**: exact line number in the source block
- **evidence**: quote the specific code verbatim
- **severity**: critical | high | medium | low
- **explanation**: answer three questions explicitly: (1) Which function/caller is affected? (2) What does the caller receive when the invariant is violated? (3) What is the user-visible symptom — specifically, how does this contradict what the module claims to do? If you cannot answer all three from visible code, say so rather than fabricating.

**Truncation rule**: Do NOT report violations in code you cannot see.

GOOD VIOLATION — contract-anchored (docstring claims error signalling, code swallows silently):
```json
{
  "invariant_id": "inv_001",
  "line": 19,
  "evidence": "except Exception:\n    _HAS_TS = False",
  "severity": "high",
  "explanation": "(1) Any caller of TS/TSX analysis functions. (2) Caller receives empty violation list []. (3) The module docstring states 'always surfaces analysis errors to the caller' — a broken tree-sitter install produces zero violations with no warning, directly contradicting that claim."
}
```

GOOD VIOLATION — intent gap (TODO still in source):
```json
{
  "invariant_id": "inv_002",
  "line": 47,
  "evidence": "# TODO: feed Z3 counterexample back to Hypothesis for targeted shrinking",
  "severity": "high",
  "explanation": "(1) The hypothesis_generator pipeline. (2) Caller receives generic random inputs rather than counterexample-guided inputs. (3) The module's stated purpose is 'adversarial input generation from behavioral contracts' — the TODO self-reports that counterexample feedback is missing, making the generation non-adversarial."
}
```

BAD VIOLATION — code quality pattern, not a contract contradiction:
```json
{
  "invariant_id": "inv_003",
  "line": 95,
  "evidence": "\"get\",",
  "severity": "medium",
  "explanation": "Any .get() call including dict.get() will match the async heuristic."
}
```
*(BAD: this is a false-positive risk in a heuristic — a linter concern. The module never claimed .get() is always async.)*

---

## FINAL SELF-CHECK

Before writing the closing `}`, verify:
1. `truncation_audit` is present and closed
2. `invariants` array is present and closed — even if empty
3. `violations` array is present and closed — even if empty
4. `understanding` has all six fields, each a closed string within the dynamic word limit
5. `key_abstractions` is NOT an empty string
6. The outer `}` closes the entire object
7. No field references a name absent from `truncation_audit.visible_definitions`
8. Every `derived_from` is a verbatim quote from the source block
9. Confidence scores span at least 0.25 of range (if invariants non-empty)
10. No `understanding` field exceeds its dynamic word limit
11. `purpose` does NOT open with a list of all visible definition names
12. No extra top-level keys — exactly four: `truncation_audit`, `invariants`, `violations`, `understanding`
13. `assumptions` field string is CLOSED — previous outputs have truncated here without closing
14. No invariant or violation references a function not in `visible_definitions`
15. If ANY `except Exception:` or `except:` block is visible AND the `behavioral_contract` claims the module always signals errors to callers → `violations` is non-empty. If the module makes no such claim, a swallowed exception belongs in `failure_modes`, not `violations`.
16. `key_abstractions` field closes with either complete analysis or `[source truncated at: <last_visible_line>]` — never an open string

---

Return JSON only — no markdown fences, no explanation outside the JSON:

{
  "truncation_audit": {
    "last_complete_unit": "...",
    "cutoff_line": "exact last line quoted verbatim",
    "visible_definitions": ["..."],
    "docstring_only_names": ["..."]
  },
  "invariants": [],
  "violations": [],
  "understanding": {
    "purpose": "...",
    "design_intent": "...",
    "key_abstractions": "...",
    "behavioral_contract": "...",
    "failure_modes": "...",
    "assumptions": "..."
  }
}