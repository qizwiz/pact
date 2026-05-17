# ADR-015: Skip `.github/` directory

## Status

Accepted

## Context

Corpus batch r22-1 (langchain stars:>200) surfaced 184 violations from `.github/` directories across the full corpus — 163 from `BerriAI/litellm` alone (in `run_llm_translation_tests.py`, a GitHub Actions CI runner script). The remaining 21 spread across ersilia-os/ersilia, getsentry/sentry, langchain, langgraph, AutoGPT, and microsoft/lisa.

`.github/` is a GitHub-reserved directory for repository metadata: workflow YAML files, Actions Python helpers, issue templates, FUNDING.yml, Dependabot config. Python files in `.github/` are CI automation scripts — not application code that pact's checkers are designed to evaluate. Violations in CI orchestration scripts (test runners, release automators) are always FP noise: bare_except in a test runner may intentionally swallow individual test failures to continue the run.

`_SKIP_DIRS` already excluded `.git` but not `.github`.

## Decision

Add `.github` to `_SKIP_DIRS` in `extractor.py`.

```python
_SKIP_DIRS = frozenset({
    "__pycache__",
    ".git",
    ".github",   # CI/repo metadata — not application code
    ...
})
```

## Consequences

- 184 corpus violations eliminated (0.3% of 56k)
- litellm's 163 `.github` violations: suppressed
- No impact on production application code — `.github/` is only used by GitHub infrastructure and is never importable as a Python package
