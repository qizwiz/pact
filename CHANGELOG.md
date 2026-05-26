# Changelog

All notable changes to pact are documented here.

## [0.3.0] — unreleased

### New capabilities

- **CrossHair symbolic verifier (Layer 2.5)** — when Z3 can't encode a contract (strings, collections, objects), CrossHair runs symbolic execution directly on the patched function. Catches semantically-wrong patches that previously slipped through to the LLM rubric.

- **SZZ bug-inducing commit density** — `enrich` now uses PyDriller's `get_commits_last_modified_lines()` to identify which commits introduced lines later tagged as bug-fixes. Hotspot files now carry an evidence-backed `bug_density` score (not just a churn heuristic).

- **Hassan change entropy** — per-file entropy score `H(f) = -∑ p_c log₂(p_c)` over commit author distribution (Hassan ICSE 2009). Outperforms raw churn as a fault predictor. Added alongside Tornhill signals in `enrich`.

- **R.C. Martin instability/abstractness metrics** — `reduce` now computes `I = Ce/(Ca+Ce)`, `A`, and `D = |I+A-1|` per module from the existing import graph. Files in the "zone of pain" (D > 0.7, concrete, highly depended-on) or "zone of uselessness" (D > 0.7, abstract, nobody depends on) are now flagged.

- **Minor-contributor ratio** — `KnowledgeSilo` in `enrich` now uses Bird et al. (ESEC/FSE 2011) methodology: authors with <5% ownership fraction are counted as minor contributors, which is the strongest post-release defect predictor ever measured at Microsoft. Replaces the weaker headcount-based `n_authors ≤ 2` signal.

### Bug fixes

- **`complexity_max` was never decreasing** — `_mine_pydriller()` was tracking a historical maximum that only went up. Fixed to use latest-commit complexity + linear trend direction (`complexity_latest` + `complexity_trend`).

- **Coupling formula** — `coupling_pct = max(pct_a, pct_b)` overestimated coupling for asymmetric pairs. Corrected to `min(pct_a, pct_b)` per Tornhill's recommendation.

---

## [0.2.0] — 2026-05-25

### Major additions

- **Full verification pipeline: Z3 → Hypothesis → heal** — behavioral contracts from intent analysis now flow through Z3 formal verification, then Hypothesis adversarial stress-testing seeded by the Z3 counterexample, then heal's CEGIS patch loop. Each step hands the baton to the next.

- **TLA+ model checking** — four TLA+ specification templates (`resource_lifecycle`, `ordering`, `accumulation`, `liveness`) now run TLC for real via subprocess and return verified/violated/unknown status. Previously generated specs but never checked them.

- **CEGIS heal loop** — the patch loop now generates → formally verifies → accepts/rejects in a real loop. LLM proposes; Z3 decides. Oracle (pytest/tox/make) confirms before applying. Previously applied patches without verification.

- **Tornhill git archaeology** — `enrich` now extracts hotspot scores (churn × complexity), temporal coupling pairs, and knowledge silos from the full git history via PyDriller. These signals feed the intent graph as L1 intent edges.

- **NetworkX intent graph** — files, issues, PRs, ADRs, and commits are now nodes in a NetworkX graph. Coverage level (L0–L3) is a structural property of graph degree, not a binary flag. Cut vertices in the intent graph surface as high-risk change points.

- **ADR coverage** — `enrich` maps ADR files from spec branches, resolves symbol mentions to specific source files, and surfaces contracts at the ADR level (L3 — highest confidence).

- **GitHub layer** — issues, PRs, and commit references are scraped from GitHub via `gh` and wired into the intent graph as L2 edges.

- **Markdown as first-class artifact** — every pipeline stage now produces both JSON (machine-composable) and Markdown (LLM-readable narrative). `--format markdown` gives a story, not a pretty-printed struct.

- **Multi-provider LLM** — `.env` support with priority chain: `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`. Model overrideable via `PACT_LLM_MODEL`.

### Bug fixes and hardening

- File-qualified call resolution in `_interproc_z3` — explicit imports now resolve to specific source files, eliminating false-positive taint edges when unrelated same-named functions exist in other files.
- LLM call detection expanded — async variants (`acreate`, `agenerate`, `stream`, `__call__`), `await`-unwrapping for async assignments.
- `reduce.py` NetworkX cut vertices now trigger intent analysis via `--intent-trigger` flag, closing the graph → intent → verify pipeline loop.
- Oracle safety gap — `_autodetect_test_cmd` finds pytest/tox/make automatically; `oracle_warning` emitted when applying without oracle.
- `z3_engine.py` UNKNOWN fixedpoint now emits `RuntimeWarning` instead of silent `proved_safe`.
- `extractor.py` `SyntaxError`/`OSError` now emit `RuntimeWarning` instead of silent return.
- `sheaf_summary` guard_deficit now uses call-graph β₁ (`tda_beta1_max`) not site-graph β₁ (always 0).
- Duplicate checker-name false negatives emit `RuntimeWarning` listing excluded function names.

---

## [0.1.0] — initial release

- AST-based static checker for Python (16 violation modes), TypeScript, and Go
- Z3 constraint engine for contract verification with typed templates
- Interprocedural taint analysis across file boundaries
- Hypothesis integration for adversarial input generation against pact's own detectors
- `pact fix` with LLM-generated patches
- CI integration (`--incremental`, `--strict`)
- Violations found in 14,148 unique sites across 1,254 repositories; 12 PRs merged upstream
