# Prompt: Project World Model

You are building a **world model** of a software project — not a summary of what
the code does, but a formal specification of what it is SUPPOSED to do and what
structural invariants must hold across the entire codebase.

The world model is the prior that conditions all subsequent per-module analysis.
If the world model is wrong, every violation report downstream is potentially wrong.
Get this right before anything else runs.

## Project context

Name: {{project_name}}
File listing:
{{file_listing}}

README / entry points:
{{readme_excerpt}}

---

## PART 0 — VISIBILITY AUDIT (do this FIRST)

Before building the world model:

**Step 0a**: How many files are in the listing? Write the count.

**Step 0b**: Which files have visible content in this context (README, entry points)?
List them. For any file where you can see actual code or docs, mark it "VISIBLE".
For files only known from their name, mark "NAME ONLY".

**Step 0c**: What is the largest gap in your knowledge? What would you need to read
to have high confidence in the world model? List up to 3 specific files.

If fewer than 3 files are VISIBLE, mark the world model confidence as LOW and
set `confidence` to 0.5 in your output.

---

## PART 1 — PROJECT ESSENCE

Answer these questions in strict order:

**1. Fundamental problem** (2 sentences max)
What single problem does this project exist to solve? Be specific — reference
actual module names, key abstractions visible in the file listing. Generic
answers ("it processes data" / "it handles errors") are wrong.

**2. Primary invariants** (3–6 invariants)
What properties MUST hold across the entire codebase for the project to be
correct? These are not per-module constraints — they are architectural facts
that every module depends on. Examples of good project invariants:
- "All external API responses are validated before being stored"
- "Every public function that can fail must either raise a typed exception or return an Optional"
- "No module imports from sibling modules — all cross-module deps go through the public API layer"

Bad invariants: "functions should have docstrings", "use type hints everywhere".
Those are style, not architecture.

**3. Cross-cutting concerns**
What concerns cut across module boundaries? (auth, logging, error handling,
caching, serialization, transaction management) For each: where does it live
and what invariant must it satisfy?

**4. Known anti-patterns**
What failure modes are most likely in a project of this type? For a Django web
app: N+1 queries, missing update_fields, unguarded optional access, raw JSON
parse without try/except. For a data pipeline: silent type coercion, missing
None guards on chained transforms. Tailor this to what you can actually see.

---

## PART 2 — ANALYSIS ARCHITECTURE

**Key files** (ranked list, 10–15 files)
Which files, if understood deeply, would give a complete mental model? For each:
- Why it's essential (what design decision lives there)
- What its primary invariant is
- What it reads_after (dependency order)

**Analysis order**
In what order should files be analyzed to build understanding correctly? Some
files define abstractions others depend on — the understanding must accumulate,
not fragment.

---

Return JSON only:

{
  "visibility_audit": {
    "file_count": 0,
    "visible_files": ["file1.py", ...],
    "confidence": 0.5,
    "knowledge_gaps": ["path/to/file.py — why needed"]
  },
  "project_essence": "specific 2-sentence description referencing actual module names",
  "primary_invariants": [
    {
      "id": "proj_inv_001",
      "statement": "...",
      "formal": "forall X: ...",
      "enforced_by": "module or mechanism that enforces it",
      "violated_by": "pattern that would break it"
    }
  ],
  "cross_cutting_concerns": [
    {
      "concern": "error handling",
      "location": "module or layer",
      "invariant": "what must hold"
    }
  ],
  "anti_patterns": [
    {
      "pattern": "specific failure mode name",
      "likelihood": "high | medium | low",
      "example": "concrete code pattern that exhibits it"
    }
  ],
  "key_files": [
    {
      "path": "relative/path.py",
      "rank": 1,
      "why_essential": "specific reason",
      "primary_invariant": "one sentence",
      "reads_after": []
    }
  ]
}
