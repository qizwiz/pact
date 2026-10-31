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
