"""
Main orchestration: parse → graph → FailureMode registry → Z3 → violations.
"""

from pathlib import Path
from typing import Optional

from .encoder import Violation
from .extractor import (
    FunctionManifest,
    ModelManifest,
    extract_from_codebase,
    iter_python_files,
)
from .failure_mode import DEFAULT_MODES, FailureEvidence, FailureMode


def _to_violation(e: FailureEvidence) -> Violation:
    return Violation(
        file=e.file,
        line=e.line,
        call=e.call,
        missing=e.missing if e.missing else [e.message],
        context=e.mode_name,
        spec_id=e.spec_id,
    )


def _compute_dirty_set(
    changed_files: set[str],
    functions: list[FunctionManifest],
    call_sites,
) -> tuple[set[str], set[str]]:
    """
    BFS upward through the call graph from changed files.

    If F is defined in a dirty file, every call site that CALLS F is dirty too
    (a callee contract change can invalidate a caller's violation verdict).
    Returns (dirty_files, dirty_function_names).
    """
    dirty_files: set[str] = set(changed_files)
    dirty_funcs: set[str] = {f.name for f in functions if f.file in dirty_files}

    # callee_name → set of files that contain a call to it
    callee_to_caller_files: dict[str, set[str]] = {}
    for cs in call_sites:
        callee_to_caller_files.setdefault(cs.callee_name, set()).add(cs.file)

    frontier = set(dirty_funcs)
    while frontier:
        next_frontier: set[str] = set()
        for func_name in frontier:
            for caller_file in callee_to_caller_files.get(func_name, set()):
                if caller_file not in dirty_files:
                    dirty_files.add(caller_file)
                    new_funcs = {
                        f.name for f in functions if f.file == caller_file
                    } - dirty_funcs
                    dirty_funcs |= new_funcs
                    next_frontier |= new_funcs
        frontier = next_frontier

    return dirty_files, dirty_funcs


def check_codebase(
    root: Path,
    modes: Optional[list[FailureMode]] = None,
    *,
    _extracted=None,  # (models, functions, call_sites) if already extracted
) -> list[Violation]:
    """
    Run all FailureModes against every call site (and every file) in the codebase.

    Parameters
    ----------
    root:
        Directory to analyze.
    modes:
        FailureMode list to run. Defaults to DEFAULT_MODES.
        Pass a custom list to add or replace constraint classes without
        touching any other code.
    _extracted:
        Pre-extracted (models, functions, call_sites) tuple. If provided,
        skips the extraction step to avoid double-parsing.
    """
    _custom_modes = modes is not None
    if modes is None:
        modes = DEFAULT_MODES

    if _extracted is not None:
        models, functions, call_sites = _extracted
    else:
        models, functions, call_sites = extract_from_codebase(root)

    model_index: dict[str, ModelManifest] = {m.name: m for m in models}
    # Exclude names defined more than once — multiple same-named closures in
    # different scopes are indistinguishable without full scope analysis; using
    # the wrong definition produces false positives (e.g. required_arg_missing).
    _func_name_counts: dict[str, int] = {}
    for _f in functions:
        _func_name_counts[_f.name] = _func_name_counts.get(_f.name, 0) + 1
    func_index: dict[str, FunctionManifest] = {
        f.name: f for f in functions if _func_name_counts[f.name] == 1
    }

    seen: set[tuple] = set()
    violations: list[Violation] = []

    def _add(evidence: FailureEvidence) -> None:
        key = (evidence.file, evidence.line, evidence.mode_name, evidence.call)
        if key not in seen:
            seen.add(key)
            violations.append(_to_violation(evidence))

    # Per-call-site checks (modes with check=None are file-level only)
    for call in call_sites:
        for mode in modes:
            if mode.check is None:
                continue
            for evidence in mode.check(call, model_index, func_index):
                _add(evidence)

    # File-level checks — run on every Python file under root (via the same
    # iterator the extractor uses), so modes that scan definitions catch files
    # with zero call sites, models, or functions.
    file_modes = [m for m in modes if m.file_check is not None]
    if file_modes:
        for path in iter_python_files(root):
            for mode in file_modes:
                for evidence in mode.file_check(str(path)):  # type: ignore[misc]
                    _add(evidence)

    # TypeScript / TSX — tree-sitter backed; no-op if tree-sitter not installed
    try:
        from .ts_checker import check_ts_files

        for v in check_ts_files(root):
            key = (v.file, v.line, v.context, v.call)
            if key not in seen:
                seen.add(key)
                violations.append(v)
    except ImportError as _ts_import_err:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "ts_checker could not be loaded (%s); "
            "TypeScript files will not be checked. "
            "Install tree-sitter to enable TS support.",
            _ts_import_err,
        )

    # Z3 Fixedpoint confirmation for model_constraint violations.
    # PactEngine runs a Datalog proof over the same extracted facts; any
    # violation it confirms gets spec_id="z3:datalog" as a proof certificate.
    # Violations found only by Z3 (missed by AST) are added with that spec_id.
    # If Z3 is unavailable or fails, all AST results are returned unchanged.
    has_model_constraint = any(v.context == "model_constraint" for v in violations)
    if has_model_constraint:
        try:
            from .z3_engine import PactEngine

            engine = PactEngine()
            engine.load(root)
            z3_viols = engine.violations()
            # Index Z3 results by (file, line) for O(1) lookup
            z3_keys: set[tuple[str, int]] = {(zv.file, zv.line) for zv in z3_viols}
            # Annotate AST violations confirmed by Z3
            for v in violations:
                if v.context == "model_constraint" and (v.file, v.line) in z3_keys:
                    v.spec_id = "z3:datalog"
            # Add Z3-exclusive violations (AST missed them)
            ast_mc_keys: set[tuple[str, int]] = {
                (v.file, v.line) for v in violations if v.context == "model_constraint"
            }
            for zv in z3_viols:
                if (zv.file, zv.line) not in ast_mc_keys:
                    key = (zv.file, zv.line, "model_constraint", zv.call)
                    if key not in seen:
                        seen.add(key)
                        violations.append(
                            Violation(
                                file=zv.file,
                                line=zv.line,
                                call=zv.call,
                                missing=zv.missing,
                                context="model_constraint",
                                spec_id="z3:datalog",
                            )
                        )
        except ImportError as _z3_import_err:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "z3_engine could not be loaded (%s); "
                "model_constraint violations will not be Z3-confirmed and "
                "Z3-only violations will not be reported. "
                "Install z3-solver to enable Datalog proof support.",
                _z3_import_err,
            )
        except Exception as _z3_err:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "Z3 Datalog proof failed (%s: %s); "
                "model_constraint violations are unconfirmed and "
                "Z3-only violations may be missing. "
                "AST results are returned unchanged.",
                type(_z3_err).__name__,
                _z3_err,
            )

    # Semgrep — structural pattern detector.
    # Semgrep does not apply pact's semantic suppressions (guard functions,
    # session injection, noqa), so it may flag sites the AST checker suppresses.
    # We run it regardless of custom modes and de-duplicate via the `seen` set;
    # the (file, line, context, call) key ensures AST-suppressed sites are never
    # double-counted even when semgrep flags them independently.
    try:
        _semgrep_results = _run_semgrep(root)
        for v in _semgrep_results:
            key = (v.file, v.line, v.context, v.call)
            if key not in seen:
                seen.add(key)
                violations.append(v)
    except Exception:
        pass  # semgrep unavailable or crashed — AST results still complete

    # Mypy — type-system-confirmed optional_dereference violations.
    # Complements AST heuristic with union-attr + None-attr errors.
    # No-op if mypy is not installed.
    try:
        _mypy_results = _run_mypy(root)
        for v in _mypy_results:
            key = (v.file, v.line, v.context, v.call)
            if key not in seen:
                seen.add(key)
                violations.append(v)
    except Exception:
        pass  # mypy unavailable or crashed

    return violations


# ---------------------------------------------------------------------------
# Semgrep integration
# ---------------------------------------------------------------------------

# Map semgrep rule IDs → pact mode names
_SEMGREP_RULE_TO_MODE: dict[str, str] = {
    "llm-response-unguarded-choices": "llm_response_unguarded",
    "llm-response-unguarded": "llm_response_unguarded",
    "json-loads-unguarded": "json_loads_unguarded",
    "bare-except": "bare_except",
    "timeout-not-set-requests": "timeout_not_set",
    "timeout-not-set-httpx": "timeout_not_set",
    "queryset-first-none-dereference": "optional_dereference",
    "queryset-last-none-dereference": "optional_dereference",
    "dict-get-none-dereference": "optional_dereference",
    # "optional-dereference": no semgrep rule yet (needs .first()/.get() chain awareness)
    # "missing-await": no semgrep rule (semgrep cannot know which callables are async)
}


def _run_semgrep(root: Path) -> list[Violation]:
    """Run semgrep rules from pact/semgrep/ against root; return Violations.

    Returns an empty list if semgrep is not installed, the rules directory
    is absent, or the run fails for any reason.
    """
    import json as _json
    import subprocess as _sp
    import shutil

    if not shutil.which("semgrep"):
        return []

    rules_dir = Path(__file__).parent / "semgrep"
    if not rules_dir.exists():
        return []

    try:
        proc = _sp.run(
            ["semgrep", "--config", str(rules_dir), "--json", str(root)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode not in (0, 1):  # 1 = findings found (normal)
            return []
        data = _json.loads(proc.stdout)
    except Exception:
        return []

    # Cache file lines for call-text extraction (semgrep extra.lines is
    # unreliable when login is required — extract from source directly).
    _file_lines: dict[str, list[str]] = {}

    def _match_text(path: str, start: dict, end: dict) -> str:
        if path not in _file_lines:
            try:
                _file_lines[path] = Path(path).read_text(errors="replace").splitlines()
            except OSError:
                _file_lines[path] = []
        lines = _file_lines[path]
        sl, sc = start.get("line", 1) - 1, start.get("col", 1) - 1
        el, ec = end.get("line", 1) - 1, end.get("col", 1) - 1
        if sl < 0 or sl >= len(lines):
            return ""
        if sl == el:
            return lines[sl][sc:ec].strip()
        return lines[sl][sc:].strip()

    def _sibling_guarded(path: str, line_1based: int, var_name: str) -> bool:
        """Return True if a sibling if-guard for var_name.choices precedes this line.

        Semgrep's pattern-not-inside only suppresses matches that are INSIDE an
        if-block; early-exit guards on the preceding line are siblings and semgrep
        cannot suppress them. We scan backwards up to 15 lines at the same or
        shallower indent level.
        """
        lines = _file_lines.get(path, [])
        idx = line_1based - 1  # 0-based
        if idx <= 0 or idx >= len(lines):
            return False
        target_indent = len(lines[idx]) - len(lines[idx].lstrip())
        for i in range(idx - 1, max(idx - 16, -1), -1):
            raw = lines[i]
            stripped = raw.strip()
            if not stripped:
                continue
            indent = len(raw) - len(raw.lstrip())
            if indent > target_indent:
                continue  # inside a nested block — skip
            if stripped.startswith(("def ", "async def ", "class ")):
                break  # new scope — stop scanning
            if (
                f"if {var_name}.choices" in stripped
                or f"if not {var_name}.choices" in stripped
                or f"if len({var_name}.choices)" in stripped
            ):
                return True
        return False

    def _is_bare_except_reraise(path: str, end_line_1based: int) -> bool:
        """Return True if the bare-except block ends with only a bare `raise`.

        Scans backward from end_line to find the `except:` header, then checks
        if the sole body statement is `raise`.  Suppresses the false positive
        that semgrep's structural pattern cannot exclude.
        """
        lines = _file_lines.get(path, [])
        # Search backward from end of match for the except: line
        for i in range(min(end_line_1based - 1, len(lines) - 1), -1, -1):
            stripped = lines[i].strip()
            if stripped == "except:":
                except_indent = len(lines[i]) - len(lines[i].lstrip())
                # Check the next non-blank line
                for j in range(i + 1, min(i + 10, len(lines))):
                    body_stripped = lines[j].strip()
                    if not body_stripped:
                        continue
                    body_indent = len(lines[j]) - len(lines[j].lstrip())
                    if body_indent <= except_indent:
                        break  # end of except body
                    if body_stripped == "raise":
                        # Verify no further statements in the body
                        for k in range(j + 1, min(j + 5, len(lines))):
                            ks = lines[k].strip()
                            if not ks:
                                continue
                            ki = len(lines[k]) - len(lines[k].lstrip())
                            if ki > except_indent:
                                return False  # more body statements — not pure reraise
                            break
                        return True
                    return False  # first body stmt is not raise
                break
        return False

    results: list[Violation] = []
    for finding in data.get("results", []):
        rule_id = finding.get("check_id", "").split(".")[-1]
        mode = _SEMGREP_RULE_TO_MODE.get(rule_id)
        if not mode:
            continue
        path = finding.get("path", "")
        start = finding.get("start", {})
        end = finding.get("end", {})
        line = start.get("line", 0)
        call = _match_text(path, start, end)

        # Suppress known false positive: early-return sibling guard before choices[0]
        if "choices[0]" in call:
            meta = finding.get("extra", {}).get("metavariables", {})
            var_name = meta.get("$RESPONSE", {}).get("abstract_content", "")
            if not var_name and ".choices[0]" in call:
                # semgrep OSS doesn't populate metavariables — extract from call text
                var_name = call.split(".choices[0]")[0].strip().lstrip("(")
            if var_name and _sibling_guarded(path, line, var_name):
                continue

        # Suppress bare-except false positive: `except: raise` is a pure re-raise
        # (semgrep structural patterns cannot exclude it, so filter in Python).
        if mode == "bare_except" and _is_bare_except_reraise(
            path, end.get("line", line)
        ):
            continue

        # Suppress optional_dereference FP: X.first().attr if X else Y
        # Semgrep pattern-not cannot suppress inline ternary guards on the same line.
        if rule_id in {
            "queryset-first-none-dereference",
            "queryset-last-none-dereference",
        }:
            import re as _re2

            _src_lines = _file_lines.get(path, [])
            if _src_lines and 0 < line <= len(_src_lines):
                if _re2.search(
                    r"\.\b(?:first|last)\(\)\.\w+\s+if\s+\w", _src_lines[line - 1]
                ):
                    continue

        # Skip vendor/third-party directories
        if any(seg in path for seg in ("/_vendor/", "/vendor/", "/site-packages/")):
            continue

        # Respect # noqa — check from start line to end line of the match span
        _file_lines_cache = _file_lines.get(path, [])
        end_line = end.get("line", line)
        if _file_lines_cache and any(
            "# noqa" in _file_lines_cache[i]
            for i in range(line - 1, min(end_line, len(_file_lines_cache)))
            if 0 <= i < len(_file_lines_cache)
        ):
            continue

        results.append(
            Violation(
                file=path,
                line=line,
                call=call[:80],
                missing=[finding.get("extra", {}).get("message", "")[:120]],
                context=mode,
                spec_id="semgrep",
            )
        )
    return results


def _run_mypy(root: Path) -> list[Violation]:
    """Run mypy for optional_dereference violations.

    Targets union-attr errors (Optional[X].attr without None-check) and
    attr-defined errors where the object is definitively None.  Both are
    type-system-confirmed optional_dereference violations — higher confidence
    than the AST heuristic but slower to compute.

    Returns an empty list when mypy is not installed or the run fails.
    """
    import json
    import shutil
    import subprocess
    import sys

    # Look for mypy: prefer the same Python env as pact (venv alongside __file__),
    # then the same env as the caller (sys.executable), then PATH.
    _pact_venv = Path(__file__).parent / ".venv" / "bin" / "mypy"
    mypy_bin = None
    for candidate in [
        _pact_venv,
        Path(sys.executable).parent / "mypy",
    ]:
        if candidate.exists():
            mypy_bin = candidate
            break
    if mypy_bin is None:
        mypy_bin_str = shutil.which("mypy")
        if not mypy_bin_str:
            return []
        mypy_bin = Path(mypy_bin_str)

    # Discover Python source directories: top-level dirs with ≥10 .py files
    # and valid Python identifier names (no hyphens — mypy can't import them).
    # Targeting specific dirs avoids duplicate-module errors from unrelated
    # sub-repos or vendor trees that mypy sees when targeting ".".
    import re as _re

    _VALID_PY_NAME = _re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    _SKIP_DIRS = frozenset(
        {"node_modules", "__pycache__", ".venv", "venv", ".git", "migrations"}
    )
    _py_dirs: list[str] = []
    for _child in sorted(root.iterdir()):
        if not _child.is_dir():
            continue
        if _child.name.startswith(".") or _child.name in _SKIP_DIRS:
            continue
        if not _VALID_PY_NAME.match(_child.name):
            continue  # hyphens etc. — not importable as a Python package
        _py_count = sum(1 for _ in _child.glob("**/*.py"))
        if _py_count >= 10:
            _py_dirs.append(_child.name)
    _targets = _py_dirs if _py_dirs else ["."]

    _mypy_flags = [
        "--ignore-missing-imports",
        "--check-untyped-defs",
        "--follow-imports=skip",
        "--no-error-summary",
        "--explicit-package-bases",
        "-O",
        "json",
    ]
    # Use plain mypy (not dmypy daemon). The daemon is optimized for analyzing
    # the same codebase repeatedly (IDE/incremental use). pact analyzes many
    # different codebases per session, so the daemon's cross-cwd cache is a
    # liability: it leaks one process per unique working directory and returns
    # stale results when the same daemon sees unrelated tmp directories.
    proc = None
    try:
        proc = subprocess.run(
            [str(mypy_bin), *_targets, *_mypy_flags],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=root,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    _mypy_file_lines: dict[str, list[str]] = {}
    results: list[Violation] = []

    for raw in proc.stdout.splitlines() + proc.stderr.splitlines():
        raw = raw.strip()
        if not raw or not raw.startswith("{"):
            continue
        try:
            finding = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if finding.get("severity") != "error":
            continue
        code = finding.get("code", "")
        msg = finding.get("message", "")
        if code == "union-attr":
            pass  # Optional[X].attr — always an optional_dereference
        elif code == "attr-defined" and '"None" has no attribute' in msg:
            pass  # definitively-None attribute access
        else:
            continue

        path = finding.get("file", "")
        line = finding.get("line", 0)
        if not path or line <= 0:
            continue

        full_path = (
            str((root / path).resolve()) if not Path(path).is_absolute() else path
        )

        if any(
            seg in full_path for seg in ("/_vendor/", "/vendor/", "/site-packages/")
        ):
            continue

        if full_path not in _mypy_file_lines:
            try:
                _mypy_file_lines[full_path] = (
                    Path(full_path).read_text(errors="replace").splitlines()
                )
            except OSError:
                _mypy_file_lines[full_path] = []

        file_lines = _mypy_file_lines[full_path]
        end_line = finding.get("end_line", line)
        if file_lines and any(
            "# noqa" in file_lines[i]
            for i in range(line - 1, min(end_line, len(file_lines)))
            if 0 <= i < len(file_lines)
        ):
            continue

        results.append(
            Violation(
                file=full_path,
                line=line,
                call=msg[:80],
                missing=[f"mypy [{code}]: {msg}"[:120]],
                context="optional_dereference",
                spec_id="mypy",
            )
        )
    return results


def check_codebase_incremental(
    root: Path,
    changed_files: set[str],
    modes: Optional[list[FailureMode]] = None,
    *,
    _extracted=None,
) -> tuple[list[Violation], dict]:
    """
    Like check_codebase, but skips call sites whose enclosing file AND whose
    callees' files are all unchanged.

    Propagation: if any function F is defined in a dirty file, every call site
    that calls F is also dirty — a callee change can alter whether a caller
    is flagged.  The BFS terminates when no new files are reached.

    Returns
    -------
    violations : list[Violation]
        Only violations reachable from the dirty subgraph.
    stats : dict
        Diagnosis keys: total_files, dirty_files, total_call_sites,
        dirty_call_sites, skip_ratio.
    """
    if modes is None:
        modes = DEFAULT_MODES

    if _extracted is not None:
        models, functions, call_sites = _extracted
    else:
        models, functions, call_sites = extract_from_codebase(root)

    dirty_files, _ = _compute_dirty_set(changed_files, functions, call_sites)
    dirty_call_sites = [cs for cs in call_sites if cs.file in dirty_files]

    model_index: dict[str, ModelManifest] = {m.name: m for m in models}
    # Exclude names defined more than once (see check_codebase for rationale).
    _func_name_counts: dict[str, int] = {}
    for _f in functions:
        _func_name_counts[_f.name] = _func_name_counts.get(_f.name, 0) + 1
    func_index: dict[str, FunctionManifest] = {
        f.name: f for f in functions if _func_name_counts[f.name] == 1
    }

    seen: set[tuple] = set()
    violations: list[Violation] = []

    def _add(evidence: FailureEvidence) -> None:
        key = (evidence.file, evidence.line, evidence.mode_name, evidence.call)
        if key not in seen:
            seen.add(key)
            violations.append(_to_violation(evidence))

    for call in dirty_call_sites:
        for mode in modes:
            if mode.check is None:
                continue
            for evidence in mode.check(call, model_index, func_index):
                _add(evidence)

    file_modes = [m for m in modes if m.file_check is not None]
    if file_modes:
        for path in iter_python_files(root):
            if str(path) not in dirty_files:
                continue
            for mode in file_modes:
                for evidence in mode.file_check(str(path)):  # type: ignore[misc]
                    _add(evidence)

    total_cs = len(call_sites)
    all_py_files = {str(p) for p in iter_python_files(root)}
    stats = {
        "total_files": len(all_py_files),
        "dirty_files": len(dirty_files & all_py_files),
        "total_call_sites": total_cs,
        "dirty_call_sites": len(dirty_call_sites),
        "skip_ratio": round(1.0 - len(dirty_call_sites) / max(total_cs, 1), 3),
    }
    return violations, stats
