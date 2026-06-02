"""
gan_py — fully un-rigged discovery benchmark on PYTHON, testing the REAL pact engine.

Everything earlier in this benchmark line tested a Claude prompt dressed as the
finder. This does NOT. Here:

  GENERATOR  (deepseek — a different model family) writes, LIVE each run, a fresh
             realistic Python module containing exactly ONE planted bug that is an
             instance of one of pact's own failure modes, plus a pytest test that
             DEMONSTRATES it (fails on the buggy module, passes on the fixed one),
             plus the fixed module. Nothing pre-baked by me.

  GATE       (pytest — execution, sound) runs the test against both modules. The
             challenge is valid only if the test FAILS on buggy and PASSES on fixed:
             the bug is real and the fix removes it. Differential, by execution.

  FINDER     (the REAL pact engine — pact.checker.check_codebase, imported as a
             library, the same code the MCP server and CI run) analyses the buggy
             module. Not a prompt. The actual product.

  MATCH      pact 'found' the planted bug iff it reports a violation whose mode ==
             the planted mode AND whose line falls inside the planted function.
             Objective (mode + location), no soft judge.

Only pytest and pact's fixed engine are trusted. pact's vocabulary is a FIXED set
of failure modes, so we plant instances of THOSE modes in fresh code — the fair
test of pact's actual claim ("I detect these modes") on code it has never seen.
Bugs outside pact's vocabulary are out of scope and reported as such.

    .venv/bin/python gan_py.py [TRIALS_PER_MODE]
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(HERE, ".env"))
import gan  # noqa: E402  (reuse _ask / _strip_fence / LLM client)

# --- import the REAL pact engine as a library (pact-standalone via symlink parent) --
_LINK_PARENT = os.path.join(tempfile.gettempdir(), "pact_pkg_link")
os.makedirs(_LINK_PARENT, exist_ok=True)
_LINK = os.path.join(_LINK_PARENT, "pact")
if not os.path.exists(_LINK):
    os.symlink(HERE, _LINK)
sys.path.insert(0, _LINK_PARENT)
from pact.checker import check_codebase  # noqa: E402  THE REAL FINDER

GEN_MODEL = "deepseek/deepseek-chat"  # different family from any Claude
# work dir OUTSIDE the pact package — else pytest walks up to pact's root __init__.py
# (relative imports) and dies during collection. Isolated /tmp dir avoids that.
WORK = os.path.join(tempfile.gettempdir(), "gan_py_work")
os.makedirs(WORK, exist_ok=True)
VENV_PY = os.path.join(HERE, ".venv/bin/python")

# pact modes confirmed to fire on hand-written instances, each with a clean
# executable differential (a planted instance misbehaves on some input/use).
MODES = [
    {
        "name": "mutable_default_arg",
        "desc": "a function with a mutable default argument (list/dict/set) that "
        "accumulates state across separate calls",
        "diff": "call the function twice in separate calls; the buggy version shares "
        "state across calls, the fixed version allocates a fresh container each call",
    },
    {
        "name": "json_loads_unguarded",
        "desc": "json.loads() applied to external/untrusted text with no try/except, "
        "so malformed input crashes with ValueError",
        "diff": "feed malformed JSON; the buggy version raises uncaught, the fixed "
        "version catches it and returns a default or raises a domain error",
    },
    {
        "name": "optional_dereference",
        "desc": "a value obtained from an optional source (dict.get / .get(...) / a "
        "function that may return None) is dereferenced (.attr or [idx]) with no None check",
        "diff": "pass input that makes the source None; the buggy version raises "
        "AttributeError/TypeError, the fixed version guards the None case",
    },
    {
        "name": "subprocess_exit_code_unchecked",
        "desc": "subprocess.run/call whose exit code is ignored, so on failure the code "
        "proceeds with empty/garbage output",
        "diff": "make the invoked command fail; the buggy version proceeds silently with "
        "bad output, the fixed version detects the failure (check=True / returncode) and raises",
    },
]


# --------------------------------------------------------------------------- #
# generator (live, non-Claude)
# --------------------------------------------------------------------------- #
def _gen_prompt(mode: dict) -> str:
    return (
        "Design a small PYTHON audit challenge. Write a realistic module (a plausible "
        "utility/service with real-looking names and logic, ~30-70 lines) that contains "
        "EXACTLY ONE planted bug, embedded naturally so it is not glaringly obvious.\n\n"
        f"The planted bug must be an instance of THIS class:\n  {mode['desc']}\n\n"
        "Also write a pytest test that DEMONSTRATES the bug by EXECUTION:\n"
        f"  {mode['diff']}\n"
        "The test must FAIL against the buggy module and PASS against the fixed module. "
        "It must `import target` (the module under test is always named target.py) and "
        "define one or more `def test_*` functions using plain asserts / pytest.raises.\n\n"
        "Also write the FIXED module: same public interface, the bug repaired, nothing "
        "else changed.\n\n"
        "Return EXACTLY this format, nothing else:\n"
        "===TARGET===\n<buggy target.py source>\n"
        "===FIXED===\n<fixed module source, same interface>\n"
        "===TEST===\n<pytest test source that imports target>\n"
        "===BUGFUNC===\n<name of the single function in target.py that contains the bug>\n"
    )


def _gen_repair(mode: dict, why: str) -> str:
    return (
        f"Your challenge was invalid: {why}\n\n"
        "Requirements: the test must FAIL on the buggy module and PASS on the fixed module "
        f"(differential by execution), the planted bug must be: {mode['desc']}. Keep the "
        "module named target.py with the bug in a single function. Re-emit ALL four sections "
        "in the same ===TARGET===/===FIXED===/===TEST===/===BUGFUNC=== format, nothing else."
    )


def _sec(text: str, tag: str) -> str:
    m = re.search(rf"==={tag}===\s*(.*?)(?=\n===[A-Z]+===|\Z)", text, re.S)
    return gan._strip_fence(m.group(1)) if m else ""


def _funcname(raw_bugfunc: str) -> str:
    """Models emit the bug function as a name, a full `def ...:` line, or noise.
    Extract the bare identifier."""
    m = re.search(r"def\s+([A-Za-z_]\w*)", raw_bugfunc)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Za-z_]\w*)\b", raw_bugfunc)
    return m.group(1) if m else ""


def _pytest_passes(target_src: str, test_src: str) -> bool:
    for fn in os.listdir(WORK):
        if fn.endswith(".py"):
            os.remove(os.path.join(WORK, fn))
    with open(os.path.join(WORK, "target.py"), "w") as f:
        f.write(target_src)
    with open(os.path.join(WORK, "test_bug.py"), "w") as f:
        f.write(test_src)
    env = dict(os.environ, PYTHONPATH=WORK, PYTHONDONTWRITEBYTECODE="1")
    res = subprocess.run(
        [VENV_PY, "-m", "pytest", "test_bug.py", "-q", "-p", "no:cacheprovider"],
        cwd=WORK,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    return res.returncode == 0


def generate(mode: dict, max_tries: int = 4) -> dict | None:
    raw = gan._ask(GEN_MODEL, _gen_prompt(mode), 3000)
    for attempt in range(1, max_tries + 1):
        target, fixed, test, bugfunc = (
            _sec(raw, "TARGET"),
            _sec(raw, "FIXED"),
            _sec(raw, "TEST"),
            _funcname(_sec(raw, "BUGFUNC")),
        )
        why = ""
        if not (target and fixed and test):
            why = "could not parse all sections"
        elif _pytest_passes(target, test):
            why = "test PASSES on the buggy module (does not demonstrate a bug)"
        elif not _pytest_passes(fixed, test):
            why = "test does not PASS on the fixed module"
        if not why:
            print(
                f"  generate: valid challenge on attempt {attempt} (bugfunc={bugfunc})"
            )
            return {"target": target, "fixed": fixed, "test": test, "bugfunc": bugfunc}
        print(f"  generate: attempt {attempt} invalid — {why}; repairing...")
        raw = gan._ask(GEN_MODEL, _gen_repair(mode, why), 3000)
    print("  generate: FAILED to produce a valid challenge")
    return None


# --------------------------------------------------------------------------- #
# finder (the REAL pact engine) + match
# --------------------------------------------------------------------------- #
def _func_span(src: str, name: str) -> tuple[int, int]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return (0, 10**9)
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return (node.lineno, getattr(node, "end_lineno", node.lineno + 50))
    return (0, 10**9)  # function not found → don't constrain by line


def real_pact(target_src: str) -> list:
    """Run the real pact engine on the buggy module in an isolated dir."""
    pdir = os.path.join(WORK, "_pact")
    shutil.rmtree(pdir, ignore_errors=True)  # nuke caches (.mypy_cache etc.) too
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "target.py"), "w") as f:
        f.write(target_src)
    from pathlib import Path

    return check_codebase(Path(pdir))


def run_mode(mode: dict, idx: int) -> dict | None:
    print(f"\n=== {mode['name']} #{idx} ===")
    ch = generate(mode)
    if ch is None:
        return None
    print("  gate: pytest FAILS on buggy, PASSES on fixed ✓ (bug real, fix removes it)")
    vs = real_pact(ch["target"])
    lo, hi = _func_span(ch["target"], ch["bugfunc"])
    modes_here = [getattr(v, "context", None) for v in vs]
    hit = any(
        getattr(v, "context", None) == mode["name"]
        and lo <= getattr(v, "line", -1) <= hi
        for v in vs
    )
    mark = "🟢 FOUND" if hit else "🔴 MISSED"
    print(f"  real pact violations on target.py: {modes_here or '[]'}")
    print(f"  {mark} (planted {mode['name']} in {ch['bugfunc']} lines {lo}-{hi})")
    return {"mode": mode["name"], "found": hit}


def main(trials: int = 1) -> None:
    print(
        f"GAN (python, REAL pact): generator={GEN_MODEL}  modes={len(MODES)}  trials={trials}"
    )
    rounds = []
    for mode in MODES:
        for i in range(1, trials + 1):
            r = run_mode(mode, i)
            if r is not None:
                rounds.append(r)
    if not rounds:
        print("\nno valid challenges")
        return
    print("\n" + "=" * 56)
    by_mode: dict[str, list] = {}
    for r in rounds:
        by_mode.setdefault(r["mode"], []).append(r["found"])
    total_found = sum(r["found"] for r in rounds)
    for m, hits in by_mode.items():
        print(f"  {m:32s} {sum(hits)}/{len(hits)}")
    print(f"\nreal-pact recall on live challenges: {total_found}/{len(rounds)}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    main(n)
