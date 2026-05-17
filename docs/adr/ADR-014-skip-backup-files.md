# ADR-014: Skip files named `*.backup.py` and `*_backup.py`

## Status

Accepted

## Context

Corpus batch r22-0 (llm stars:>500) surfaced 225 violations from backup files — 224 in `firecrawl/apps/python-sdk/firecrawl/firecrawl.backup.py` and 1 in `agent/curator_backup.py`. These are explicitly non-production files: developers use the `.backup.py` / `_backup.py` naming convention to archive old code before a rewrite. Violations in backup files are always false positives in terms of actionability — no engineer will fix bare_excepts in a file that is never imported or executed.

`iter_python_files` skips directories in `_SKIP_DIRS` but had no filename-level exclusion for backup naming conventions.

## Decision

In `iter_python_files`, skip any `.py` file whose stem ends with `.backup` or `_backup`:

```python
stem = path.stem  # "firecrawl.backup" for "firecrawl.backup.py"
if stem.endswith(".backup") or stem.endswith("_backup"):
    continue
```

This targets the two established Python backup naming conventions without being broader (e.g., excluding any file with "backup" in its name would catch `backup_service.py`, which is a real production file).

## Consequences

- firecrawl's 224 `*.backup.py` violations: eliminated
- No impact on `backup_service.py`, `run_backup_task.py`, or other files that happen to contain "backup" but are not themselves backups
- Pattern is additive — future backup naming conventions (e.g., `.old.py`, `.orig.py`) are not covered and would need their own exclusions if they appear in corpus
