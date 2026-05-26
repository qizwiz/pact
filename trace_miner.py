"""
pact trace_miner -- Daikon-style execution trace miner.

Instruments a test suite via sys.settrace to record variable values at
function entry/exit, then checks which invariant templates hold universally.
Universally-holding templates become ``empirically_mined`` invariants.

Public API:
    mine_invariants(root, test_cmd=None, timeout=30) -> list[MinedInvariant]

These are *observed* invariants from real execution — categorically different
from the static/LLM-derived invariants in intent.py.
"""

from __future__ import annotations

import pickle
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

_SKIP_MODULES: frozenset[str] = frozenset(
    {
        "pact",
        "trace_miner",
        "_pytest",
        "pytest",
        "pluggy",
        "_pytest.python",
        "_pytest.runner",
    }
)


@dataclass
class MinedInvariant:
    function: str  # "module::function_name"
    variable: str  # "result", "x", etc.
    template: str  # human-readable template e.g. "x >= 0"
    confidence: float  # fraction of calls where template held (1.0 = universal)
    call_count: int  # how many calls observed
    type: str = "empirically_mined"

    def to_dict(self) -> dict:
        return {
            "function": self.function,
            "variable": self.variable,
            "template": self.template,
            "confidence": self.confidence,
            "call_count": self.call_count,
            "type": self.type,
        }

    def render(self) -> str:
        pct = f"{self.confidence * 100:.0f}%"
        return (
            f"[{self.type}] {self.function}: {self.template}"
            f"  (n={self.call_count}, conf={pct})"
        )


# ---------------------------------------------------------------------------
# Template checking — pure functions, easily testable in isolation
# ---------------------------------------------------------------------------


def check_nonneg(val: Any) -> bool:
    """x >= 0"""
    try:
        return bool(val >= 0)
    except Exception:
        return False


def check_not_none(val: Any) -> bool:
    """x is not None"""
    return val is not None


def check_valid_len(val: Any) -> bool:
    """len(x) >= 0"""
    try:
        return len(val) >= 0
    except Exception:
        return False


def check_identity(val: Any, enter_val: Any) -> bool:
    """x == x_enter (input unchanged)"""
    try:
        return bool(val == enter_val)
    except Exception:
        return False


def check_monotone_increase(result: Any, enter_val: Any) -> bool:
    """result >= x_enter"""
    try:
        return bool(result >= enter_val)
    except Exception:
        return False


def check_monotone_decrease(result: Any, enter_val: Any) -> bool:
    """result <= x_enter"""
    try:
        return bool(result <= enter_val)
    except Exception:
        return False


def check_membership(result: Any, enter_val: Any) -> bool:
    """result in x_enter"""
    try:
        return result in enter_val
    except Exception:
        return False


def check_pair_leq(x: Any, y: Any) -> bool:
    """x <= y"""
    try:
        return bool(x <= y)
    except Exception:
        return False


def check_pair_eq(x: Any, y: Any) -> bool:
    """x == y"""
    try:
        return bool(x == y)
    except Exception:
        return False


def check_pair_diff_nonneg(x: Any, y: Any) -> bool:
    """y - x >= 0"""
    try:
        return bool(y - x >= 0)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Observation collection logic
# ---------------------------------------------------------------------------

# Each entry: dict with keys "function", "enter_args" (dict name→value),
# "return_value" (or None), "raised" (bool)
Observation = dict


def _eval_templates_for_observations(
    obs_list: list[Observation],
) -> list[MinedInvariant]:
    """
    Given a list of observations for a single (function) context,
    check each template and return universally-holding ones.
    """
    if len(obs_list) < 3:
        return []

    func_name = obs_list[0]["function"]
    results: list[MinedInvariant] = []

    # Gather non-raised observations (raised calls excluded)
    clean = [o for o in obs_list if not o.get("raised", False)]
    if len(clean) < 3:
        return []

    # --- Single-variable templates on return value ---
    single_templates = [
        ("result >= 0", check_nonneg),
        ("result is not None", check_not_none),
        ("len(result) >= 0", check_valid_len),
    ]
    for tpl_str, checker in single_templates:
        holds = sum(1 for o in clean if checker(o.get("return_value")))
        confidence = holds / len(clean)
        if confidence == 1.0:
            results.append(
                MinedInvariant(
                    function=func_name,
                    variable="result",
                    template=tpl_str,
                    confidence=confidence,
                    call_count=len(clean),
                )
            )

    # --- Templates involving result and each entry argument ---
    for arg_name in _common_arg_names(clean):
        pairs_checked = [
            o
            for o in clean
            if arg_name in o.get("enter_args", {}) and "return_value" in o
        ]
        if len(pairs_checked) < 3:
            continue

        rel_templates = [
            (f"result == {arg_name}_enter", check_identity),
            (f"result >= {arg_name}_enter", check_monotone_increase),
            (f"result <= {arg_name}_enter", check_monotone_decrease),
            (f"result in {arg_name}_enter", check_membership),
        ]
        for tpl_str, checker in rel_templates:
            holds = sum(
                1
                for o in pairs_checked
                if checker(o["return_value"], o["enter_args"][arg_name])
            )
            confidence = holds / len(pairs_checked)
            if confidence == 1.0:
                results.append(
                    MinedInvariant(
                        function=func_name,
                        variable=f"result/{arg_name}",
                        template=tpl_str,
                        confidence=confidence,
                        call_count=len(pairs_checked),
                    )
                )

    # --- Pair templates between entry arguments ---
    arg_names = sorted(_common_arg_names(clean))
    for i, name_x in enumerate(arg_names):
        for name_y in arg_names[i + 1 :]:
            pairs_checked = [
                o
                for o in clean
                if name_x in o.get("enter_args", {})
                and name_y in o.get("enter_args", {})
            ]
            if len(pairs_checked) < 3:
                continue
            pair_templates = [
                (f"{name_x} <= {name_y}", check_pair_leq),
                (f"{name_x} == {name_y}", check_pair_eq),
                (f"{name_y} - {name_x} >= 0", check_pair_diff_nonneg),
            ]
            for tpl_str, checker in pair_templates:
                holds = sum(
                    1
                    for o in pairs_checked
                    if checker(
                        o["enter_args"][name_x],
                        o["enter_args"][name_y],
                    )
                )
                confidence = holds / len(pairs_checked)
                if confidence == 1.0:
                    results.append(
                        MinedInvariant(
                            function=func_name,
                            variable=f"{name_x}/{name_y}",
                            template=tpl_str,
                            confidence=confidence,
                            call_count=len(pairs_checked),
                        )
                    )

    return results


def _common_arg_names(obs_list: list[Observation]) -> list[str]:
    """Return arg names that appear in ALL observations."""
    if not obs_list:
        return []
    all_sets = [frozenset(o.get("enter_args", {}).keys()) for o in obs_list]
    common = all_sets[0]
    for s in all_sets[1:]:
        common = common & s
    return sorted(common)


# ---------------------------------------------------------------------------
# In-process tracer (runs inside the subprocess via the wrapper script)
# ---------------------------------------------------------------------------

_TRACER_SOURCE = textwrap.dedent("""
    import sys
    import os
    import pickle
    import runpy

    _OBSERVATIONS = {}  # func_qualified_name -> list[dict]
    _CALL_STACK = []   # stack of (func_qualified_name, enter_args)

    _STDLIB = getattr(sys, 'stdlib_module_names', frozenset())
    _SKIP_PREFIXES = ('_pytest', 'pytest', 'pluggy', 'pact', '_')

    def _should_skip(frame):
        mod = frame.f_globals.get('__name__', '') or ''
        pkg = mod.split('.')[0]
        if pkg in _STDLIB:
            return True
        for prefix in _SKIP_PREFIXES:
            if mod == prefix or mod.startswith(prefix + '.'):
                return True
        filename = frame.f_code.co_filename or ''
        if 'site-packages' in filename:
            return True
        if filename.startswith('<'):
            return True
        return False

    def _safe_copy(val):
        \"\"\"Shallow copy of val if safe; return None otherwise.\"\"\"
        try:
            import copy
            return copy.copy(val)
        except Exception:
            try:
                return val
            except Exception:
                return None

    def _tracer(frame, event, arg):
        if event == 'call':
            if _should_skip(frame):
                return None
            mod = frame.f_globals.get('__name__', 'unknown')
            func = frame.f_code.co_name
            qualified = f'{mod}::{func}'
            try:
                enter_args = {
                    k: _safe_copy(v)
                    for k, v in frame.f_locals.items()
                    if not k.startswith('_')
                }
            except Exception:
                enter_args = {}
            _CALL_STACK.append((qualified, enter_args, False))
            return _tracer
        elif event == 'return':
            if not _CALL_STACK:
                return
            qualified, enter_args, _ = _CALL_STACK[-1]
            # Check frame matches
            mod = frame.f_globals.get('__name__', 'unknown')
            func = frame.f_code.co_name
            if qualified != f'{mod}::{func}':
                return
            _CALL_STACK.pop()
            obs = {
                'function': qualified,
                'enter_args': enter_args,
                'return_value': _safe_copy(arg),
                'raised': False,
            }
            _OBSERVATIONS.setdefault(qualified, []).append(obs)
        elif event == 'exception':
            if not _CALL_STACK:
                return
            qualified, enter_args, _ = _CALL_STACK[-1]
            mod = frame.f_globals.get('__name__', 'unknown')
            func = frame.f_code.co_name
            if qualified == f'{mod}::{func}':
                _CALL_STACK[-1] = (qualified, enter_args, True)

    sys.settrace(_tracer)

    # Run pytest
    import pytest
    exit_code = pytest.main(sys.argv[1:])

    sys.settrace(None)

    # Pickle observations to output path
    out_path = os.environ.get('TRACE_MINER_OUT', '/tmp/trace_miner_obs.pkl')
    with open(out_path, 'wb') as fh:
        pickle.dump(_OBSERVATIONS, fh, protocol=4)

    sys.exit(0)
    """)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def mine_invariants(
    root: Path,
    test_cmd: Optional[str] = None,
    timeout: int = 30,
) -> list[MinedInvariant]:
    """
    Run the project's test suite under a sys.settrace tracer, collect
    function entry/exit observations, then check invariant templates.

    Returns a list of MinedInvariant where confidence == 1.0 and call_count >= 3.

    Parameters
    ----------
    root:
        Project root directory.
    test_cmd:
        Optional explicit test command (list or shell string). If None,
        tries to detect pytest in .venv or falls back to 'pytest'.
    timeout:
        Subprocess timeout in seconds.
    """
    root = Path(root).resolve()

    # Resolve Python executable
    python_exe = _find_python(root)

    # Build pytest args — strip the executable from test_cmd if given
    pytest_args = _pytest_args_from_cmd(test_cmd, root)

    with tempfile.TemporaryDirectory() as tmpdir:
        runner_path = Path(tmpdir) / "_trace_runner.py"
        obs_path = Path(tmpdir) / "obs.pkl"
        runner_path.write_text(_TRACER_SOURCE, encoding="utf-8")

        env = {**_base_env(), "TRACE_MINER_OUT": str(obs_path)}

        subprocess.run(
            [python_exe, str(runner_path)] + pytest_args,
            cwd=str(root),
            env=env,
            timeout=timeout,
            capture_output=True,
        )

        if not obs_path.exists():
            # Tests may have failed but observations might still be written;
            # if not, return empty.
            return []

        with open(obs_path, "rb") as fh:
            raw_observations: dict[str, list[Observation]] = pickle.load(fh)

    # Filter to only observations from files under root
    filtered = _filter_to_project(raw_observations, root)

    # Evaluate templates
    invariants: list[MinedInvariant] = []
    for func_name, obs_list in filtered.items():
        invariants.extend(_eval_templates_for_observations(obs_list))

    return invariants


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_python(root: Path) -> str:
    """Return path to python executable — prefer .venv inside root."""
    for candidate in [
        root / ".venv" / "bin" / "python",
        root / "venv" / "bin" / "python",
        Path(sys.executable),
    ]:
        if Path(candidate).exists():
            return str(candidate)
    return sys.executable


def _pytest_args_from_cmd(test_cmd: Optional[str], root: Path) -> list[str]:
    """Convert optional test_cmd string to a list of pytest args."""
    if test_cmd is None:
        return ["-x", "-q", "--tb=no", "--no-header"]
    # Strip leading 'pytest' or 'python -m pytest' if present
    import shlex

    parts = shlex.split(test_cmd)
    # Remove executable prefix (python, pytest, etc.)
    while parts and parts[0] in ("pytest", "python", "python3", "-m", "py.test"):
        parts.pop(0)
    # If empty after stripping, use defaults
    if not parts:
        return ["-x", "-q", "--tb=no", "--no-header"]
    return parts


def _base_env() -> dict[str, str]:
    """Build a clean env dict — inherit PATH and PYTHONPATH."""
    import os

    keep = {
        "PATH",
        "PYTHONPATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TERM",
        "VIRTUAL_ENV",
    }
    return {k: v for k, v in os.environ.items() if k in keep}


def _filter_to_project(
    raw: dict[str, list[Observation]],
    root: Path,
) -> dict[str, list[Observation]]:
    """Keep only observations whose module path falls under root."""
    # Module names are "module::func"; we can't directly map module→path here
    # without importing. Instead, filter by module prefix heuristic:
    # keep anything whose module name doesn't look like stdlib or 3rd-party.
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    skip_prefixes = ("_pytest", "pytest", "pluggy", "_", "importlib")
    result: dict[str, list[Observation]] = {}
    for qualified, obs_list in raw.items():
        mod = qualified.split("::")[0]
        pkg = mod.split(".")[0]
        if pkg in stdlib:
            continue
        skip = False
        for prefix in skip_prefixes:
            if mod == prefix or mod.startswith(prefix + ".") or mod.startswith(prefix):
                skip = True
                break
        if skip:
            continue
        result[qualified] = obs_list
    return result
