# Prompt: Module Understanding

You are performing semantic analysis for a formal verification pipeline. Return ONLY valid JSON. No markdown fences, no explanation outside the JSON.

<budget:token_budget>1000000</budget:token_budget>

## CRITICAL: Schema Violation = Complete Failure

The parser has ZERO error recovery. ANY schema deviation destroys the entire pipeline:
- Top-level 'path' key → parser crash (THIS IS THE #1 FAILURE MODE — 'path' is NOT in schema)
- Top-level 'file' key → parser crash
- Top-level 'module' key → parser crash
- Wrong key order → parser crash
- Missing closing brace → parser crash
- Unclosed string in understanding → parser crash
- Empty invariants array when source truncates → analysis failure
- Describing code beyond cutoff_line as if visible → verification failure
- Referencing line numbers > cutoff_line → hallucination failure

Your output will be parsed by a strict JSON validator that expects EXACTLY this structure.

## MANDATORY FIRST ACTION: Count Visible Lines

Before you write ANY output, perform this test:

1. Locate the line "```python" below in this prompt. The source code starts on the NEXT line.
2. Count down from that line. Each line gets a number: 1, 2, 3, ...
3. Locate the line "```" that closes the python block. The source code ends on the PREVIOUS line.
4. The LAST line of visible source is your cutoff_line. This line number is your MAXIMUM. You CANNOT reference any line number greater than this.
5. Quote cutoff_line EXACTLY as written, even if it's an incomplete statement like 'or os.environ.get("ANTHROPIC_AUTH_TOKEN' with no closing quote.

**EXAMPLE**: If you count 66 lines, you CANNOT write "lines 68-75" or "line 177" or "resolve_model (lines 113-123)". These line numbers do not exist. Writing them is HALLUCINATION.

## PRE-FLIGHT CHECK: What Character Sequence Starts Your Output?

Stop. Before typing anything, answer internally:
- What are the first 5 characters I will type? (Answer: {"tru)
- What are characters 6-21? (Answer: ncation_audit":{)
- Does the word "path" appear as a top-level key? (Answer: NO — this key does not exist in schema)
- Does the word "file" appear as a top-level key? (Answer: NO)
- Does the word "module" appear as a top-level key? (Answer: NO)
- What is the second top-level key after truncation_audit? (Answer: invariants)

If you cannot answer these correctly, you will generate invalid output. Re-read the schema template below.

## Schema Template (memorize this structure)

```json
{
  "truncation_audit": {
    "last_complete_unit": "string",
    "cutoff_line": "string",
    "visible_definitions": ["array"],
    "docstring_only_names": ["array"],
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
    "purpose": "string",
    "design_intent": "string",
    "key_abstractions": "string",
    "behavioral_contract": "string",
    "failure_modes": "string",
    "assumptions": "string",
    "resource_obligations": "string"
  }
}
```

**FORBIDDEN TOP-LEVEL KEYS**: path, file, module, source, filename, analysis, summary, metadata. ONLY the four keys shown above are valid.

## Project Context

{{project_essence}}

## Module to Analyze

File: {{filename}}
Full path: {{file_path}}

```python
{{source}}
```

{{truncation_note}}

## Declared Intent

### Git history
```
{{git_log}}
```

### Intent signals
```
{{intent_signals}}
```

{{graphify_rationale}}

## CRITICAL REALITY CHECK: What Source Is Actually Visible?

Before you write ANYTHING:

1. Count the lines between ```python and closing ```. If you count 66 lines, then line 66 is your cutoff_line.
2. Scan for function/class definitions with COMPLETE bodies. A function ending at line 66 with 'or os.environ.get("ANTHROPIC_AUTH_TOKEN' (unclosed string, no return statement) is NOT complete.
3. The LAST complete unit is the last function/class with a full definition. If resolve_key starts at line 50 and truncates incomplete at line 66, then the LAST complete unit is whatever comes before line 50 (likely make_client at lines 20-47).
4. visible_definitions: ONLY include constructs with complete definitions. Mark incomplete ones as "function_name (incomplete)".
5. docstring_only_names: Scan module docstring (lines before first import) and git history for mentions of functions/classes/commands not in visible_definitions.

## LINE NUMBER HALLUCINATION DETECTION

If you are about to write:
- "resolve_key returns empty string (lines 62-66)" when cutoff_line is line 66 ending mid-statement
- "lines 68-75" when visible source has only 66 lines
- "_call (lines 113-123)" when source ends before line 100

STOP. You are hallucinating code that does not exist in visible source. Re-count the lines between ```python and ```.

**TEST**: What is the line number of the closing ```? Subtract 1. That is your maximum line number. You cannot reference anything beyond it.

## Emission Protocol (MANDATORY SEQUENCE)

### Step 0: Pre-emission verification (DO NOT SKIP)

1. Count visible lines between ```python and ```. This is your line_count.
2. Find the LAST line of visible source (line_count). Quote it verbatim, even if incomplete. This is cutoff_line.
3. Find the LAST complete syntactic unit (function with full body, complete import, complete assignment). This is last_complete_unit.
4. Scan visible source for functions/classes with COMPLETE bodies. These go in visible_definitions. Functions truncating mid-body are marked "(incomplete)".
5. Scan module docstring for function names, CLI commands, tool mentions not in visible_definitions. These go in docstring_only_names.
6. COMMIT: The first 21 characters you type are: {"truncation_audit":{

### Step 1: Write truncation_audit

Start typing NOW. First character: {
Characters 2-21: "truncation_audit":{

**DO NOT WRITE**: {"path": or {"file": or {"module": — these keys do not exist.

**last_complete_unit**: Quote the name and line range of the last construct with 100% complete definition. If resolve_key truncates at line 66 mid-statement, last_complete_unit is likely "make_client function (lines 20-47)".

**cutoff_line**: Quote the EXACT text of the last visible line, even if incomplete: 'or os.environ.get("ANTHROPIC_AUTH_TOKEN'

**visible_definitions**: List constructs with complete definitions. If resolve_key is incomplete, write "resolve_key (incomplete)".

**docstring_only_names**: CRITICAL. List ALL names mentioned in docstring/comments/git history that are NOT in visible_definitions. If docstring mentions resolve_model but it's not visible, add "resolve_model". If git history mentions a function not visible, add it.

**pattern_counts**: Count ONLY in visible source:
- incomplete_field_definitions: 1 if cutoff_line is mid-statement (unclosed string, unclosed paren, no semicolon)
- truncated_bodies: 1 if any function definition visible but body incomplete
- All other patterns: count visible occurrences

### Step 2: Write invariants placeholder

```json
"invariants":[],
```

### Step 3: Write violations placeholder

```json
"violations":[],
```

### Step 4: Write understanding

```json
"understanding":{
```

**Token budget**: 120 words per field max if visible_definitions ≤ 5.

**CRITICAL: Never describe functions not in visible_definitions as if they exist in visible source.**

#### understanding.purpose

**IF source truncates mid-function:**

Mandatory format: "Source truncates at line X in [construct]. Last complete: [quote last_complete_unit]. Cutoff line X: '[quote cutoff_line verbatim]'. Module docstring (lines A-B) claims '[quote claim]'. Visible: [list visible_definitions]. [If docstring_only_names non-empty: 'Docstring/git mentions [list items] but none visible.'] Cannot verify [list 2-3 specific capabilities]."

**EXAMPLE for llm.py**:
"Source truncates at line 66 mid-statement in resolve_key. Last complete: make_client (lines 20-47). Line 66: 'or os.environ.get("ANTHROPIC_AUTH_TOKEN' — unclosed string, no closing paren, no return statement. Module docstring describes 'Unified LLM client factory — Anthropic API, bearer proxies (Bonsai), OpenRouter.' Visible: DEFAULT_MODEL, _KEY_HELP, make_client (complete), resolve_key (incomplete). Cannot verify: resolve_key return logic for bearer token case, RuntimeError raising when no auth, or if resolve_model function exists (not visible in source)."

**FORBIDDEN**: "resolve_key (lines 62-66) returns empty string when ANTHROPIC_AUTH_TOKEN set" when line 66 is cutoff_line ending mid-statement with no return visible.

#### understanding.design_intent

**IF source truncates:**

"Docstring describes [design] but truncation prevents full verification. Visible design decisions: [for EACH item in visible_definitions with complete body, state ONE design decision observable in its implementation with line numbers]."

**EXAMPLE for llm.py**:
"Docstring describes multi-provider auth but resolve_key truncates before return logic. Visible design decisions: (1) make_client (lines 20-47) defers 'import anthropic' to line 27 (not module-level) — allows llm.py import when anthropic package absent; consequence: ImportError surfaces at call-time not import-time. (2) Key priority: PACT_LLM_API_KEY (line 30) → PACT_ANTHROPIC_API_KEY (line 31) → ANTHROPIC_API_KEY (line 32) — PACT_* namespace prevents shell ANTHROPIC_API_KEY from shadowing proxy keys (git: 'so proxy keys aren't shadowed'). (3) _KEY_HELP (lines 10-17) documents OpenRouter as 'https://openrouter.ai/api' not '/api/v1' — base_url omits /v1 because SDK appends /v1/messages automatically."

#### understanding.key_abstractions

**Mandatory format**: "visible_definitions: [exact list]. [If docstring_only_names non-empty: 'Docstring/git mentions [list] but not visible (truncates line X).'] [For each visible definition: 1-2 sentences describing its structure/role with line numbers]."

**EXAMPLE for llm.py**:
"visible_definitions: [DEFAULT_MODEL, _KEY_HELP, make_client, resolve_key (incomplete)]. DEFAULT_MODEL (line 8): string constant 'claude-sonnet-4-6', default when PACT_LLM_MODEL unset. _KEY_HELP (lines 10-17): multi-line string documenting three auth configs, raised in RuntimeError when no auth; specifies OpenRouter base as https://openrouter.ai/api not /api/v1 because SDK appends /v1/messages. make_client (lines 20-47): factory taking optional api_key, returns anthropic.Anthropic; implements 5-tier key priority, base_url override via PACT_LLM_BASE_URL or ANTHROPIC_BASE_URL (lines 35-37), raises RuntimeError with _KEY_HELP if no auth (line 39), defers anthropic import to line 27. resolve_key (lines 50-66, incomplete): docstring claims returns str (non-empty key or '' for bearer auth), raises RuntimeError if no auth, but source truncates line 66 mid-statement: 'or os.environ.get("ANTHROPIC_AUTH_TOKEN' — no closing quote, no return visible."

#### understanding.behavioral_contract

**IF function incomplete:**

"Only [list complete constructs] visible with complete definitions. [For each complete: quote contract from docstring + implementation, with line numbers]. [For incomplete: state truncation as contract verification failure]."

**EXAMPLE for llm.py**:
"Only make_client (lines 20-47) visible with complete definition. make_client contract: accepts optional api_key str, returns anthropic.Anthropic; raises RuntimeError if no key and no ANTHROPIC_AUTH_TOKEN (line 39); no try/except visible for anthropic import (line 27) so ImportError propagates to caller. resolve_key contract (incomplete): docstring claims returns str (non-empty or ''), raises RuntimeError if no auth, but source truncates before return statements visible (line 66 mid-statement) — cannot verify contract enforcement."

#### understanding.failure_modes

**Mandatory**: For each try/except visible, quote verbatim with line numbers. For truncated functions, state truncation as failure mode.

**EXAMPLE for llm.py**:
"No try/except blocks visible. make_client (lines 20-47) imports anthropic line 27 with no exception handling — failure mode: anthropic package not installed raises ImportError at call-time, error message is default Python 'No module named anthropic' with no guidance. make_client line 39 raises RuntimeError with _KEY_HELP when no auth — failure mode: error documents ANTHROPIC_AUTH_TOKEN but doesn't explain bearer tokens only work with Bonsai-style proxies. resolve_key truncates line 66 mid-statement: 'or os.environ.get("ANTHROPIC_AUTH_TOKEN' — failure mode: cannot audit return logic, error handling, or RuntimeError raising; logic after line 66 unverifiable."

#### understanding.assumptions

**IF minimal visible code:**

"[For each complete function: list assumptions observable in implementation with line numbers]. [For incomplete: state assumption verification blocked by truncation]."

**EXAMPLE for llm.py**:
"make_client assumes anthropic package installed (import line 27, no try/except) — breaks if package absent. make_client assumes base_url strings in PACT_LLM_BASE_URL or ANTHROPIC_BASE_URL omit /v1 path (SDK appends /v1/messages) — breaks if base_url='https://proxy.com/api/v1' produces /api/v1/v1/messages. make_client assumes ANTHROPIC_AUTH_TOKEN read by SDK automatically for bearer auth (no explicit handling beyond line 39 check) — implicit coupling with SDK internals. resolve_key (incomplete) appears to assume caller distinguishes '' return (bearer case) from non-empty (x-api-key case) — cannot verify assumption because source truncates line 66 before return logic."

#### understanding.resource_obligations

**IF no file I/O visible:**

"No file handles, network sockets, or async tasks visible. [For each function returning resource: state obligation]. [For incomplete: state obligation unverifiable]."

**EXAMPLE for llm.py**:
"No file handles, network sockets, or async visible. make_client (lines 20-47) returns anthropic.Anthropic client — obligation: caller must close client when done (anthropic SDK uses httpx with connection pooling), but anthropic.Anthropic does not expose .close() in v0.18+, uses context manager for streaming only — in practice, obligation is 'reuse single client instance, do not create per request'. resolve_key (incomplete, truncates line 66) returns str — obligation: caller must not mutate string (Python strings immutable, no obligation)."

### Step 5: Close understanding

```json
  }
```

### Step 6: Backfill invariants

**MANDATORY**: If source truncates mid-function, generate inv_truncation_NNN invariant. For EACH name in docstring_only_names, generate inv_intent_gap_NNN invariant.

**Format for truncation invariant**:
```json
{
  "id": "inv_truncation_001",
  "type": "intent_gap",
  "statement": "[function_name] truncates at line X: visible line X is '[quote cutoff_line verbatim]' with [describe incompleteness: unclosed string/paren/bracket, no return statement]. Cannot verify: (1) [specific behavior from docstring], (2) [specific behavior], (3) [specific behavior].",
  "applies_to": ["module.function_name"],
  "formal": "∀ f ∈ visible_functions: truncated(f.body) → ∀ behavior ∈ f.post_truncation: unverifiable(behavior)",
  "derived_from": "Source line X last visible line mid-statement; [function_name] signature complete but body incomplete",
  "confidence": 0.70
}
```

**EXAMPLE for llm.py**:
```json
{
  "id": "inv_truncation_001",
  "type": "intent_gap",
  "statement": "resolve_key truncates at line 66: visible line 66 is 'or os.environ.get(\"ANTHROPIC_AUTH_TOKEN' with no closing quote, no closing paren, no return statement. Cannot verify: (1) return value when ANTHROPIC_AUTH_TOKEN set but no x-api-key (docstring claims returns empty string ''), (2) RuntimeError raising when no auth configured (docstring claims raises), (3) string formatting/encoding of returned key.",
  "applies_to": ["llm.resolve_key"],
  "formal": "∀ f ∈ visible_functions: truncated(f.body) → ∀ behavior ∈ f.post_truncation: unverifiable(behavior)",
  "derived_from": "Source line 66 last visible line mid-statement; resolve_key signature complete but body incomplete",
  "confidence": 0.70
}
```

**Format for intent_gap invariant** (when docstring_only_names non-empty):
```json
{
  "id": "inv_intent_gap_001",
  "type": "intent_gap",
  "statement": "Docstring/git mentions [name] but [name] not visible in source (truncates at line X). Cannot verify if [name] exists, accepts expected inputs, implements described logic, or returns documented output.",
  "applies_to": ["module.name"],
  "formal": "∀ mentioned_name: (docstring_mentions(name) ∨ git_mentions(name)) ∧ ¬visible(name.body) → unverifiable(name.behavior)",
  "derived_from": "Docstring/git mentions [name]; visible_definitions: [list]; [name] not in visible_definitions",
  "confidence": 0.70
}
```

**MANDATORY**: If source truncates mid-function, invariants.length >= 1. If docstring_only_names.length > 0, invariants.length >= 1 + docstring_only_names.length.

### Step 7: Backfill violations

**Mandatory if source truncates before critical functionality:**

```json
{
  "id": "viol_contract_001",
  "type": "contract_violation",
  "statement": "Module docstring line X describes module as '[quote]' implying [capability], but source truncates at line Y before [construct] completes. Last complete: [quote last_complete_unit]. Line Y: '[quote cutoff_line]'. Cannot verify: (1) [specific capability], (2) [specific capability], (3) [specific capability]. [If docstring_only_names non-empty: 'Docstring/git mentions [list items] but none visible.'] Consumers calling [incomplete construct] cannot rely on documented behavior.",
  "severity": "medium",
  "applies_to": ["module"],
  "confidence": 0.85
}
```

**EXAMPLE for llm.py**:
```json
{
  "id": "viol_contract_001",
  "type": "contract_violation",
  "statement": "Module docstring line 1 describes 'Unified LLM client factory — Anthropic API, bearer proxies (Bonsai), OpenRouter' implying complete multi-provider support, but source truncates at line 66 before resolve_key completes. Last complete: make_client (lines 20-47). Line 66: 'or os.environ.get(\"ANTHROPIC_AUTH_TOKEN' — unclosed string, no return visible. Cannot verify: (1) resolve_key return logic for bearer token case (ANTHROPIC_AUTH_TOKEN set but no x-api-key), (2) RuntimeError raising when no auth configured, (3) resolve_model function for PACT_LLM_MODEL override (not visible in source). make_client is complete and implements auth priority + base_url override, but resolve_key incompleteness means consumers calling resolve_key directly cannot rely on documented behavior.",
  "severity": "medium",
  "applies_to": ["module"],
  "confidence": 0.85
}
```

### Step 8: Close root object

```json
}
```

**FINAL VERIFICATION CHECKLIST**:
- [ ] First 21 chars: {"truncation_audit":{
- [ ] NO 'path', 'file', or 'module' top-level keys
- [ ] cutoff_line quotes EXACT last visible line verbatim
- [ ] visible_definitions marks incomplete constructs as "(incomplete)"
- [ ] docstring_only_names populated with ALL names from docstring/git not in visible_definitions
- [ ] invariants.length >= 1 if source truncates mid-function
- [ ] Every understanding field closed with "
- [ ] No line numbers > cutoff_line referenced anywhere
- [ ] Root object closes with }

Return JSON only. Begin typing NOW with character `{`: