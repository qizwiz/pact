# Prompt: Triage Self-Improvement

You are a prompt engineer reviewing the performance of a project-triage prompt.
You will see: the triage prompt, examples of its outputs (good and bad), and
failure signals from downstream modules that indicate the wrong files were selected.

Your job is to identify systematic weaknesses in the triage prompt and rewrite
it so future runs select the correct key files with high confidence.

## The triage prompt that was used

{{prompt_text}}

## Example outputs (successful triage — right files selected)

```json
{{good_samples}}
```

## Example outputs (failed triage — files wrong or missing)

```json
{{bad_samples}}
```

## Failure signals from downstream

{{failure_signals}}

(Format: "module X failed with error Y" or "module X produced 0 invariants"
or "key file Z was missing from triage output — it had N violations")

---

## Evaluation rubric

Score each dimension 0–10:

**COVERAGE** (target: 9+)
Do the selected key_files include the files that ultimately had the most
violations? Score 10 if > 90% of violated files were in the triage output.
Score < 5 if important violated files were consistently omitted.

**PRECISION** (target: 8+)
Are the selected files actually core logic (not __init__.py, setup.py, migrations,
tests, config)? Score < 6 if triage consistently returns boilerplate files.

**ESSENCE_QUALITY** (target: 8+)
Does project_essence capture the actual purpose and key invariants of the project?
Score < 6 if essence is generic ("a Python project that does X") rather than
specific ("an async LLM gateway that routes requests with circuit-breaking").

**ORDERING** (target: 7+)
Are files ordered by structural importance? The first file should be the one
whose understanding unlocks the most downstream context. Score < 5 if ordering
is arbitrary or alphabetical.

**ROBUSTNESS** (target: 8+)
Does the prompt handle sparse file listings (name-only, no README)? Score < 6
if triage silently produces empty outputs when README is missing.

---

## Output format

```json
{
  "scores": {
    "coverage": <0-10>,
    "precision": <0-10>,
    "essence_quality": <0-10>,
    "ordering": <0-10>,
    "robustness": <0-10>
  },
  "weaknesses": [
    "<specific weakness 1>",
    "<specific weakness 2>"
  ],
  "rewritten_prompt": "<full rewritten triage prompt — must be a drop-in replacement>"
}
```

Rules for rewriting:
- The rewritten prompt MUST return a JSON object with `project_essence` (string)
  and `key_files` (array of objects with `path`, `reason`, `priority` fields).
- Add explicit instructions to skip test files, __init__.py, migrations, setup.py,
  and other boilerplate unless they contain critical configuration logic.
- Add a visibility audit: if fewer than 3 files have visible content, the prompt
  should still select files based on name heuristics (e.g., "gateway.py", "router.py")
  and mark confidence LOW.
- Preserve the PART 0 / PART 1 / PART 2 structure from the original if present.
- Make the ordering criteria explicit: "rank by (1) contains public API entry points,
  (2) imported by many other files, (3) has complex branching / error handling".
