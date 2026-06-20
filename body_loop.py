"""
body_loop — does the BODY prompt self-improve to write VERIFIABLE invariants? (the semantic hole)

The macro owns structure (deploy/fund/vm). The remaining gap is semantic: the body over-reaches
(reward-math, the rewards-solvency FP) instead of the simple principal invariant that proves. That
is exactly what self-improvement should fix — IF the grounded signal is rich enough (the
lesson-stripped law). This watches the trajectory across rounds, it does not assert off one pass.

Grounded score = PROVED-rate on CLEAN fixtures. An over-reaching body (exhausts) AND an FP body
(CAUGHT on clean code) both score 0; only a simple SOUND invariant that PROVES wins. The transcript
tells the prompt why it failed. improve_if_weak rewrites prompts/sol_body_fill.md.

    .venv/bin/python body_loop.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import invariant_agent as agent
from plumbline import prompt_improve as pi
import meta_template as mt
import hybrid_audit as H

FORGE = os.path.expanduser("~/.foundry/bin/forge")
HALMOS = os.path.join(HERE, ".venv/bin/halmos")
THRESHOLD = getattr(pi, "THRESHOLD", 0.7)
ROUNDS = 3

DVD = "/Users/jonathanhill/src/damn-vulnerable-defi"
# CLEAN fixtures (no in-lens bug) -> the right answer is PROVED. CAUGHT here = false positive.
FIXTURES = [
    ("src/DamnValuableStaking.sol", "DamnValuableStaking"),
    ("src/DamnValuableToken.sol", "DamnValuableToken"),
]


def eval_body(project, rel, name):
    """Returns (score in {0,1}, transcript-line). score 1 only if a SOUND clean verdict (PROVED)."""
    src = open(os.path.join(project, rel)).read()
    params = mt.extract_ctor(name, src)
    preamble, assets = H.build_scaffold(name, "../" + rel, params)
    stmt, body = H.fill_body(name, src, assets)
    if not body:
        return 0, f"{name}: no body produced."
    harness = preamble.replace("__BODY__", "\n".join("        " + l for l in body.splitlines()))
    test_path = os.path.join(project, "test", "_BodyLoop.t.sol")
    try:
        with open(test_path, "w") as f:
            f.write(harness)
        # --ast REQUIRED: halmos skips artifacts lacking the AST -> stale-artifact fake pass without it.
        b = subprocess.run([FORGE, "build", "--ast", "--root", project], capture_output=True, text=True, timeout=400)
        if b.returncode != 0:
            err = " ".join(l.strip() for l in (b.stdout + b.stderr).splitlines()
                           if re.search(r"Error|error\[", l))[:200]
            return 0, f"{name}: BUILD_FAIL ({err}). Keep the body simple/valid Solidity."
        h = subprocess.run([HALMOS, "--root", project, "--contract", "Invariants", "--function", "check"],
                           capture_output=True, text=True, timeout=300)
        out = h.stdout + h.stderr
        if "[PASS]" in out:
            return 1, f"{name}: PROVED (sound) — '{stmt[:60]}'"
        if "[FAIL]" in out:
            return 0, (f"{name}: CAUGHT on a CLEAN contract = FALSE POSITIVE. The invariant "
                       f"'{stmt[:60]}' is not a real bug (likely asserts externally-funded rewards). "
                       "Use a simpler self-guaranteed invariant (principal: balanceOf(c) >= total staked).")
        return 0, (f"{name}: NO CLEAN VERDICT (reverted/timeout). The body over-reached "
                   "(reward-math / vm.warp / earned). The contract is CLEAN; a SIMPLE principal "
                   "invariant (e.g. asset balance of c >= total staked principal) PROVES fast.")
    finally:
        if os.path.exists(test_path):
            os.remove(test_path)


def main():
    print("body_loop: does the body prompt SELF-IMPROVE to verifiable invariants? (PROVED-rate, clean fixtures)\n")
    history = []
    for r in range(1, ROUNDS + 1):
        print(f"────────── round {r} ──────────")
        scores, transcripts = [], []
        for rel, name in FIXTURES:
            s, t = eval_body(DVD, rel, name)
            print(f"  {t}")
            scores.append(s)
            if s == 0:
                transcripts.append(t)
        score = sum(scores) / len(scores)
        history.append(score)
        print(f"  PROVED-rate {score:.2f}\n")
        if score >= THRESHOLD:
            print(f"  >= {THRESHOLD}: body prompt writes sound verifiable invariants. done.")
            break
        pi.improve_if_weak("sol_body_fill", score, "\n".join(transcripts), agent._ask)
        print()
    print("\n================ trajectory ================")
    print("  " + "  ->  ".join(f"{x:.2f}" for x in history))
    if len(history) > 1 and history[-1] > history[0]:
        print("  the body prompt SELF-CORRECTED on a grounded signal. ✓")
    elif history[0] >= THRESHOLD:
        print("  already sound on round 1.")
    else:
        print("  plateaued — signal not discoverable enough (the lesson-stripped law). honest.")


if __name__ == "__main__":
    main()
