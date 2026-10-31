# TEST HEALTH

Audit of the full pytest suite, with two recorded runs (before / after) as the oracle.

- Branch: `claude/test-health-audit-t80has` (the session harness pins this name for the
  requested `claude/test-health`; nothing is pushed to any other branch, no PR, `main` untouched).
- Python 3.13.18 (CI matrix is 3.10–3.12), Linux, `go version go1.24.7`.
- Full suite = `python -m pytest -q -p no:cacheprovider` from the repo root, no `--ignore`,
  no `-k`, no deselection (CI ignores `go/` and `test_go_checker.py`; this audit does not).

## BEFORE (no code changed, commit `2020532`)

### Run A — install exactly as `pyproject.toml` / `pytest.ini` say

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q -p no:cacheprovider
```

| PASS | FAIL | ERROR | SKIP | collected |
|-----:|-----:|------:|-----:|----------:|
| 0 | 0 | 1 (conftest load) | 0 | 0 |

Exit code 4. `conftest.py` cannot load because the package itself cannot import:

```
__init__.py:3: from . import constraint_graph
constraint_graph.py:19: import networkx as nx
E   ModuleNotFoundError: No module named 'networkx'
```

`networkx` is imported unconditionally on `import pact`, but is not declared in
`[project] dependencies` nor in the `dev` extra. Only CI's hand-written `pip install` line has it.

### Run B — pyproject install + CI's extra packages (`networkx requests black ruff`)

```
pip install -e ".[dev]" networkx requests black ruff
python -m pytest -q -p no:cacheprovider --tb=short -rfE
```

| PASS | FAIL | ERROR | SKIP | collected |
|-----:|-----:|------:|-----:|----------:|
| **848** | 4 | 0 | 40 | 892 |

`4 failed, 848 passed, 40 skipped, 11 warnings in 214.91s`

Failing:

- `test_go_checker.py::TestGoCheckerIntegration::test_ignored_error_detected`
- `test_go_checker.py::TestGoCheckerIntegration::test_bare_recover_detected`
- `test_go_checker.py::TestGoCheckerIntegration::test_unchecked_assertion_detected`
- `test_go_checker.py::TestGoCheckerIntegration::test_goroutine_no_sync_detected`

All four: `run_go_checker` returns `[]` for Go sources that contain the violation.

**The "done" comparison uses Run B (848 passed, 892 collected) as the baseline**, not Run A's 0,
so the improvement can't come just from the suite starting to collect.

## FIXES (one commit each)

| Commit | Turns green | Cause → fix |
|---|---|---|
| `ee44896` deps: declare networkx | collection of the whole suite under a pyproject-only install (Run A: 0 → 892 collected) | `import pact` → `constraint_graph` → `import networkx`, undeclared. Added `networkx>=3.0` to `[project] dependencies` (it is a runtime import, not dev-only). |
| `e62c63d` go_checker: `go run` from inside the module | the 4 `TestGoCheckerIntegration` tests above | With no pre-built `pact-go` binary, `run_go_checker` ran `go run <abs>/go/checker` from the caller's cwd. Go refuses that outside the module (`go.mod file not found`, exit 1, empty stdout), and the bridge read empty stdout as "no violations", so every Go file looked clean. Fallback now runs with `cwd=go/checker`; input paths are `.resolve()`d so relative paths still point at the caller's files. |

No test file was edited, deleted, skipped, or had an assertion changed (`git diff 2020532 -- 'test_*.py' conftest.py pytest.ini` is empty).

## AFTER (HEAD with both fixes)

Pyproject-only install, same as Run A: `.venv/bin/pip install -e ".[dev]"`, nothing else
(`requests`, `black`, `ruff` are *not* installed in this venv):

```
.venv/bin/python -m pytest -q -p no:cacheprovider --tb=short -rfE
```

| PASS | FAIL | ERROR | SKIP | collected |
|-----:|-----:|------:|-----:|----------:|
| **852** | 0 | 0 | 40 | 892 |

`852 passed, 40 skipped, 11 warnings in 216.92s`, exit 0.

**Before → after:** 848 → 852 passed (vs Run B), 0 → 852 (vs Run A); collected 892 → 892, so no
tests were removed.

### Still failing

None.

### Skipped (40, unchanged, all `skipif`/`importorskip` on optional tools not installed by `.[dev]`)

| count | file | reason |
|---:|---|---|
| 13 | `test_checker.py` | semgrep not installed |
| 13 | `test_reduce.py` | scipy not installed |
| 11 | `test_defuse.py` | beniget not installed (`analysis` extra) |
| 2 | `test_tda.py` | gudhi not installed |
| 1 | `test_specgen.py` | java / tla2tools.jar not available |

## Notes / follow-ups (not changed here)

- `uv.lock` does not list `networkx` and is now stale against `pyproject.toml`
  (`requirements.lock` already pins `networkx==3.6.1`). Re-lock with `uv lock` when network is fine.
- `go/checker/go.mod` asks for go1.25; with local go1.24.7, the first `go run` auto-downloaded the
  go1.25.0 toolchain. That's a toolchain fetch triggered by `go.mod`, not something these fixes added.
- CI's `ci.yml` passes `--ignore=test_go_checker.py`, which is why the broken `go run` fallback
  never showed up in CI.
- A recurring warning during the suite, `heal.py:1299: checker baseline computation failed:
  attempted relative import with no known parent package`, doesn't fail any test but suggests
  heal's baseline subprocess imports pact modules outside the package. Worth a look.
