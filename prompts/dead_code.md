# Prompt: Dead Code Identification

You are auditing a software project for code that is **structurally superseded** —
modules, functions, or patterns whose purpose is now fulfilled by a newer abstraction
in the same codebase, making the old code either unreachable or redundant.

This is not style cleanup. Dead code in this sense means:
- A pipeline step that the new architecture bypasses
- A function whose callers have all been replaced
- A generation of an abstraction that a newer one fully subsumes
- A file that exists only because the refactor that created its replacement
  didn't finish removing the old path

## Project context

{{project_essence}}

## Architecture transitions (what replaced what)

{{architecture_transitions}}

## Module analyses

{{module_summaries}}

---

## PART 0 — READ BEFORE CLAIMING

For each candidate you identify, use `read_file_lines` to confirm:
1. The function/module exists (check the def line)
2. No live caller exists that ONLY calls the old path (grep mentally across the module analyses)
3. The new abstraction actually covers the old one's use case

Do NOT claim something is dead unless you can name the specific replacement.

---

## PART 1 — DEAD CODE CANDIDATES

For each candidate:

**Name**: function name or module path
**Why superseded**: one sentence naming the specific replacement
**Replacement**: the function/class/module that now covers this
**Evidence of non-use**: which callers have migrated? any remaining callers?
**Risk of removal**: LOW (no callers), MEDIUM (some callers exist but shouldn't), HIGH (callers exist and migration is non-trivial)
**Fix class**: `remove` | `replace_callsite` | `deprecate_with_warning`

---

## PART 2 — REMOVAL PATCHES

For each LOW-risk candidate: produce a removal patch.
For each MEDIUM-risk candidate: produce a deprecation warning patch.
Skip HIGH-risk candidates — flag them for human review.

Use the same patch format as heal.md:
- `original`: verbatim block from the file
- `replacement`: empty string (for remove) or deprecation stub

---

Return JSON only:

{
  "dead_code_candidates": [
    {
      "name": "function_or_module_name",
      "file": "path/to/file.py",
      "line": 0,
      "why_superseded": "...",
      "replacement": "NewFunction or new_module.py",
      "remaining_callers": ["file:line", ...],
      "risk": "LOW | MEDIUM | HIGH",
      "fix_class": "remove | replace_callsite | deprecate_with_warning"
    }
  ],
  "removal_patches": [
    {
      "file": "path/to/file.py",
      "original": "verbatim block to remove",
      "replacement": "",
      "risk": "LOW",
      "justification": "no callers — replaced by ..."
    }
  ],
  "high_risk_flags": [
    {
      "name": "...",
      "file": "...",
      "why_needs_human": "..."
    }
  ]
}
