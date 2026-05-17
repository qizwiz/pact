# ADR-017: `pact fix` — automated patch generation for fixable violation modes

## Status

Accepted

## Context

pact detects violations but leaves remediation entirely to the developer. For two modes — `llm_response_unguarded` and `missing_await` — the correct fix is mechanically derivable from the violation evidence alone:

- `llm_response_unguarded`: insert `if not var.attr: return` immediately before the unguarded subscript line
- `missing_await`: prepend `await ` before the unawaited call (handling both bare-statement and assignment forms)

External PRs filed using pact findings (phoenix#13237, merged; opik#6700, django-celery-beat#1038, under review) confirm that the fix patterns are consistent across codebases. The `FailureEvidence.call` field already encodes the information needed: `"response.choices[0]"` → `var=response`, `attr=choices`.

`save_without_update_fields` is explicitly excluded: generating the correct `update_fields` list requires tracking which model fields were mutated before the `.save()` call — a dataflow analysis problem beyond line-level patching.

## Decision

Add `pact/fixer.py` with two layers:

1. **`fix_file(path, violations) → FileResult`**: applies all fixable violations to a single file, returns patched source + lists of applied/skipped violations. Operates on `splitlines()` with reverse-order processing to keep line numbers stable during insertions.

2. **`apply_fixes(violations, *, dry_run, mode_filter) → list[FileResult]`**: groups violations by file, calls `fix_file`, optionally writes in-place.

Add `pact fix [DIR] [--apply] [--mode MODE]` subcommand to `cli.py`:
- Default: dry-run, prints unified diffs to stdout
- `--apply`: writes patches in-place
- `--mode MODE`: restrict to one fixable mode

The fixer duck-types both `FailureEvidence` (`.mode_name`) and `Violation` (`.context`) so it composes with both the raw checkers and `check_codebase()`.

## Consequences

- `pact fix`: changes pact from "report bugs" to "generate the PR"
- 12 tests in `test_fixer.py` cover: guard insertion, indentation preservation, multiple violations, malformed calls skipped, bare/assignment await, complex expressions skipped, unfixable modes passed through
- `save_without_update_fields` fix deferred — requires field mutation tracking (future ADR)
- Fixer output is `--dry-run` by default; `--apply` requires explicit opt-in (safe default)
