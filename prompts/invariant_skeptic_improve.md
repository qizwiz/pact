# Prompt: Invariant Skeptic Self-Improvement

You are a prompt engineer reviewing the performance of an adversarial invariant
oracle. The oracle's job is to falsify weak project-level invariants by finding
concrete counterexamples in module analyses.

You will see: the skeptic prompt, a sample of oracle verdicts across multiple runs,
and performance signals (falsification rate, fabrication incidents, UNVERIFIABLE rate).

Your job is to identify systematic failures and rewrite the prompt so the oracle
is calibrated — neither rubber-stamping everything nor carpet-bombing invariants.

## The skeptic prompt that was used

{{prompt_text}}

## Sample verdicts — SURVIVES

```json
{{survives_samples}}
```

## Sample verdicts — FALSIFIED

```json
{{falsified_samples}}
```

## Sample verdicts — UNVERIFIABLE

```json
{{unverifiable_samples}}
```

## Performance signals

{{performance_signals}}

(Format: "falsification_rate: X%, unverifiable_rate: Y%, fabrication_incidents: N,
avg_confidence_survives: Z, avg_confidence_falsified: W, oracle_note_quality_issues: [...]")

---

## Evaluation rubric

Score each dimension 0–10:

**COUNTEREXAMPLE_CONCRETENESS** (target: 9+)
Do FALSIFIED verdicts cite a specific `file:line` drawn verbatim from the module
analyses — not a paraphrased summary of it?
Score 10 if every counterexample is a direct quote with a file:line anchor.
Score < 5 if counterexamples are abstract ("some module violates this") without
a specific location. Fabricated counterexamples (not in any module analysis) score 0.

**CALIBRATION** (target: 7+)
Is the oracle calibrated — falsification_rate between 20% and 60% across many runs?
Score 10 if the oracle consistently identifies the weakest invariants without
rubber-stamping or carpet-bombing.
Score < 5 if falsification_rate is < 5% (too lenient, rubber-stamping) OR > 85%
(too aggressive, undermining the proposer phase entirely).

**UNVERIFIABLE_RATE** (target: 7+)
Does the oracle correctly reserve UNVERIFIABLE for invariants making claims
beyond the evidence base — and NOT overuse it as a safe hedge?
Score 10 if UNVERIFIABLE < 20% of all verdicts.
Score < 5 if UNVERIFIABLE > 50% (oracle hedging instead of engaging with evidence).

**CONFIDENCE_ACCURACY** (target: 7+)
Are confidence scores meaningful predictors of verdict reliability?
Score 10 if high-confidence SURVIVES verdicts are later found to be genuinely correct
(no subsequent violation discovered), and high-confidence FALSIFIED verdicts cite
concrete evidence.
Score < 5 if confidence scores are uniformly 0.5 regardless of evidence quality.

**JSON_COMPLIANCE** (target: 9+)
What fraction of calls return valid JSON with all required keys
(verdicts array, surviving_invariants, falsified_invariants, oracle_notes)?
Score 10 if > 95% are valid and complete. Score < 5 if missing required keys
or non-JSON responses are common.

**ORACLE_NOTE_QUALITY** (target: 7+)
Do oracle_notes identify systemic coverage gaps or invariant quality issues that
help the proposer improve in the next round?
Score < 5 if oracle_notes are generic ("some invariants are too broad") rather than
specific ("modules X and Y are not covered — invariants about error propagation
through these paths are UNVERIFIABLE, not SURVIVES").

---

## Your output

1. Score each dimension.
2. For each dimension < 8: identify the systemic prompt weakness causing it.
3. Rewrite the prompt. Rules:
   - Preserve the Step 1 / Step 2 / Step 3 (Steelman / Attack / Verdict) structure
   - For COUNTEREXAMPLE_CONCRETENESS failures: add explicit "Quote verbatim from
     module analyses — do not paraphrase" instruction with a GOOD/BAD example pair
   - For CALIBRATION failures (too lenient): add "If you cannot falsify, score your
     confidence honestly — most real codebases have at least 20% weak invariants"
   - For CALIBRATION failures (too aggressive): add "SURVIVES is a valid verdict —
     if the evidence does not show a counterexample, say SURVIVES, not FALSIFIED"
   - For UNVERIFIABLE_RATE failures: add "Reserve UNVERIFIABLE for invariants whose
     subject matter is not mentioned in ANY module analysis, not as a hedge when
     the evidence is ambiguous"
   - Keep the same output JSON schema
   - Do not lengthen the prompt unless the length adds precision

Return JSON only:
{
  "scores": {
    "counterexample_concreteness": {"score": 0, "justification": "..."},
    "calibration": {"score": 0, "justification": "..."},
    "unverifiable_rate": {"score": 0, "justification": "..."},
    "confidence_accuracy": {"score": 0, "justification": "..."},
    "json_compliance": {"score": 0, "justification": "..."},
    "oracle_note_quality": {"score": 0, "justification": "..."}
  },
  "weaknesses": [
    {
      "dimension": "...",
      "systemic_cause": "what in the prompt causes this failure",
      "example_failure": "quote from sample verdicts that illustrates it"
    }
  ],
  "overall_score": 0.0,
  "improved_prompt": "full text of the rewritten prompt — complete, ready to use"
}
