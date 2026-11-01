# TEST HEALTH

Full-suite pytest record for rescuing three commits from `claude/test-health-audit-t80has` onto
current `main`. That branch started from a stale base (merge-base with `main` is `3d2885d`,
2026-05-28) and can't merge. Every number below comes from runs made for this record. None of
them are copied from the old branch's TEST-HEALTH.md.

- Branch: `claude/rescue-test-health-wz1z3s`. You asked for `claude/rescue-test-health`, but the
  session harness pins this suffixed name. It was created at `origin/main` = `d5f31ac`
  (`corpus: batch 1 (2026-11-01)`). Nothing is pushed anywhere else, no PR is opened, nothing is
  merged.
- Environment: Python 3.13.18 (CI's matrix is 3.10–3.12), Linux, `go version go1.24.7 linux/amd64`.
- Install: a fresh venv with `pip install -e ".[dev]" requests black ruff`, which gives networkx 3.7,
  z3-solver 5.3.3.0, hypothesis 6.170.14, pytest 9.2.0, anthropic 2.3.0, PyDriller 2.12.
- Command, run from the repo root with no `--ignore`, no `-k` and no deselection:

  ```
  python -m pytest -q -p no:cacheprovider --tb=short -rfEs
  ```

  CI's `ci.yml` passes `--ignore=go --ignore=test_go_checker.py`. This record doesn't.
- `collected` comes from `python -m pytest --collect-only -q -p no:cacheprovider`. For each run,
  passed + failed + skipped adds up to it.

## Rescue decisions

| Commit on old branch | Result | Evidence |
|---|---|---|
| `ee44896` deps: declare networkx as a runtime dependency | **Skipped: already on main.** | `git cherry-pick -x ee448967` → `CONFLICT (content): Merge conflict in pyproject.toml`. `main` already declares `"networkx>=2.6"` in `[project] dependencies`, added by `fcc731a` "Declare networkx as a runtime dependency (#8)" on 2026-09-12. The only difference is the version floor (`>=2.6` vs `>=3.0`). I aborted rather than resolving it, because bumping the floor would be forcing the commit. |
| `e62c63d` go_checker: run `go run` fallback from inside the checker module | **Applied** (cherry-picked with `-x`, no conflicts). | `go_checker.py` on `main` is byte-identical to the commit's parent, so it applies as-is. |
| `edc7591` TEST-HEALTH.md record | **Skipped: doesn't apply.** | `git cherry-pick -x edc7591a` → modify/delete conflict on `TEST-HEALTH.md` (`DU`). The commit edits a file created by its parent `a0b4df3` ("record baseline suite run"), which isn't on `main` and wasn't in the rescue list. I aborted. Its figures were measured on the old `2020532` tree, so they don't describe `main` anyway. This file replaces it, with fresh measurements. |

## BEFORE (`origin/main` = `d5f31ac`, no change)

| collected | passed | failed | error | skipped | xfail |
|----------:|-------:|-------:|------:|--------:|------:|
| 936 | **884** | 4 | 0 | 48 | 0 |

`4 failed, 884 passed, 48 skipped, 8 warnings in 210.50s (0:03:30)`, exit 1.

Failing:

- `test_go_checker.py::TestGoCheckerIntegration::test_ignored_error_detected`
- `test_go_checker.py::TestGoCheckerIntegration::test_bare_recover_detected`
- `test_go_checker.py::TestGoCheckerIntegration::test_unchecked_assertion_detected`
- `test_go_checker.py::TestGoCheckerIntegration::test_goroutine_no_sync_detected`

All four fail the same way, for example `assert 'go_ignored_error' in []`. `run_go_checker` returns no
violations for Go sources that contain them.

Skipped (48, all `skipif` / `importorskip` on optional tools that `.[dev]` doesn't install):

| count | file | reason |
|---:|---|---|
| 13 | `test_checker.py` | semgrep not installed |
| 13 | `test_reduce.py` | scipy not installed |
| 11 | `test_defuse.py` | beniget not installed (`analysis` extra) |
| 7 | `test_vrefactor.py` | tree-sitter extra not installed |
| 2 | `test_tda.py` | gudhi not installed |
| 1 | `test_package_imports.py` | scipy not installed |
| 1 | `test_specgen.py` | java / tla2tools.jar not available |
