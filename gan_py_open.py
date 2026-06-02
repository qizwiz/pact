"""
gan_py_open — open-vocabulary discovery benchmark on PYTHON, REAL pact engine.

gan_py measured pact against bugs drawn from pact's OWN failure-mode list — bugs I
hand-picked because pact detects them. That is teaching to the test: the exam was
written from the answer key. Recall there (3/3) says only "pact detects instances of
its own modes," which is nearly tautological.

This removes that rig. The generator (deepseek, different family) is BLIND to pact's
vocabulary: it invents a realistic bug a code reviewer would flag, of whatever class
it likes, and realizes it as fresh code + a differential pytest. pact's coverage of
the OPEN space of bugs is what we measure.

Soundness is unchanged: pytest still proves the bug is real (fails buggy, passes fixed)
— that's execution, independent of any vocabulary. Only MATCHING changes: pact cannot
match by mode-name (the planted bug usually has no pact mode), so a hit is LOCATION —
pact flags a violation inside the planted buggy function. Honest, open-vocab.

Expected: recall well below gan_py's, because pact knows 18 Python-hygiene patterns and
a freely-invented bug usually is not one of them. That gap IS pact's real coverage.

    .venv/bin/python gan_py_open.py [N_CHALLENGES]
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gan  # noqa: E402
import gan_py  # noqa: E402  (reuse _pytest_passes, real_pact, _func_span, _sec, _funcname)

# Reviewer-level bug flavors, deliberately BROADER than pact's 18 AST patterns, to
# push the generator off pact's home turf. We never show it pact's mode list.
FLAVORS = [
    "an off-by-one / boundary error in indexing or a range",
    "a wrong comparison or boolean operator in a critical condition",
    "an incorrect state update or wrong order of operations",
    "an integer/float rounding or precision error an attacker could exploit",
    "a missing edge-case branch that returns a wrong result",
    "an incorrect default value or misread configuration",
    "a logic error in a discount/fee/quota calculation",
    "a unit or sign mismatch (seconds vs ms, +/- swapped)",
]


def _gen_prompt(flavor: str) -> str:
    return (
        "Design a small PYTHON audit challenge. INVENT a realistic bug that a careful code "
        "reviewer or security auditor would flag — the kind that actually ships in production. "
        f"For this challenge, aim for roughly: {flavor}.\n\n"
        "Write a realistic module (a plausible utility/service with real-looking names and logic, "
        "~30-70 lines) containing EXACTLY ONE such bug, embedded naturally so it is not obvious. "
        "Then write a pytest test that DEMONSTRATES the bug by EXECUTION: it must FAIL on the buggy "
        "module and PASS on the fixed module. It must `import target` (module under test is always "
        "target.py). Then write the FIXED module: same public interface, bug repaired.\n\n"
        "Return EXACTLY this format, nothing else:\n"
        "===TARGET===\n<buggy target.py>\n"
        "===FIXED===\n<fixed module, same interface>\n"
        "===TEST===\n<pytest test importing target>\n"
        "===BUGFUNC===\n<name of the single function in target.py with the bug>\n"
        "===BUGCLASS===\n<a short label for the bug you planted>\n"
    )


def _gen_repair(flavor: str, why: str) -> str:
    return (
        f"Your challenge was invalid: {why}\n\n"
        "The test must FAIL on the buggy module and PASS on the fixed module (differential by "
        f"execution). Keep target.py with one bug ({flavor}) in a single function. Re-emit ALL "
        "five sections in the same ===TARGET===/===FIXED===/===TEST===/===BUGFUNC===/===BUGCLASS=== "
        "format, nothing else."
    )


def generate(flavor: str, max_tries: int = 4) -> dict | None:
    raw = gan._ask(gan_py.GEN_MODEL, _gen_prompt(flavor), 3000)
    for attempt in range(1, max_tries + 1):
        target, fixed, test = (
            gan_py._sec(raw, "TARGET"),
            gan_py._sec(raw, "FIXED"),
            gan_py._sec(raw, "TEST"),
        )
        bugfunc = gan_py._funcname(gan_py._sec(raw, "BUGFUNC"))
        bugclass = (
            gan_py._sec(raw, "BUGCLASS").strip().splitlines()[0]
            if gan_py._sec(raw, "BUGCLASS")
            else "?"
        )
        why = ""
        if not (target and fixed and test):
            why = "could not parse all sections"
        elif gan_py._pytest_passes(target, test):
            why = "test PASSES on the buggy module (does not demonstrate a bug)"
        elif not gan_py._pytest_passes(fixed, test):
            why = "test does not PASS on the fixed module"
        if not why:
            return {
                "target": target,
                "fixed": fixed,
                "bugfunc": bugfunc,
                "bugclass": bugclass,
            }
        raw = gan._ask(gan_py.GEN_MODEL, _gen_repair(flavor, why), 3000)
    return None


def run_challenge(flavor: str, idx: int) -> dict | None:
    print(f"\n=== challenge #{idx} (flavor: {flavor[:48]}…) ===")
    ch = generate(flavor)
    if ch is None:
        print("  generate: no valid challenge; skipping")
        return None
    print(f"  planted (generator's label): {ch['bugclass']!r} in {ch['bugfunc']}()")
    print("  gate: pytest FAILS on buggy, PASSES on fixed ✓")
    vs = gan_py.real_pact(ch["target"])
    lo, hi = gan_py._func_span(ch["target"], ch["bugfunc"])
    modes = [getattr(v, "context", None) for v in vs]
    in_func = [v for v in vs if lo <= getattr(v, "line", -1) <= hi]
    hit = len(in_func) > 0
    mark = "🟢 LOCATED" if hit else "🔴 MISSED"
    print(f"  real pact violations on file: {modes or '[]'}")
    print(
        f"  {mark} (pact flagged {len(in_func)} violation(s) inside {ch['bugfunc']} lines {lo}-{hi})"
    )
    return {"bugclass": ch["bugclass"], "located": hit, "any_flag": len(vs) > 0}


def main(n: int = 6) -> None:
    print(
        f"GAN (python, OPEN vocab, REAL pact): generator={gan_py.GEN_MODEL}  challenges={n}"
    )
    rounds = []
    for i in range(1, n + 1):
        flavor = FLAVORS[(i - 1) % len(FLAVORS)]
        r = run_challenge(flavor, i)
        if r is not None:
            rounds.append(r)
    if not rounds:
        print("\nno valid challenges")
        return
    valid = len(rounds)
    located = sum(r["located"] for r in rounds)
    flagged = sum(r["any_flag"] for r in rounds)
    print("\n" + "=" * 60)
    print("planted bug classes (generator-invented, blind to pact):")
    for r in rounds:
        print(f"  [{'🟢' if r['located'] else '🔴'}] {r['bugclass']}")
    print(f"\nopen-vocab recall (pact flagged the buggy function): {located}/{valid}")
    print(f"pact flagged SOMETHING (anywhere) on:                {flagged}/{valid}")
    print(
        "(contrast: gan_py scored 3/3 by planting pact's OWN modes — this is the honest coverage)"
    )


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    main(n)
