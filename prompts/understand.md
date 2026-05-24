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

**GIT PATTERN SIGNALS** — the git log may contain pre-analysed patterns. Treat these as first-class intent evidence:
- `REVERT (Nx)`: an intent was attempted and pulled back N times. Confidence 0.95.
- `REPEATED FIXES (Nx)`: same area fixed 3+ times — structural instability. Confidence 0.85.
- `UNVERIFIED ASSERTIONS`: commits say "ensure X" with no paired test/verify commit. Confidence 0.80.
- `HIGH COMMIT DENSITY`: this file changes frequently — load-bearing or poorly bounded.

**NOT intent gaps**: uninitialised variables due to environment, heuristic constants, unreachable code paths in current runtime. These belong in `failure_modes` or `assumptions`.

*(This protocol informs Part 2 invariants only — do not produce separate output here.)*

---

## THE ONE UNRECOVERABLE FAILURE: FABRICATION

The source block above may be truncated. **Writing anything about code you cannot see is the only unrecoverable failure.**

**Before writing any claim, apply this two-part test:**
1. Does the function/class/constant name appear as an actual `def`, `class`, or assignment in the source block?
2. Can you quote the exact source line that supports the claim?

If NO to either: write `[not visible in truncated source]`. Do NOT describe its behavior.

**Four fabrication traps:**
- **Docstring trap**: A name mentioned in a docstring is NOT defined. A `def` line must exist.
- **Specificity trap**: Invented variable names, invented except clauses, invented thresholds are worse than nothing.
- **Completion trap**: When source truncates mid-function, stop analysis at the truncation point.
- **Inference trap**: Do NOT infer a function exists because a docstring or another function calls it. Only `def` lines create definitions.

**FABRICATION SELF-CHECK — perform this before writing `key_abstractions`:**
List every name you plan to mention. For each, write internally: "The `def`/`class`/assignment line is: [quote the exact line]." If you cannot complete that sentence, the name is fabricated. Remove it.

---

## SCHEMA LOCK — THE SECOND UNRECOVERABLE FAILURE

**Emitting a JSON object that does not contain all four top-level keys is a schema failure equal to fabrication.**

The output JSON object has EXACTLY FOUR top-level keys, in this order:
1. `truncation_audit`
2. `invariants`
3. `violations`
4. `understanding`

**Extra top-level keys (`path`, `file`, `module`, anything else) are forbidden.** Their presence is a schema failure.

**SCHEMA PRE-EMISSION GATE**: The very first characters you emit must be `{"truncation_audit":`. If you have written anything else as the first characters, stop and restart. Nothing may precede `truncation_audit`.

---

## MANDATORY EXECUTION ORDER — READ THIS FIRST, FOLLOW IT EXACTLY

Token budget exhaustion inside `understanding` has repeatedly caused `invariants` and `violations` to be silently dropped, producing broken JSON. Previous runs have also emitted forbidden top-level keys (`path`) and omitted required arrays entirely. The ONLY defence is this order:

**PHASE A — Emit structural skeleton first:**

Your output must begin EXACTLY as follows (copy this template character-for-character):
```
{"truncation_audit": { ... },
 "invariants": [],
 "violations": [],
 "understanding": {
```

Write `truncation_audit`, then write `"invariants": [],` and `"violations": [],` as literal placeholders BEFORE opening `understanding`. **If your next token after closing `truncation_audit` is not `"invariants"`, stop and restart.** These placeholders guarantee the keys exist even if you run out of tokens inside `understanding`.

**PHASE B — Write `understanding` fields in this fixed order:** `purpose`, `design_intent`, `key_abstractions`, `behavioral_contract`, `failure_modes`, `assumptions`, `resource_obligations`.

**PHASE B BUDGET CHECKPOINT**: After writing `design_intent`, pause and count how many fields remain (5). If you have already used more than 50% of your estimated token budget, switch immediately to one-sentence-per-field mode for all remaining fields. A one-sentence field that is closed is better than a detailed field that is truncated mid-sentence and breaks the JSON.

Close each string before opening the next. If budget runs low, truncate with `[budget reached]"` and close the field immediately.

**PHASE C — After closing `understanding`, replace the placeholder `[]` arrays with real invariants and violations.**

**PHASE D — Emit closing `}`.**

**NEVER open `understanding` before the placeholder lines exist in your output. This is the single most important rule.**

---

## PRE-FLIGHT BUDGET CALCULATION

Before writing a single character:
1. Count names you will place in `visible_definitions`. Call this N.
2. Word budget per `understanding` field: N≤5 → 120 words. N=6–12 → 70 words. N≥13 → 50 words.
3. Character budget for `key_abstractions`: N×60 characters maximum. If you hit this limit mid-entry, write `[truncated]"` and close the field.
4. Invariant budget: 2–4 invariants for truncated source; 2–8 for complete source.

**PRE-FLIGHT PATTERN COUNT** — before writing anything, count these patterns in the visible source:
- `except` clauses: ___
- `None` returns or checks: ___
- `re.compile` or `re.match`/`re.search` calls: ___
- `subprocess` calls: ___
- Hard-coded numeric constants or string literals used as thresholds/limits: ___
- `timeout=` parameters: ___

Write this count internally. If the total is > 0 and your invariants array is empty when you reach Phase C, you have failed the checklist. You must produce at least one invariant per visible `except` clause and one per `timeout=` parameter.

---

## PART 0 — TRUNCATION AUDIT

JSON field: `truncation_audit`

Answer exactly:
1. **last_complete_unit**: The last syntactic unit (function, class, or statement) that is 100% complete in the source block. Quote its `def`, `class`, or assignment line verbatim.
2. **cutoff_line**: The exact last line of the source block, quoted verbatim, character for character.
3. **visible_definitions**: Every name that has an actual `def`, `class`, or top-level assignment in the source block. Include module-level constants. A function whose `def` line is visible but body is truncated: include in list, mark with `(truncated body)`.
4. **docstring_only_names**: Names mentioned in docstrings, comments, or other functions' bodies that lack a `def`, `class`, or assignment line in the source block.

**After `truncation_audit`, IMMEDIATELY emit `"invariants": [],` and `"violations": [],` before any other output.**

---

## PART 1 — UNDERSTAND

**Gate check**: Every name in every sentence must be from `truncation_audit.visible_definitions`. If not, replace with `[not visible in truncated source]`.

**Sentence structure requirement**: Every sentence must contain at least one backtick-quoted code fragment that appears verbatim in the source block.

**FORBIDDEN preamble**: Do NOT begin `purpose` with a list of all visible names.

### purpose
What specific problem does THIS module solve, based ONLY on visible definitions? Name the visible functions/classes by role. Do not restate the module docstring — add specificity the docstring omits. Acknowledge truncation explicitly if source is cut off.

GOOD: "`_run` wraps `subprocess.run` with `except Exception: return ''` — any subprocess failure, including git-binary-missing or timeout, silently returns empty string. All callers treat `''` as 'no data found' with no way to distinguish failure from genuine absence."

BAD: "The module extracts violation signals from git history" — restates the docstring without code-specific detail.

### design_intent
Name exactly 2 specific visible design decisions. For each: state the exact construct (quote it), the choice made, the alternative that was rejected, and the consequence if the alternative were used.

GOOD: "`_FIX_PATTERN` includes `\\b(handle|check|ensure)\\b` — these are implementation verbs, not fix signals. The choice to include them over-broadens commit matching: a commit saying 'handle auth flow' matches and inflates the fix-signal count, diluting the prior with non-failure data. The alternative (only matching 'fix|bug|crash') would miss informal fix language but reduce noise."

BAD: "Uses regex for pattern matching" — states a fact, not a design decision with consequences.

### key_abstractions
**This field must NOT be empty if any visible definitions exist.**

**CHARACTER BUDGET: N×60 characters. Stop at the limit, write `[truncated]"`, close the field.**

For every name in `visible_definitions` (and ONLY those names):
- Constants: type, value, role, any inline comments.
- Booleans set by try/except: what they gate, what happens when False.
- Functions: signature, key branches visible in source body. For truncated bodies, quote the last visible line and note truncation.
- **For every `except` clause in a function body: quote it verbatim and state the return value.**
- **For every `timeout=` parameter: quote it verbatim and state what exception propagates when it fires.**
- **For every non-200 HTTP status check: quote the branch verbatim and state the fallback return value.**

Do NOT mention names from `docstring_only_names` in this field.

### behavioral_contract
Quote ONLY exact lines from the visible source that create guarantees.

**SWALLOWED-EXCEPTION RULE**: For every `except Exception:` or bare `except:` that does not log or re-raise, state ALL FIVE:
1. The function/scope containing it.
2. The exact except clause verbatim.
3. What upstream trigger causes it to fire.
4. What the caller receives instead (the return value).
5. What the user-visible symptom is — specifically, how does this hide a real failure?

GOOD: "`_run`: `except Exception: return ''` — fires on subprocess.TimeoutExpired (a subclass of Exception) in addition to all other errors. Caller receives `''`, which `_git_log` interprets as empty history and returns `'(no git history)'`. User sees an empty prior JSON identical to a legitimately-analyzed file with no fix history."

BAD: "The function returns empty string on error" — does not explain the user-visible symptom.

**HTTP SILENT-FALLBACK RULE**: For every function that checks `r.status_code` and returns a fallback on non-200 WITHOUT raising or logging, apply the same five-point analysis:
1. The function name.
2. The exact status check line verbatim.
3. What HTTP condition triggers the fallback.
4. What the caller receives.
5. How this masks a real failure (rate-limit hit, private repo, wrong token, etc.).

**SYSTEMIC PATTERN RULE**: If three or more functions share the same silent-failure contract (e.g., all return neutral/zero values on exception), name this as a systemic property and explain what a plausible-looking aggregate output means when all sub-components fail simultaneously.

### failure_modes
Quote each try/except block verbatim from the VISIBLE source. If none visible, say so.

For each swallowed exception (no log, no re-raise): explain what distinguishable information is lost. Ask: "What two different real-world conditions produce the same return value?" Answer that question explicitly.

GOOD: "`_run`'s `except Exception: return ''` conflates: (a) git binary not installed, (b) repository not initialized, (c) 30-second timeout on large repo, (d) file never committed. All four return `''`. Downstream code cannot distinguish them."

**TIMEOUT FAILURE MODE**: For every `timeout=N` in a `requests.get` or `subprocess.run` call, state: (a) what exception fires at N seconds, (b) whether an `except` clause catches it, (c) if not caught, what propagates to the caller, (d) what the user-visible symptom is.

**HTTP FAILURE MODE**: For every `session.get(...)` call, state what happens on: (a) network unreachable, (b) 401/403 (bad token), (c) 429 (rate limited), (d) 404 (repo not found). If all four map to the same return value, name this as a conflation failure.

**AGGREGATE FAILURE MODE**: If multiple sub-functions each have independent silent-failure returns, state what the top-level aggregation function (e.g., Counter, ranking) returns if ALL sub-functions fail simultaneously. Is that output distinguishable from a legitimately computed result?

### assumptions
List implicit assumptions embedded in VISIBLE code only. For each, quote the exact expression and explain what breaks if violated.

MOST VALUABLE ASSUMPTIONS for a GitHub-crawling/corpus-analysis module:
- **Line-count adequacy**: hard-coded `n_lines=60` — breaks if imports appear after line 60.
- **Branch-name fallback**: `return "main"` — breaks if the default branch is `master` or `trunk` and GitHub returns non-200.
- **Token availability**: any `f"Bearer {token}"` — breaks silently if token is empty string or expired.
- **Session retry behaviour**: if `session` has no retry adapter, a single 429 aborts the crawl.
- **Regex completeness**: `re.match(r"^import\s+...")` — breaks for `import (foo)` multi-line imports or `__import__` calls.
- **Heuristic transform completeness**: any fixed `transforms` list — breaks when pip package name differs from repo name by a pattern not in the list.
- **Cache key stability**: any `{cache_key}.json` — breaks if the key construction changes between runs, causing stale cache hits.

### resource_obligations
Look for these patterns in visible code:
- **HTTP session lifecycle**: is `session` created with a context manager? What happens if it is never closed?
- **Timeout coverage**: `timeout=10` in `requests.get` — does an `except` catch `requests.Timeout`? If not, the exception propagates uncaught.
- **Cache file writes**: any `json.dump` to disk — is there an `except` for disk-full or permission errors?
- **Rate-limit handling**: any `time.sleep` or retry logic visible? If not, rapid sequential calls risk 429.
- **Missing timeout coverage**: `requests.get` with `timeout=10` but no `except requests.Timeout` — net effect: timeout aborts the entire run.
- **Global/module-level mutable state**: any `_cache = {}` or `Counter()` modified inside functions.

If none visible, write: `"(none detected in visible source)"`

---

## PART 2 — INVARIANTS

Based ONLY on `visible_definitions`. Every invariant must pass: can you quote the exact source line in `derived_from`? If not, do not list it.

**MANDATORY INVARIANT SOURCES** — if you see these and produce no invariant, that is a checklist failure:
1. **`except Exception:` or bare `except:`** that does not re-raise → `error_contract` invariant.
2. **`re.compile(...)` or `re.match/re.search` with documented over-breadth or exclusion comments** → `data_flow` invariant about what import patterns are missed.
3. **Hard-coded numeric limit (e.g., `n_lines=60`)** → `assumption` invariant about what is silently truncated.
4. **`timeout=N` in `session.get` or `subprocess.run`** → `error_contract` invariant about what exception fires and whether it is caught.
5. **HTTP status check that falls through to a default** → `error_contract` invariant about conflated HTTP failure conditions.
6. **Boolean flag set by import try/except** → `guard_requirement` invariant.
7. **TODO/FIXME/BUG/HACK comment** → `intent_gap` invariant, confidence 0.95.
8. **Docstring claim not enforced by visible code** → `intent_gap` invariant, confidence 0.90–0.95.
9. **Git commit says "fix X" but no guard visible** → `intent_gap` invariant, confidence 0.85.
10. **Multiple functions sharing the same silent-failure return pattern** → `error_contract` invariant describing the systemic aggregate effect.
11. **Heuristic transform list applied to repo names** → `assumption` invariant about false negatives when repo naming deviates from the pattern.
12. **Hard-coded normalisation ceiling (e.g., `/ 50.0`)** → `assumption` invariant describing what happens when real values exceed the ceiling.

**CALIBRATION REQUIREMENT**: Confidence scores MUST span at least 0.25 of range. Structurally-visible lines → 0.90–0.97. One-inferential-step patterns → 0.70–0.89. Plausible-but-not-directly-quoted → 0.60–0.69. Do not list below 0.60. All scores within 0.10 of each other = calibration failure — rewrite before emitting.

**INVARIANT QUALITY TEST**: Could this invariant have been written from a description of the module, without reading the actual code? If yes, rewrite it to reference a specific line, threshold, or structural choice.

GOOD:
```json
{
  "id": "inv_001",
  "type": "error_contract",
  "statement": "`fetch_file_head`'s `timeout=10` fires `requests.Timeout` (a subclass of `OSError`). No `except` clause is visible in the function body. The exception propagates uncaught to the caller. If `main()` does not catch it, a single slow GitHub response aborts the entire corpus scan.",
  "applies_to": ["fetch_file_head"],
  "formal": "always: fetch_file_head with slow host → uncaught requests.Timeout → corpus scan aborted",
  "derived_from": "r = session.get(url, headers={\"Authorization\": f\"Bearer {token}\"}, timeout=10)",
  "confidence": 0.92
}
```

BAD:
```json
{
  "statement": "The module swallows HTTP errors",
  "derived_from": "fetch_file_head function"
}
```

For each invariant:
- **id**: inv_NNN
- **type**: nullable_contract | async_contract | error_contract | guard_requirement | data_flow | uniqueness | cache_contract | intent_gap | other
- **statement**: what must always be true — must reference a specific constant, line, or structural choice
- **applies_to**: names verbatim from `visible_definitions` ONLY
- **formal**: semi-formal (∀ / always: / never:)
- **derived_from**: QUOTE the exact line from visible source. No quote = no invariant.
- **confidence**: 0.0–1.0

**Count rules**: 2–8 invariants. Heavily truncated source: 2–4 maximum.

**EMPTY INVARIANTS ARRAY SELF-CHECK**: Before finalising, scan your source block for: `except`, `None` check, guard, `frozenset` constant, bounded numeric type, `re.compile`, `re.match`, `re.search`, hard-coded model name, hard-coded threshold, hard-coded normalisation divisor, `timeout=`, or `status_code` check. If ANY of these are present and `invariants` is `[]`, that is a checklist failure. You must produce at least one invariant.

---

## PART 3 — VIOLATIONS

**CONTRACT-FIRST GATE**: A violation is only valid if it contradicts a specific claim in `behavioral_contract` or `purpose`. Before writing a violation, state internally: *"The behavioral_contract says [X]. The code fails to deliver [X] because [Y]."* If you cannot complete that sentence from visible code, do not emit a violation.

**LINTER BOUNDARY** — NOT violations:
- A variable conditionally populated based on environment.
- A heuristic constant that might produce false positives.
- Two constants that might drift out of sync with no enforcement.

**WHAT IS A VIOLATION**: Evidence that the module fails to deliver its stated purpose — a docstring makes a claim the code does not honour, a git commit says "ensure X" but X is absent, a TODO names a missing capability, or a function makes an implicit promise ("fetches" = succeeds) that the visible code can silently break.

For each violation:
- **invariant_id**: which invariant.
- **line**: exact line number.
- **evidence**: quote the specific code verbatim.
- **severity**: critical | high | medium | low.
- **explanation**: answer three questions explicitly:
  1. Which function/caller is affected?
  2. What does the caller receive when the invariant is violated?
  3. What is the user-visible symptom — specifically, how does this contradict what the module claims to do?
  If you cannot answer all three from visible code, say so and do not emit the violation.

GOOD:
```json
{
  "invariant_id": "inv_001",
  "line": 37,
  "evidence": "r = session.get(url, headers={\"Authorization\": f\"Bearer {token}\"}, timeout=10)",
  "severity": "high",
  "explanation": "(1) `main()` calls `fetch_file_head` for every corpus file. (2) On timeout, `requests.Timeout` propagates uncaught, aborting the entire scan. (3) The module docstring claims to rank packages across the full corpus — a single slow GitHub server response silently produces a partial ranking with no indication of how many files were skipped."
}
```

**SYSTEMIC VIOLATION RULE**: If the module docstring claims to compute a ranking or exposure metric, and multiple sub-components of that metric have independent silent-failure returns, this constitutes a single high-severity violation: the claimed metric can be indistinguishable from a fully-failed measurement. Emit this as one violation citing all affected functions.

**Truncation rule**: Do NOT report violations in code you cannot see.

---

## FINAL SELF-CHECK (20 items — verify ALL before emitting closing `}`)

1. The first characters emitted are `{"truncation_audit":` — no other key precedes it.
2. `truncation_audit` is present and closed.
3. `invariants` array is present and closed — even if empty.
4. `violations` array is present and closed — even if empty.
5. `understanding` has all seven fields, each a closed string: `purpose`, `design_intent`, `key_abstractions`, `behavioral_contract`, `failure_modes`, `assumptions`, `resource_obligations`.
6. `key_abstractions` is NOT an empty string.
7. The outer `}` closes the entire object.
8. No field references a name absent from `truncation_audit.visible_definitions`.
9. Every `derived_from` is a verbatim quote from the source block.
10. Confidence scores span at least 0.25 of range (if invariants non-empty).
11. No `understanding` field exceeds its dynamic word limit.
12. `purpose` does NOT open with a list of all visible definition names.
13. No extra top-level keys — exactly four: `truncation_audit`, `invariants`, `violations`, `understanding`. Keys named `path`, `file`, `module` are forbidden.
14. `assumptions` field string is CLOSED.
15. No invariant or violation references a function not in `visible_definitions`.
16. Every name in `key_abstractions` has a corresponding `def`/`class`/assignment line in the source block — names from `docstring_only_names` are ABSENT from `key_abstractions`.
17. `invariants` array is non-empty if ANY of these are visible: `except`, `None` check, guard, `re.match`, `re.search`, `re.compile`, hard-coded threshold, `timeout=`, or `status_code` check. An empty invariants array when such patterns exist is a checklist failure.
18. If multiple functions share the same silent-failure return contract AND the module claims to compute an aggregate metric from them, a systemic `error_contract` invariant and a corresponding violation exist.
19. Every `except` clause visible in the source has a corresponding `error_contract` invariant quoting it verbatim.
20. Every `timeout=` parameter visible in the source has a corresponding `error_contract` invariant stating what exception fires and whether it is caught.

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
    "assumptions": "...",
    "resource_obligations": "..."
  }
}