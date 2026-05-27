# Prompt: Module Understanding

You are performing semantic analysis for a formal verification pipeline. Return ONLY valid JSON. No markdown fences, no explanation outside the JSON.

<budget:token_budget>1000000</budget:token_budget>

## SCHEMA VIOLATION = CATASTROPHIC FAILURE

The parser has ZERO error recovery. The #1 failure mode is emitting a top-level 'path' key—THIS KEY DOES NOT EXIST IN THE SCHEMA and crashes the parser immediately.

**FORBIDDEN TOP-LEVEL KEYS** (emitting any of these destroys the pipeline):
- "path"
- "file" 
- "module"
- "filename"
- "source"
- "metadata"
- "analysis"
- "summary"

**MANDATORY TOP-LEVEL KEYS** (in this exact order):
1. "truncation_audit"
2. "invariants"
3. "violations" 
4. "understanding"

EMISSION TEST: What are the first 21 characters you will type?
CORRECT ANSWER: {"truncation_audit":{
INCORRECT (parser crash): {"path": or {"file": or {"module":

If you cannot state with certainty that your first 21 characters are {"truncation_audit":{ then STOP and re-read this section.

## MANDATORY FIRST ACTION: Count Visible Lines

Before writing ANY output:

1. Locate the line "```python" below. Source code starts on the NEXT line.
2. Count lines from 1 until you reach the closing "```". The line BEFORE the closing "```" is your cutoff_line.
3. That line number is your MAXIMUM. You CANNOT reference line numbers greater than this.
4. Quote cutoff_line EXACTLY as written, even if it's incomplete: 'site = dat'

**EXAMPLE**: If you count 69 lines, you CANNOT write "build_call_graph (lines 73-98)" or "lines 86-88". Line 69 is the last line that exists. Writing "line 73" is HALLUCINATION.

## LINE NUMBER HALLUCINATION = ANALYSIS FAILURE

If the visible source ends at line 69 with an incomplete statement 'site = dat', and you write:
- "build_call_graph (lines 73-98)" — you are describing code that does not exist
- "short_to_qual dictionary maps (line 88)" — you are referencing lines beyond the cutoff
- "call_sites_to returns list[CallSite] (lines 60-69)" when line 69 has incomplete variable assignment — you are describing behavior not visible in source

TEST: What is the line number of the closing ```? Subtract 1. That is cutoff_line. You cannot reference anything beyond it.

## PRE-FLIGHT CHECK

Before typing, answer internally:
- First 5 characters? (Answer: {"tru)
- Characters 6-21? (Answer: ncation_audit":{)
- Does "path" appear as top-level key? (Answer: NO—forbidden key)
- Does "file" appear as top-level key? (Answer: NO—forbidden key)
- Second top-level key after truncation_audit? (Answer: invariants)
- What is cutoff_line number? (Count lines between ```python and ```)
- What is the EXACT text of cutoff_line? (Quote verbatim even if incomplete)

## Schema Template

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

## REALITY CHECK: What Source Is Actually Visible?

Before writing anything:

1. Count lines between ```python and closing ```. If 69 lines, line 69 is cutoff_line.
2. Find functions/classes with COMPLETE bodies. A function ending at line 69 with 'site = dat' (incomplete assignment, no return) is NOT complete.
3. last_complete_unit = last function/class with full definition. If call_sites_to starts line 60 and truncates incomplete at line 69, then last_complete_unit is callers_of (lines 53-58).
4. visible_definitions: ONLY include constructs with complete definitions OR mark incomplete as "function_name (lines X-Y, incomplete)".
5. docstring_only_names: Scan module docstring and git history for function/class names NOT in visible_definitions.

## Emission Protocol

### Step 0: Pre-emission verification

1. Count visible lines between ```python and ```. This is line_count.
2. Last line of visible source = cutoff_line. Quote verbatim even if incomplete.
3. Find LAST complete syntactic unit (function with full body including return, complete class). This is last_complete_unit.
4. Scan visible source for functions/classes with COMPLETE bodies → visible_definitions. Truncated mid-body → mark "(lines X-Y, incomplete)".
5. Scan module docstring for names NOT in visible_definitions → docstring_only_names.
6. COMMIT: First 21 characters = {"truncation_audit":{

### Step 1: Write truncation_audit

Start typing. First character: {
Characters 2-21: "truncation_audit":{

**last_complete_unit**: Name + line range of last construct with 100% complete definition. If call_sites_to truncates at line 69, last_complete_unit is "callers_of method (lines 53-58)".

**cutoff_line**: EXACT text of last visible line, even if incomplete: "        site = dat"

**visible_definitions**: List constructs. Mark incomplete explicitly:
- "CallGraph class (lines 22-69, incomplete)" — class definition visible but truncates mid-method
- "call_sites_to method (lines 60-69, incomplete)" — signature complete but body incomplete
- "callers_of method (lines 53-58)" — complete with return statement

**docstring_only_names**: CRITICAL. List ALL names in docstring/comments/git NOT in visible_definitions. If module docstring mentions no functions beyond visible, this is [].

**pattern_counts**:
- incomplete_field_definitions: 1 if cutoff_line mid-statement (incomplete assignment 'site = dat')
- truncated_bodies: count functions with visible signature but incomplete body
- All others: count visible occurrences ONLY (no hallucination beyond cutoff_line)

### Step 2: Write invariants

**MANDATORY if source truncates mid-function**: Generate inv_truncation_NNN invariant.

**Format** (use EXACT cutoff_line text):
```json
{
  "id": "inv_truncation_001",
  "type": "intent_gap",
  "statement": "[function] truncates at line X: visible line X is '[quote cutoff_line verbatim including whitespace]' with [describe incompleteness: unclosed paren/string/incomplete assignment/no return]. Cannot verify: (1) [specific behavior from type hint/docstring], (2) [specific behavior], (3) [specific behavior].",
  "applies_to": ["module.function"],
  "formal": "∀ f ∈ visible_functions: truncated(f.body) → ∀ behavior ∈ f.post_truncation: unverifiable(behavior)",
  "derived_from": "Source line X last visible mid-statement; [function] signature complete but body incomplete",
  "confidence": 0.70
}
```

**MANDATORY if docstring_only_names non-empty**: For EACH name, generate inv_intent_gap_NNN.

**SPECIFICITY REQUIREMENT FOR ALL INVARIANTS**:
- Quote actual variable names from visible code ("'site = dat' incomplete" not "variable assignment incomplete")
- Reference actual type hints ("list[CallSite] per line 60" not "returns list")
- Cite actual line numbers from visible source ONLY
- Describe actual incompleteness ("no source for 'dat', no append to sites list, no return" not "body incomplete")

### Step 3: Write violations

**MANDATORY if source truncates before critical functionality**:

```json
{
  "id": "viol_contract_001",
  "type": "contract_violation",
  "statement": "Module docstring line X describes '[quote]' implying [capability], but source truncates at line Y before [construct] completes. Last complete: [quote last_complete_unit with line range]. Line Y: '[quote cutoff_line verbatim]'. Cannot verify: (1) [specific capability from docstring], (2) [specific capability from type hint], (3) [specific error handling]. [If docstring_only_names non-empty: 'Docstring/git mentions [list] but none visible.'] Consumers calling [incomplete construct] cannot rely on documented behavior.",
  "severity": "medium",
  "applies_to": ["module.function"],
  "confidence": 0.85
}
```

**SPECIFICITY REQUIREMENT**: Quote actual docstring text with line numbers, quote actual cutoff_line, name actual constructs.

### Step 4: Write understanding

**IF source truncates mid-function**, understanding.purpose MANDATORY format:

"Source truncates at line X in [construct with exact name]. Last complete: [quote last_complete_unit with line range]. Cutoff line X: '[quote cutoff_line verbatim]'. Module docstring (lines A-B) claims '[quote claim]'. Visible: [list visible_definitions with line ranges]. [If docstring_only_names non-empty: 'Docstring/git mentions [list] but none visible.'] Cannot verify [list 2-3 specific capabilities from docstring/type hints]."

**FORBIDDEN PATTERNS**:
- "call_sites_to returns list[CallSite]" when body incomplete at cutoff_line
- "build_call_graph (lines 73-98)" when cutoff_line is 69
- Generic descriptions without line numbers: "handles errors gracefully"
- Describing behavior beyond cutoff_line

**REQUIRED PATTERN**: For EVERY claim, cite line numbers ≤ cutoff_line:
- "_require_graph method (lines 26-44): precondition checker"
- "reachable_from (lines 46-51): wraps nx.descendants"
- "call_sites_to (lines 60-69, incomplete): body truncates at 'site = dat'"

**understanding.design_intent** IF truncates:

"Docstring describes [design] but truncation prevents verification. Visible design decisions: [for EACH complete item in visible_definitions, state ONE design decision observable in implementation with line numbers ≤ cutoff_line]."

**EXAMPLE GOOD**: "_require_graph pattern (lines 26-44): centralizes two failure modes with distinct warnings (line 28-34 for missing networkx, line 36-42 for uninitialized graph). All three query methods (lines 47-48, 54-55, 61-62) call _require_graph before accessing self._g."

**EXAMPLE BAD**: "Module uses defensive programming to handle missing dependencies" (generic, no line numbers, could apply to any code).

**understanding.key_abstractions** format:

"visible_definitions: [exact list with line ranges]. [If docstring_only_names non-empty: 'Docstring/git mentions [list] but not visible (truncates line X).'] [For each visible definition: 1-2 sentences with line numbers ≤ cutoff_line describing actual implementation details]."

**understanding.behavioral_contract** IF incomplete:

"Only [list complete constructs with line ranges] visible with complete definitions. [For each complete: quote contract from docstring/type hint + describe visible implementation with line numbers]. [For incomplete: state truncation as contract verification failure with cutoff_line quote]."

**understanding.failure_modes**:

For each try/except visible, quote verbatim with line numbers. For truncated functions, state truncation as failure mode: "call_sites_to (line 60-69): truncates at 'site = dat' preventing verification of exception handling for missing 'call_site' key in edge data."

**understanding.assumptions** IF minimal visible:

"[For each complete function: list assumptions with line numbers]. [For incomplete: state assumption verification blocked by truncation]."

**understanding.resource_obligations** IF no I/O visible:

"No file handles, sockets, async visible. [For each function returning resource: state obligation]. [For incomplete: state obligation unverifiable]."

### Step 5: Close root object

```json
}
```

## FINAL VERIFICATION

- [ ] First 21 chars: {"truncation_audit":{
- [ ] NO 'path', 'file', 'module' top-level keys
- [ ] cutoff_line quotes EXACT last visible line with original whitespace
- [ ] visible_definitions marks incomplete as "(lines X-Y, incomplete)"
- [ ] docstring_only_names populated with ALL names from docstring/git NOT in visible_definitions
- [ ] invariants.length >= 1 if truncates mid-function
- [ ] No line numbers > cutoff_line anywhere in output
- [ ] Every claim in understanding cites line numbers ≤ cutoff_line
- [ ] Root closes with }

Return JSON only. Begin with `{`: