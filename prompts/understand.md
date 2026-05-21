# Prompt: Module Understanding

You are building a world model of a software project. You have already
established the project's essence (below). Now you are building a deep
understanding of one specific module within it.

## Project context

{{project_essence}}

## Module to analyse

File: {{filename}}

```python
{{source}}
```

{{truncation_note}}

---

## Your task — three parts, in strict order

Do NOT jump ahead. Complete Part 1 fully before writing Part 2.
Complete Part 2 fully before writing Part 3.

### PART 1 — UNDERSTAND (do this first)

Read every line. Then write:

**purpose**
What specific problem does THIS module solve within the larger project?
Why does it exist as a separate file — what would break or become incoherent
if it were merged into another module? Name the actual functions, classes,
and constants that make it what it is. Write 3-5 paragraphs. Generic answers
like "this module processes data" are wrong — be specific to this code.

**design_intent**
What design decisions are visible in this code? Examples of what to look for:
- Why LRU cache? (performance? correctness? both?)
- Why dataclass not dict? (what invariant does that enforce?)
- Why frozenset not list? (immutability — what does that protect against?)
- Why module-level constants not class attributes?
- Why AST not regex? (what class of errors does AST catch that regex misses?)
Explain what the structure of this code tells you about what the author was
optimizing for. 2-4 paragraphs.

**key_abstractions**
What are the main types, concepts, and data flows in this module?
Name them explicitly. How do they relate to each other? What does an instance
of the core type look like in memory? What transformations does this module
perform on data as it flows through?

**behavioral_contract**
What does this module promise its callers? Be precise:
- What can callers RELY on? (not "it works correctly" — specific guarantees)
- What will this module NEVER do? (what errors does it absorb vs propagate?)
- What are the return type guarantees? (always a list? never None? always sorted?)
- What are the side effect guarantees? (always pure? mutates in place? idempotent?)

**failure_modes**
What can go wrong? Look at the actual error handling in the code:
- What errors are explicitly caught and handled here?
- What errors are intentionally propagated to callers?
- What edge cases does this code handle? (empty inputs? None values? missing keys?)
- What edge cases does this code NOT handle — trusting callers to prevent them?

**assumptions**
What does this code believe about its inputs, environment, and callers?
Look for implicit assumptions — things the code does without checking:
- Accesses dict keys without .get()? → assumes key always present
- Calls .strip() directly? → assumes value is not None
- Uses [0] indexing? → assumes list is non-empty
List these explicitly. These are the load-bearing beliefs of the module.

### PART 2 — INVARIANTS (derived from Part 1 only)

Based ONLY on what you understood in Part 1 — not on general Python
conventions — what must be true for this module to work as its author intended?

For each invariant:
- **id**: inv_NNN (sequential)
- **type**: nullable_contract | async_contract | error_contract | guard_requirement | data_flow | uniqueness | other
- **statement**: plain English — what must always be true
- **applies_to**: list of actual function/class names from this code
- **formal**: semi-formal (∀ calls to f. result ≠ None  /  always: x checked before y)
- **derived_from**: quote the specific part of your Part 1 analysis that implies this
- **confidence**: 0.0–1.0 — how certain are you this is a real invariant vs a pattern you imposed?

Do not list invariants with confidence < 0.6. Do not list more than 8.

### PART 3 — VIOLATIONS (derived from Part 2 only)

For each invariant in Part 2, examine the actual code:
Does the code satisfy this invariant everywhere it applies?

For each violation found:
- **invariant_id**: which invariant is violated
- **line**: exact line number
- **evidence**: quote the specific code that violates the invariant
- **severity**: critical (crashes in production) | high (data corruption or silent failure) | medium (degraded behavior) | low (edge case)
- **explanation**: given THIS module's stated intent — not external conventions —
  why does this violation matter? What specifically breaks if it triggers?

Only report violations you are confident about. A missing None check that the
module's own behavioral_contract says should never happen is a violation.
A missing None check on an input the module's assumptions say will always be
non-None is NOT a violation.

---

Return JSON only — no markdown fences, no explanation outside the JSON:

{
  "understanding": {
    "purpose": "...",
    "design_intent": "...",
    "key_abstractions": "...",
    "behavioral_contract": "...",
    "failure_modes": "...",
    "assumptions": "..."
  },
  "invariants": [...],
  "violations": [...]
}
