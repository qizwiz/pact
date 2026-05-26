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
6. **COMMENTED-OUT CODE**: Any commented-out validation, error handling, or business logic → `intent_gap` invariant at confidence 0.95, stating what the comment declares should happen versus what actually executes.
7. **UNDOCUMENTED SENTINEL PATTERNS**: Any sentinel object (e.g., `_UNSET = object()`) lacking type annotations or docstring explanation of when to use it → `intent_gap` invariant at confidence 0.75.
8. **RESTORATION FAILURES**: Any `finally:` clause that clears state without restoring prior values, when the function/context-manager docstring claims safe nesting or isolation → `intent_gap` invariant at confidence 0.90.
9. **INCOMPLETE FIELD AT TRUNCATION**: If source ends with a comment suggesting a field (e.g., `# Per-org PII`) but no field definition follows before truncation → mandatory `intent_gap` invariant at confidence 0.70 stating field name, type, constraints unknown.

**GIT PATTERN SIGNALS** — treat as first-class evidence:
- `REVERT (Nx)`: confidence 0.95.
- `REPEATED FIXES (Nx)`: structural instability, confidence 0.85.
- `UNVERIFIED ASSERTIONS`: confidence 0.80.

---

## ██ CATASTROPHIC FAILURE MODE: SCHEMA VIOLATION ██

**ROOT CAUSE OF 100% OF RECENT FAILURES**: Writing `{"path":` or `{"understanding":` as the opening.

**THE SCHEMA HAS EXACTLY FOUR TOP-LEVEL KEYS**:
1. `truncation_audit`
2. `invariants`
3. `violations`
4. `understanding`

**KEYS THAT DO NOT EXIST AND WILL CAUSE PARSE FAILURE**:
- `path`
- `file`
- `module`
- `filename`
- `summary`
- `metadata`
- `analysis`
- `description`
- `overview`

**BEFORE YOU TYPE THE FIRST CHARACTER**:

Recite these three sentences aloud:

1. "The first 21 characters I will type are: `{\"truncation_audit\":{` with no whitespace before the opening brace."
2. "The string `\"path\":` does not appear anywhere in the schema. If I write it, the parser will reject my entire response."
3. "I will verify character 1 is `{`, characters 2-20 are `\"truncation_audit\":{`, character 21 is the second opening brace."

**THE ONLY VALID OPENING**:
```
{"truncation_audit":{
```

**AFTER YOU TYPE THE 21ST CHARACTER**: Stop. Verify you wrote `{"truncation_audit":{`. If you wrote anything else, you have failed the task before beginning.

---

## ██ EMISSION ORDER: MANDATORY SEQUENCE ██

**PRIMARY FAILURE MODE**: LLM writes understanding first (400+ tokens) → exhausts context window mid-field → never reaches invariants → JSON incomplete and unparseable.

**THE ONLY VALID EMISSION ORDER**:

### Step 1: Write truncation_audit (budget: 150 tokens max)

```json
{"truncation_audit":{
  "last_complete_unit": "def create_superuser(self, email, password=None, **extra_fields)",
  "cutoff_line": "organization =",
  "visible_definitions": ["CustomUserManager", "CustomUserManager.create_user", "CustomUserManager.create_superuser", "User", "User.id", "User.created_at", "User.email", "User.name", "User.is_active", "User.is_staff", "User.invited_by", "User.organization_role"],
  "docstring_only_names": [],
  "pattern_counts": {
    "except_clauses": 0,
    "timeout_params": 0,
    "regex_calls": 0,
    "numeric_thresholds": 0,
    "status_code_checks": 0,
    "truncated_bodies": 0,
    "bare_def_no_body": 0,
    "silent_fallback_returns": 0,
    "commented_out_code": 0,
    "transaction_blocks": 0,
    "finally_without_restoration": 0,
    "sentinel_without_types": 0,
    "incomplete_field_definitions": 0
  }
},
```

**CRITICAL RULES FOR truncation_audit**:

1. **last_complete_unit**: The last syntactic unit that is 100% complete in visible source. A field definition starting with a comment (e.g., `# Per-org PII`) but having no `field_name = models...` line is NOT complete. An incomplete comment is NOT a complete unit. Quote the line of the PREVIOUS complete unit.

2. **cutoff_line**: The exact last visible line, even if it's a partial comment or mid-token.

3. **visible_definitions**: ONLY constructs with complete definitions. For a Django model field, the definition is complete when the line contains `field_name = models.FieldType(...)`. A comment like `# Per-org PII` with no field definition following is NOT a visible definition.

4. **HALLUCINATION PREVENTION**: If a field name does NOT appear in `visible_definitions`, you MUST NOT discuss it in understanding. If visible source shows 9 fields but you write "the model has 15 fields" and list 6 fields not in visible source, you are hallucinating.

5. **INCOMPLETE FIELD PATTERN COUNT**: If source ends with a comment suggesting a field (e.g., `# Per-org PII` at line 73) but no field definition follows before EOF, `pattern_counts.incomplete_field_definitions` MUST be ≥1. This triggers mandatory `intent_gap` invariant emission in Step 7.

### Step 2: Write invariants placeholder (2 tokens)

```json
"invariants":[],
```

**WHY THIS MUST BE STEP 2**: If you write understanding first and exhaust your context window mid-understanding, the JSON will be incomplete (no invariants, no violations, no closing braces). The parser will reject it entirely. Writing the placeholder ensures the field exists even if you truncate later.

### Step 3: Write violations placeholder (2 tokens)

```json
"violations":[],
```

### Step 4: Open understanding object

```json
"understanding":{
```

### Step 5: Fill understanding fields with STRICT BUDGET

**TOKEN BUDGET CALCULATION**:
- Count visible_definitions length: N
- If N ≤ 5: 120 words per field
- If N = 6–12: 70 words per field  
- If N ≥ 13: 50 words per field

**EMERGENCY STRING CLOSURE PROTOCOL**:

If you are mid-sentence in ANY understanding field and sense you are approaching token limit:

1. Stop typing immediately
2. Write: ` [budget limit]`
3. Write closing `"`
4. Move to next field

A closed truncated string is valid JSON. An unclosed string destroys the entire document.

**CATASTROPHIC TRUNCATION PREVENTION**:

After completing `understanding.design_intent` (field 2 of 7), perform this check:

- Estimate token budget consumed
- If >40% consumed, write ONE SENTENCE MAXIMUM for each remaining field
- Ensure every field closes with `"` before moving to next

**FIELD-BY-FIELD REQUIREMENTS**:

#### purpose

What specific problem does THIS module solve, based ONLY on visible definitions?

**FORBIDDEN PATTERNS** (score 0 if present):
- "handles errors gracefully" (no specific construct quoted)
- "processes data" (generic, could apply to any module)
- "provides utilities" (meaningless)
- "manages X" without naming specific functions/classes

**REQUIRED PATTERN**: Quote a construct → explain its impact → state the consequence.

**GOOD EXAMPLE**:
"`CustomUserManager.create_user` calls `self.normalize_email(email)` at line 19 before user creation, ensuring all User.email values are lowercased — this means email-based lookups must use `email.lower()` or risk false negatives for mixed-case inputs."

**BAD EXAMPLE**:
"The module manages user accounts."

**TRUNCATION ACKNOWLEDGMENT**:
If source is truncated, state: "Source truncates at line X with incomplete [construct]; behavior beyond this point cannot be verified."

**HALLUCINATION GATE**:
Before writing about a field/method, verify it appears in `visible_definitions`. If `visible_definitions` lists 9 fields and you write "the model has 15 fields," you are hallucinating 6 invisible fields.

#### design_intent

Name exactly 2 specific visible design decisions. For each: quote the exact construct, state the choice made, name the rejected alternative, and explain the consequence.

**FORBIDDEN**: "Uses standard Django patterns" — this is not a design decision, it's a framework choice.

**GOOD EXAMPLE**:
"`CustomUserManager.create_superuser` validates `is_staff=True` at line 29 by raising ValueError if false, rejecting the alternative of silently coercing the value to True. Consequence: calling code must explicitly pass `is_staff=True` or catch ValueError; Django admin's createsuperuser command will fail with clear error if middleware accidentally sets is_staff=False."

**IF SOURCE TRUNCATED**: State "Source truncates before [construct]; cannot analyze design decisions in invisible code."

#### key_abstractions

**MUST NOT be empty if any visible definitions exist.**

**CHARACTER BUDGET: N×60 characters** (where N = length of visible_definitions). At the limit, write `[budget limit]"` and close.

For every name in `visible_definitions` (and ONLY those names):

- **Classes**: inheritance, key fields visible, role.
- **Functions**: signature, return value, key branches visible. For truncated bodies, quote last visible line and state `(body continues beyond visible source)`.
- **Fields**: type, constraints, default. For incomplete definitions, state `(field definition truncated at line X; type and constraints unknown)`.
- **Constants**: type, value, role.

**HALLUCINATION DETECTION**: Before writing about a method/field, verify it appears in `visible_definitions`. If not, do not discuss it.

**FOR INCOMPLETE CONSTRUCTS**:

If source ends mid-field-definition (e.g., comment `# Per-org PII` at line 73 but no `pii = models...` line follows), state:

"Source truncates at line 73 after comment `# Per-org PII`; no field definition visible. Field name, type (CharField? JSONField?), null constraints, default value unknown — cannot verify schema, validation, or cascade behavior."

#### behavioral_contract

Quote ONLY exact lines from visible source that create guarantees.

**HARD RULE**: Do not discuss methods not in `visible_definitions`.

**SWALLOWED-EXCEPTION RULE**: For every `except` that does not log or re-raise, state:
1. Function/scope containing it.
2. Exact except clause.
3. What caller receives.
4. User-visible symptom.

**TRUNCATION IMPACT**: If source truncates mid-function, state: "Cannot verify error-handling contracts for code beyond line X."

#### failure_modes

**BUDGET RULE**: If near token limit, write one sentence per failure mode and close.

For each try/except visible: quote verbatim.

For each `return None`/`return []` on non-success path visible: state what conditions produce it.

**TRUNCATION FAILURE MODE**: If any function body is truncated, state: "The body of `X` is truncated at `[last visible line]`; logic after that point cannot be audited."

**INCOMPLETE FIELD FAILURE MODE**:

If source ends with a comment suggesting a field (e.g., `# Per-org PII`) but no field definition follows, state:

"Source truncates at line 73 after comment `# Per-org PII`; if this field exists beyond truncation, its type, constraints, and default are unknown. If this is a JSONField with no validator, malformed JSON can crash read paths; if ForeignKey with on_delete=CASCADE, deletion of referenced object will cascade without visible verification."

#### assumptions

List implicit assumptions embedded in VISIBLE code. For each, quote the exact expression and explain what breaks if violated.

**HIGH-VALUE ASSUMPTIONS**:
- **Field defaults**: `is_active=True` at line 45 — assumes new users are active; if registration workflow requires email verification before activation, this default bypasses the gate.
- **Unique constraints**: `email = models.EmailField(unique=True)` — assumes email is stable identifier; if users can change email, old email becomes inaccessible but unique constraint prevents reuse, orphaning the address.
- **ForeignKey null constraints**: `invited_by = models.ForeignKey("self", ..., null=True, default=None)` — assumes invited_by can be unknown; if analytics require tracking invitation chains, null breaks the chain.

**IF TRUNCATED**: State "Cannot verify assumptions in code beyond line X."

#### resource_obligations

Look for: database transactions, S3 writes, async task spawning, global state mutation, ContextVar management.

If none visible in truncated source, write `"[none detected in visible source; source truncates at line X]"`.

### Step 6: Close understanding object (DO NOT CLOSE ROOT YET)

```json
  }
```

**WAIT — DO NOT CLOSE ROOT OBJECT YET. YOU MUST BACKFILL INVARIANTS AND VIOLATIONS FIRST.**

### Step 7: MANDATORY BACKFILL — Invariants

Scroll back to the line where you wrote `"invariants":[],`

Replace the `[]` with your actual invariants array.

**INVARIANT DEBT RULE**:

Count your `pattern_counts`. Each non-zero count requires ≥1 invariant.

Before submission:
- `incomplete_field_definitions: 1` → must have ≥1 `intent_gap` invariant stating what cannot be verified
- `except_clauses: 1` → must have ≥1 error_contract or intent_gap invariant
- `truncated_bodies: 1` → must have ≥1 intent_gap invariant at confidence 0.70

If `sum(non_zero_pattern_counts) > len(invariants)`, you have failed.

**MANDATORY INVARIANT FOR INCOMPLETE FIELD AT TRUNCATION**:

If source ends with a comment (e.g., `# Per-org PII` at line 73) but no field definition follows:

```json
{
  "id": "inv_001",
  "type": "intent_gap",
  "statement": "Source truncates at line 73 after comment `# Per-org PII`; field definition incomplete or invisible. Cannot verify: (1) field name, (2) field type (CharField? JSONField? ForeignKey?), (3) null/blank constraints, (4) default value, (5) on_delete cascade behavior if ForeignKey, (6) validators/choices if CharField. If this field exists beyond visible source and lacks schema validation, malformed data can propagate to reads; if ForeignKey with CASCADE, deletion of referenced object will cascade-delete AgentccOrgConfig records.",
  "applies_to": ["AgentccOrgConfig.<unknown_field>"],
  "formal": "∀ field f where comment_suggests(f) ∧ definition_invisible(f): field_type(f) ∧ constraints(f) ∧ cascade_semantics(f) ∧ validation(f) unverifiable",
  "derived_from": "# Per-org PII",
  "confidence": 0.70
}
```

**CALIBRATION**: Confidence scores MUST span ≥0.25. Directly quoted lines → 0.90–0.97. One-step inference → 0.70–0.89. Indirect → 0.60–0.69.

**INVARIANT QUALITY TEST**: Could this have been written without reading the code? If yes, rewrite to reference a specific line/threshold/choice.

### Step 8: MANDATORY BACKFILL — Violations

Replace `"violations":[]` with actual violations.

**CONTRACT-FIRST GATE**: A violation is valid only if it contradicts a specific claim in `behavioral_contract` or `purpose`. Before writing, complete: "The code claims [X]. The visible code fails to deliver [X] because [Y]." If you cannot complete this from visible code, do not emit.

**HALLUCINATION GATE**: Do not report violations in code not present in `visible_definitions`.

**VIOLATIONS FORCING RULE**: If you have invariants but zero violations, you MUST add a sibling field `"violations_audit":` (string) explaining for each invariant type why no violation exists in visible code.

### Step 9: Close root object

```json
}
```

---

## PRE-FLIGHT PATTERN COUNT — MANDATORY

Before writing ANY output, scan visible source and count:

- `except` clauses: ___
- `timeout=` parameters: ___
- `re.compile`/`re.match`/`re.search` calls: ___
- Hard-coded numeric constants used as thresholds: ___
- `status_code` checks: ___
- Functions whose body is truncated (def line visible, body cut off): ___
- Bare `def` lines with NO body at all: ___
- `return None` / `return []` / `return "fallback"` on non-success paths: ___
- Commented-out validation/error-handling code: ___
- Transaction decorators (`@transaction.atomic`, `select_for_update()`): ___
- `finally:` clauses that clear state without restoration: ___
- Sentinel objects (e.g., `_UNSET = object()`) lacking type hints: ___
- Incomplete field definitions:
  - Lines ending with `=` but no value: ___
  - Comments suggesting fields (e.g., `# Per-org PII`) with no field definition following before truncation: ___

Write these counts under `"pattern_counts"` inside `truncation_audit`.

**INVARIANT DEBT VERIFICATION**: Before closing the document, verify `invariants.length >= number of non-zero pattern_counts`. If this check fails, you have failed.

---

## PART 2 — INVARIANTS

Based ONLY on `visible_definitions`. Every invariant must pass: can you quote the exact source line in `derived_from`? If not, do not list it.

**MANDATORY SOURCES** — missing these when the pattern is visible = failure:

1. `except` not re-raised → `error_contract`.
2. `re.match`/`re.search` → `data_flow` about what is missed.
3. Hard-coded limit → `assumption` about silent truncation.
4. Incomplete field definition at truncation point → `intent_gap`, confidence 0.70, stating what cannot be verified.
5. Comment suggesting field with no definition following → `intent_gap`, confidence 0.70, stating field name/type/constraints unknown.
6. Commented-out validation → `intent_gap`, confidence 0.95.
7. TODO/FIXME/BUG → `intent_gap`, confidence 0.95.
8. Git commit "fix X" with no visible guard → `intent_gap`, confidence 0.85.
9. Docstring claim absent from code → `intent_gap`, confidence 0.90.
10. Truncated function body → `intent_gap`, confidence 0.70.
11. Bare `def` no body → `intent_gap`, confidence 0.70.

---

## FINAL SELF-CHECK (verify ALL before final `}`)

1. **FIRST 21 CHARACTERS**: Are your literal first 21 characters `{"truncation_audit":{`? If you wrote `{"path":` or `{"understanding":` or anything else, output is invalid.
2. `truncation_audit` contains `pattern_counts` with all keys.
3. `"invariants":` appears immediately after `truncation_audit` closes.
4. `"violations":` appears before `"understanding"`.
5. `understanding` has all seven fields, each properly closed with `"`.
6. `key_abstractions` is NOT empty (if any definitions exist).
7. Outer `}` closes root object.
8. No field references names absent from `visible_definitions`.
9. Every `derived_from` is verbatim quote from visible source.
10. Confidence scores span ≥0.25 (if invariants non-empty).
11. No understanding field exceeds dynamic word limit.
12. Exactly four top-level keys: `truncation_audit`, `invariants`, `violations`, `understanding`. No `path`/`file`/`module`.
13. Each non-zero pattern_count has ≥1 corresponding invariant.
14. If incomplete field definitions exist (comment with no field following), ≥1 `intent_gap` invariant documenting unverifiable semantics.
15. If `violations` is `[]` and `invariants` non-empty, `violations_audit` field exists.
16. No understanding field truncated mid-sentence without `[budget limit]` closure.
17. Neither `invariants` nor `violations` is still placeholder `[]` after backfill (if patterns exist).
18. **NO PATH KEY**: String `"path":` does not appear as top-level key.
19. **EMISSION ORDER VERIFIED**: Did you write truncation_audit → `"invariants":[]` → `"violations":[]` → understanding → backfill? If you wrote understanding before the placeholders, output may be truncated.
20. **HALLUCINATION CHECK**: Every method/field discussed in understanding appears in `visible_definitions`. If `visible_definitions` has 9 entries and you discussed 15 fields, you hallucinated 6 invisible fields.
21. **INCOMPLETE FIELD AT TRUNCATION**: If last visible line is a comment suggesting a field (e.g., `# Per-org PII`) with no field definition following, you emitted an `intent_gap` invariant stating field name/type/constraints unknown.

---

Return JSON only — no markdown fences, no explanation:

{
  "truncation_audit": {
    "last_complete_unit": "...",
    "cutoff_line": "...",
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
      "silent_fallback_returns": 0,
      "commented_out_code": 0,
      "transaction_blocks": 0,
      "finally_without_restoration": 0,
      "sentinel_without_types": 0,
      "incomplete_field_definitions": 0
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