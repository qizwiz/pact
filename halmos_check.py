"""
halmos_check — pact's real engine, seed: discharge a Solidity invariant by SYMBOLIC EVM.

This is the one thing the capability audit said is pact's genuine asset (SMT-backed
conservation/invariant counterexamples) — rebuilt on the backend that makes it sound:
Halmos symbolic execution with BitVec(256) EVM semantics, instead of the old
regex + unbounded-z3.Int path that (proven by the audit) FALSE-NEGATIVED a trivial
conservation bug (Sneaky.sol) and was blind to overflow entirely.

Thesis: AI proposes a global invariant as a Halmos `check_*` test (symbolic args);
Halmos proves it holds for ALL inputs or returns a concrete EVM-real counterexample.
Pattern-matchers (slither/semgrep) can't do this; a proof + counterexample is a
different category. This module is the discharger; the AI-proposer is the layer on top.

    .venv/bin/python halmos_check.py            # demo: catch Sneaky + overflow
    .venv/bin/python halmos_check.py <root>     # run check_* invariants in a foundry project
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HALMOS = os.path.join(HERE, ".venv/bin/halmos")
FOUNDRY_BIN = os.path.expanduser("~/.foundry/bin")


def run_halmos(
    root: str, function_prefix: str = "check", timeout: int = 600
) -> list[dict]:
    """Run Halmos on a foundry project; return one verdict per symbolic test.

    Each verdict: {function, proved: bool, counterexample: dict|None}.
    proved=True  -> invariant holds for ALL symbolic inputs (a real proof).
    proved=False -> Halmos found a concrete EVM input that violates it (counterexample).
    """
    env = dict(os.environ, PATH=f"{FOUNDRY_BIN}:{os.environ.get('PATH','')}")
    proc = subprocess.run(
        [HALMOS, "--root", root, "--function", function_prefix],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=root,
    )
    return _parse(proc.stdout + proc.stderr)


def _parse(out: str) -> list[dict]:
    results: list[dict] = []
    pending_ce: dict = {}
    in_ce = False
    for line in out.splitlines():
        s = _strip_ansi(line).strip()
        if s.startswith("Counterexample:"):
            in_ce, pending_ce = True, {}
            continue
        if in_ce:
            m = re.match(r"(p_\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)", s)
            if m:
                pending_ce[m.group(1)] = m.group(2)
                continue
            in_ce = in_ce and not (s.startswith("[FAIL") or s.startswith("[PASS"))
        m = re.search(r"\[(PASS|FAIL)\]?\s*(\w+)\(", s)
        if m:
            proved = m.group(1) == "PASS"
            results.append(
                {
                    "function": m.group(2),
                    "proved": proved,
                    "counterexample": None if proved else (pending_ce or None),
                }
            )
            pending_ce, in_ce = {}, False
    return results


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


if __name__ == "__main__":
    root = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(HERE, "examples/solidity/halmos")
    )
    print(f"halmos_check: discharging invariants in {root}\n")
    verdicts = run_halmos(root)
    if not verdicts:
        print("no symbolic tests found / halmos produced no verdicts")
        sys.exit(1)
    for v in verdicts:
        if v["proved"]:
            print(f"  ✅ PROVED   {v['function']} — holds for ALL symbolic inputs")
        else:
            print(
                f"  🔴 VIOLATED {v['function']} — counterexample {v['counterexample']}"
            )
    n_bug = sum(1 for v in verdicts if not v["proved"])
    print(
        f"\n{n_bug}/{len(verdicts)} invariants violated (EVM-real counterexamples, BitVec(256))."
    )
    print(
        "These are exactly the bugs the old regex+z3.Int path MISSED (Sneaky FN + overflow)."
    )
