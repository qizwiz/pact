"""
body_catch_loop — can the body prompt teach itself to CATCH (recall), not just prove?

The precision loop trained on clean fixtures and minimized the body to `balanceOf(c) >= a` — which
proves clean code but MISSES bugs. This trains on DISCRIMINATION: one invariant that PROVES on the
clean contract AND is CAUGHT on a buggy mutant. That single score forces rich-enough-to-catch yet
sound-enough-to-prove; the intent gate guards the FP class. Honest transcript (names the failure,
does NOT hand over the fix — the lesson-stripped law) so a climbing trajectory = real discovery.

  fixture pair: DamnValuableStaking (clean) vs DamnValuableStakingBug (mint-2x supply inflation)
  score = 1 iff (PROVED on clean) AND (CAUGHT on buggy)

    .venv/bin/python body_catch_loop.py
"""

from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import invariant_agent as agent
from plumbline import prompt_improve as pi
import meta_template as mt
import hybrid_audit as H
import audit

FORGE = os.path.expanduser("~/.foundry/bin/forge")
HALMOS = os.path.join(HERE, ".venv/bin/halmos")
DVD = "/Users/jonathanhill/src/damn-vulnerable-defi"
THRESHOLD = getattr(pi, "THRESHOLD", 0.7)


def make_bug():
    src = open(DVD + "/src/DamnValuableStaking.sol").read()
    bug = src.replace("contract DamnValuableStaking", "contract DamnValuableStakingBug")
    # mint TWICE the staked principal -> stDVT supply inflates above DVT held (conservation break)
    bug = bug.replace("_mint(msg.sender, amount)", "_mint(msg.sender, amount * 2)")
    open(DVD + "/src/DamnValuableStakingBug.sol", "w").write(bug)


def run_one(name, rel, params, body):
    pre, _assets = H.build_scaffold(name, "../" + rel, params)
    harness = pre.replace("__BODY__", "\n".join("        " + l for l in body.splitlines()))
    test = DVD + "/test/_Catch.t.sol"
    try:
        open(test, "w").write(harness)
        # --ast is REQUIRED: halmos skips contracts whose forge artifact lacks the AST. Without it the
        # pre-build poisons the cache with ast-less artifacts and halmos silently skips our harness
        # (KeyError 'ast') -> falls back to a stale artifact -> FAKE pass. Sound oracle needs --ast.
        b = subprocess.run([FORGE, "build", "--ast", "--root", DVD], capture_output=True, text=True, timeout=400)
        if b.returncode != 0:
            return "BUILD_FAIL"
        h = subprocess.run([HALMOS, "--root", DVD, "--contract", "Invariants", "--function", "check"],
                           capture_output=True, text=True, timeout=200)
        out = h.stdout + h.stderr
        if "[PASS]" in out:
            return "PROVED"
        if "[FAIL]" in out:
            return "CAUGHT"
        return "no_verdict"
    finally:
        if os.path.exists(test):
            os.remove(test)


def eval_pair():
    src = open(DVD + "/src/DamnValuableStaking.sol").read()
    params = mt.extract_ctor("DamnValuableStaking", src)
    _pre, assets = H.build_scaffold("DamnValuableStaking", "../src/DamnValuableStaking.sol", params)
    stmt, body = H.fill_body("DamnValuableStaking", src, assets)
    if not body:
        return 0, "no body produced."
    ok, reason = audit.intended("DamnValuableStaking", src, stmt)
    if not ok:
        return 0, f"gate rejected (not-intended): {reason}"
    clean = run_one("DamnValuableStaking", "src/DamnValuableStaking.sol", params, body)
    buggy = run_one("DamnValuableStakingBug", "src/DamnValuableStakingBug.sol", params, body)
    if clean == "PROVED" and buggy == "CAUGHT":
        return 1, f"DISCRIMINATES: PROVED on clean, CAUGHT on buggy — '{stmt[:55]}'"
    # honest transcript: name the failure + drive DIMENSION exploration, do NOT hand over the fix
    return 0, (f"clean={clean}, buggy={buggy}; invariant='{stmt[:55]}'. It did NOT distinguish the "
               "correct contract from one with a real accounting bug. You keep checking the SAME "
               "relationship — VARY IT: try a DIFFERENT PAIR of quantities (relate the tokens the "
               "contract HOLDS to the shares/accounting it ISSUED, rather than to the deposited "
               "amount). A real accounting bug breaks the relationship you are not yet checking.")


def main():
    print("body_catch_loop: can it teach itself to CATCH? (discrimination: prove clean AND catch buggy)\n")
    make_bug()
    try:
        history = []
        for r in range(1, 5):
            print(f"────────── round {r} ──────────")
            s, t = eval_pair()
            print(f"  {t}")
            history.append(s)
            if s >= 1:
                print("  it CATCHES the bug AND proves clean. discrimination learned.")
                break
            pi.improve_if_weak("sol_body_fill", float(s), t, agent._ask)
            print()
        print("\n================ trajectory ================")
        print("  " + "  ->  ".join(str(x) for x in history))
        if history and history[-1] >= 1 and history[0] < 1:
            print("  the body prompt SELF-TAUGHT to CATCH (without being handed the fix). ✓")
        elif history and history[0] >= 1:
            print("  discriminated on round 1.")
        else:
            print("  did not learn to catch in budget — signal too thin, or fix outside the body's reach.")
    finally:
        if os.path.exists(DVD + "/src/DamnValuableStakingBug.sol"):
            os.remove(DVD + "/src/DamnValuableStakingBug.sol")


if __name__ == "__main__":
    main()
