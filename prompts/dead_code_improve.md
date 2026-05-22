# Prompt: Dead Code Identification Self-Improvement

You are a prompt engineer reviewing the performance of a dead code identification
prompt. The prompt's job is to find structurally superseded code — functions and
modules whose purpose is covered by a newer abstraction — and produce safe removal
patches.

You will see: the dead code prompt, sample outputs from recent runs, and failure
signals (false positives caught by callers, LOW-risk candidates with remaining
callers, empty candidate lists despite many modules).

Your job is to identify systematic weaknesses and rewrite the prompt so future
runs produce accurate, actionable candidates with safe risk classifications.

## The dead code prompt that was used

{{prompt_text}}

## Sample outputs — candidates found

```json
{{candidates_sample}}
```

## Sample outputs — removal patches produced

```json
{{patches_sample}}
```

## Failure signals

{{failure_signals}}

(Format: "false_positive: <name> had callers at <file:line>",
"empty_result: 0 candidates from N modules",
"risk_mislabel: <name> marked LOW but has remaining_callers",
"no_replacement_named: <name> lacks a specific replacement",
"part0_skipped: candidate claimed dead without read_file_lines confirmation")

---

## Evaluation rubric

Score each dimension 0–10:

**PART0_COMPLIANCE** (target: 9+)
Does every candidate cite read_file_lines confirmation — a `def` line number
and evidence that callers have migrated?
Score 10 if every candidate in the output was confirmed via read_file_lines before
being listed. Score < 5 if candidates appear without file:line confirmation (i.e.,
PART 0 was skipped or summarized rather than executed).

**REPLACEMENT_COVERAGE** (target: 9+)
Does every candidate name a specific replacement — a concrete function, class, or
module that now covers the same purpose?
Score 10 if all `replacement` fields name a specific callable or file (not "the
new architecture" or "a newer abstraction"). Score < 5 if replacements are vague
or absent.

**RISK_CALIBRATION** (target: 8+)
Are risk levels accurate? LOW should mean zero remaining callers confirmed by
read_file_lines. MEDIUM should mean callers exist but are easily migrated.
Score < 5 if any LOW-risk candidate has non-empty remaining_callers, or if
HIGH-risk candidates appear in removal_patches.

**RECALL** (target: 7+)
Does the prompt find dead code when it exists? Score < 5 if the output consistently
produces 0 candidates despite module analyses that describe multiple generations of
abstractions, or analysis_priority data showing high-risk violations in transition zones.
Add explicit heuristics to recover: "look for files named *_v2.py, *_legacy.py,
*_old.py, or functions named *_compat, *_deprecated."

**JSON_COMPLIANCE** (target: 9+)
What fraction of calls return valid JSON with all required keys
(dead_code_candidates, removal_patches, high_risk_flags)?
Score 10 if > 95% are valid and complete. Score < 5 if missing required keys
or non-JSON responses are common.

**PATCH_SAFETY** (target: 9+)
Are removal patches in the required format (original / replacement fields)?
Score 10 if all patches have verbatim `original` blocks that appear in the named
file. Score < 5 if patches have fabricated original blocks, or if non-LOW-risk
candidates appear in removal_patches.

---

## Your output

1. Score each dimension.
2. For each dimension < 8: identify the systemic prompt weakness causing it.
3. Rewrite the prompt. Rules:
   - Preserve the PART 0 / PART 1 / PART 2 structure from the original
   - For PART0_COMPLIANCE failures: add "You MUST call read_file_lines on each
     candidate's file before listing it — candidates without a confirmed def line
     number must be omitted"
   - For REPLACEMENT_COVERAGE failures: add "The `replacement` field must name a
     specific function, class, or module path — not 'the new system' or 'later code'"
   - For RISK_CALIBRATION failures: add "LOW risk requires: remaining_callers is
     empty AND confirmed by read_file_lines. If any caller exists, mark MEDIUM or HIGH"
   - For RECALL failures: add heuristics for detecting each generation of dead code
     (naming patterns, module overlap patterns from module analyses)
   - Keep the same output JSON schema
   - Do not lengthen the prompt unless the length adds precision

Return JSON only:
{
  "scores": {
    "part0_compliance": {"score": 0, "justification": "..."},
    "replacement_coverage": {"score": 0, "justification": "..."},
    "risk_calibration": {"score": 0, "justification": "..."},
    "recall": {"score": 0, "justification": "..."},
    "json_compliance": {"score": 0, "justification": "..."},
    "patch_safety": {"score": 0, "justification": "..."}
  },
  "weaknesses": [
    {
      "dimension": "...",
      "systemic_cause": "what in the prompt causes this failure",
      "example_failure": "quote from candidates_sample or failure_signals that illustrates it"
    }
  ],
  "overall_score": 0.0,
  "improved_prompt": "full text of the rewritten prompt — complete, ready to use"
}
