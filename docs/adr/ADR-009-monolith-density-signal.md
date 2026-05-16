# ADR-009: Monolith Density Signal — Reporting per-file violation concentration

**Status**: Accepted  
**Date**: 2026-05-16  
**Deciders**: qizwiz

---

## Context

The r17-0 corpus scan found NousResearch/hermes-agent with 199 bare_except violations — 118 of them in a single 14,167-line `cli.py` file (202 total bare_except matches). This is qualitatively different from a repo with 199 violations distributed across 199 files.

Two distinct signals are being conflated:
1. **Point violations** — specific call sites that need a specific fix (a guarded LLM response, an update_fields argument, a missing await)
2. **Structural density** — a file or module with an extreme violation concentration, indicating architectural debt rather than fixable bugs

Filing 202 PRs to fix 202 individual `except Exception: pass` calls in one file is not actionable. The right signal is: "this file has a bare_except density of 1.4/100 lines — consider refactoring error handling strategy."

## Decision

Introduce a **monolith density threshold**: when a single file contributes ≥50 violations of the same mode, pact should surface a file-level structural warning rather than (or in addition to) individual violation rows.

The threshold of 50 is chosen because:
- It's above the noise floor for large but well-structured codebases
- A file with 50+ same-mode violations in a single file almost certainly has a systemic error-handling approach that won't be fixed call by call
- It provides a meaningful signal without suppressing the individual violation data

The density metric is: `violations_per_100_lines = (count / file_lines) * 100`.

## Output format

When `--blast-radius` or `--json` is used and a file exceeds the threshold:

```
⚠  MONOLITH DENSITY: cli.py — 118 bare_except in 14,167 lines (0.8/100 lines)
   This file's error handling is a systemic pattern, not individual fixable bugs.
   Consider: structured error hierarchy, centralized exception handling, or decomposition.
```

## Consequences

- Individual violation rows are still emitted (for completeness and grep-ability)
- The density signal is additive — it doesn't suppress existing output
- PR filing heuristics should skip repos where >50% of violations come from monolith-density files
- The `--reduce` score for functions inside monolith-density files should be annotated with the density context
- Implementation: add to `cli.py` output path when violation count per file exceeds threshold

## What this is NOT

This ADR does not suppress bare_except detection. It adds a *layer* of interpretation. The violations are still real; the structural signal is just an additional dimension that helps prioritize (or de-prioritize) action.
