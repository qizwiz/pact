# Prompt: Project Intent Self-Improvement

You are a prompt engineer reviewing the performance of a project-level intent
synthesis prompt. This prompt reads per-module analyses and synthesizes
cross-module invariants, violation clusters, and analysis priority.

You will see: the project_intent prompt, sample outputs from recent runs, and
failure signals (zero invariants surviving the oracle pass, generic invariants
that apply to every codebase, clusters with 1 violation, or analysis_priority
that just repeats the first violation without reasoning).

Your job is to identify systematic weaknesses and rewrite the prompt so future
runs produce specific, falsifiable, cross-module invariants that survive
adversarial oracle scrutiny.

## The project_intent prompt that was used

{{prompt_text}}

## Sample outputs — project invariants produced

```json
{{invariants_sample}}
```

## Sample outputs — violation clusters produced

```json
{{clusters_sample}}
```

## Oracle pass results (from adversarial oracle)

{{oracle_results}}

(Format: "N of M proposed invariants survived oracle",
"falsified: [list of invariant_ids with counterexample quotes]",
"unverifiable: [list of invariant_ids — evidence base too narrow]")

## Failure signals

{{failure_signals}}

(Format: "zero_survivors: 0 of N invariants survived",
"generic_invariant: '<statement>' applies to every Python project",
"single_violation_cluster: cluster '<name>' has only 1 violation — not cross-module",
"no_formal_statement: invariant '<id>' lacks a ∀/∃/if-then formal statement",
"analysis_priority_shallow: highest_risk_violation repeats first violation without reasoning")

---

## Evaluation rubric

Score each dimension 0–10:

**CROSS_MODULE_SCOPE** (target: 9+)
Do all project_invariants genuinely cut across at least 2 modules?
Score 10 if every invariant in applies_to_modules has ≥2 entries.
Score < 5 if invariants are effectively per-module observations promoted to project
level without cross-module evidence.

**FALSIFIABILITY** (target: 8+)
Are invariants falsifiable with a specific code pattern?
Score 10 if every invariant has a concrete code structure that would violate it
(not "all errors are handled" but "every bare `except:` clause violates this").
Score < 5 if invariants are tautological or uncheckable without running the system.

**ORACLE_SURVIVAL_RATE** (target: 6+)
What fraction of proposed invariants survive the adversarial oracle pass?
Score 10 if > 80% survive. Score < 5 if < 25% survive (oracle carpet-bombs them
as UNVERIFIABLE — the prompt is producing invariants beyond the visible evidence).

**CLUSTER_QUALITY** (target: 7+)
Do violation clusters name a shared root cause — not just co-located violations?
Score 10 if every cluster names a structural root cause (not "there are several
json.loads calls in two modules") and names a single fix that resolves all.
Score < 5 if clusters are 1-violation buckets or named by symptom rather than cause.

**FORMAL_PRECISION** (target: 8+)
Does every invariant have a formal statement in ∀ / ∃ / if-then notation?
Score 10 if formal fields are logically precise and directly encodable in Z3.
Score < 5 if formal fields are English paraphrases of the statement field.

**JSON_COMPLIANCE** (target: 9+)
Valid JSON with all required keys (coherence_check, project_invariants,
violation_clusters, analysis_priority)?
Score 10 if > 95% of calls return valid and complete JSON. Score < 5 if
keys are missing or non-JSON responses are common.

---

## Your output

1. Score each dimension.
2. For each dimension < 8: identify the systemic prompt weakness causing it.
3. Rewrite the prompt. Rules:
   - Preserve the PART 0 / PART 1 / PART 2 / PART 3 structure
   - For CROSS_MODULE_SCOPE failures: add "Before listing an invariant, name the
     two modules it requires. If you can only name one, it is a module-level
     observation — do NOT list it as a project invariant."
   - For FALSIFIABILITY failures: add concrete examples of falsifiable vs.
     unfalsifiable invariants in the rubric ("GOOD: ∀ endpoint e in tracer.py,
     if e calls model_hub.route(), there is an auth check before the call.
     BAD: all components should handle errors")
   - For ORACLE_SURVIVAL_RATE failures: add "Prefer invariants you can confirm from
     visible code to invariants you cannot confirm. Mark confidence 0.6–0.7
     if you are inferring from a single data point."
   - For CLUSTER_QUALITY failures: add "A cluster must name ONE fix class that
     resolves ALL violations in it. If violations need different fixes, split them."
   - Keep the same output JSON schema
   - Do not lengthen the prompt unless the length adds precision

Return JSON only:
{
  "scores": {
    "cross_module_scope": {"score": 0, "justification": "..."},
    "falsifiability": {"score": 0, "justification": "..."},
    "oracle_survival_rate": {"score": 0, "justification": "..."},
    "cluster_quality": {"score": 0, "justification": "..."},
    "formal_precision": {"score": 0, "justification": "..."},
    "json_compliance": {"score": 0, "justification": "..."}
  },
  "weaknesses": [
    {
      "dimension": "...",
      "systemic_cause": "what in the prompt causes this failure",
      "example_failure": "quote from invariants_sample or oracle_results that illustrates it"
    }
  ],
  "overall_score": 0.0,
  "improved_prompt": "full text of the rewritten prompt — complete, ready to use"
}
