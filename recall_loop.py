"""
recall_loop — ground the prompt self-improvement on MUTATION RECALL, not "did it run".

The improve hooks in solidity_intent score prompts on skeptic-survival and build-success:

    pscore = surviving / proposed          # propose: did the skeptic spare it?
    rscore = 1.0 if (built and verdicts)   # render:  did it build + run?

A VACUOUS test (a check whose preconditions are only satisfiable at the trivial input, so
the buggy path is never exercised) scores 1.0 on BOTH — it builds, Halmos runs, the skeptic
doesn't falsify a correct invariant — and yet it catches nothing. That is exactly the false
negative we hit on StakePool.unstake: `require(staked(user) >= amount)` from a zero-init
contract forces amount == 0, so `unstake(0)` is a no-op that trivially "conserves".

This module closes the loop with the signal those scores are missing — RECALL against
known-buggy mutants:

    1. generate operator-swap mutants of a CORRECT seed (sol_mutate)
    2. a GOLD invariant test (hand-written, non-vacuous) labels which mutants are REAL bugs
    3. the AI proposer/renderer (solidity_intent, self-improving prompts) generates its test
    4. recall = (gold-live mutants the AI test CATCHES) / (gold-live mutants)
    5. feed recall to prompt_improve.improve_if_weak -> the prompt rewrites itself toward
       tests that actually detect bugs, and we re-propose and re-measure.

A vacuous AI test scores recall 0 here (it catches no mutant), so the loop finally SEES the
failure the build/survival scores were blind to.

    .venv/bin/python recall_loop.py            # StakePool (zero-init: vacuity-prone)
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sol_mutate import generate_mutants  # noqa: E402
from solidity_intent import propose_invariants, render_tests  # noqa: E402
import invariant_agent as agent  # noqa: E402  (_setup_project, _build, _ask, _repair_prompt, PROJECT)
import prompt_improve as pi  # noqa: E402
from halmos_check import run_halmos  # noqa: E402

THRESHOLD = pi.THRESHOLD if hasattr(pi, "THRESHOLD") else 0.7
MAX_ITERS = 3

# ---- fixture: a CORRECT, zero-init staking pool (the vacuity-prone shape) ---------------
SEED_NAME = "StakePool"
SEED = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract StakePool {
    mapping(address => uint256) public staked;
    uint256 public totalStaked;

    function stake(uint256 amount) external {
        staked[msg.sender] += amount;
        totalStaked += amount;
    }

    function unstake(uint256 amount) external {
        require(staked[msg.sender] >= amount, "insufficient stake");
        staked[msg.sender] -= amount;
        totalStaked -= amount;
    }
}
"""

# GOLD test: the *non-vacuous* conservation check. It STAKES first to establish a real
# pre-state, THEN unstakes, THEN asserts totalStaked == staked[this]. Proves on the correct
# seed; catches operator-swap mutants in either function. This is ground truth for "real bug".
GOLD_TEST = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {StakePool} from "../src/StakePool.sol";

contract Invariants {
    StakePool c;
    constructor() { c = new StakePool(); }

    // only address(this) ever acts, so totalStaked must equal staked[this] after any sequence
    function check_stake_conserves(uint256 s) public {
        c.stake(s);
        assert(c.totalStaked() == c.staked(address(this)));
    }

    function check_unstake_conserves(uint256 s, uint256 u) public {
        c.stake(s);                                  // establish a non-zero pre-state FIRST
        require(c.staked(address(this)) >= u);
        c.unstake(u);
        assert(c.totalStaked() == c.staked(address(this)));
    }
}
"""


def _caught(verdicts) -> bool:
    """A bug is detected iff some invariant is VIOLATED."""
    return bool(verdicts) and any(not v["proved"] for v in verdicts)


def _grade(name: str, contract_src: str, test_src: str):
    """Write contract_src as src/{name}.sol, build with test_src, run Halmos.
    Returns (built, verdicts). verdicts == [] if it doesn't compile."""
    agent._setup_project(name, contract_src)  # writes foundry.toml + src/{name}.sol
    built, _out = agent._build(test_src)  # writes test/Invariants.t.sol + forge build
    if not built:
        return False, []
    return True, run_halmos(agent.PROJECT)


def find_live_mutants(name: str, mutants: list[dict], gold_test: str) -> list[dict]:
    """Ground truth: a mutant is a REAL bug iff the GOLD test catches it (compiles + violated)."""
    live = []
    for i, m in enumerate(mutants):
        built, v = _grade(name, m["src"], gold_test)
        status = "live-bug" if (built and _caught(v)) else ("equiv/revert" if built else "no-compile")
        print(f"    mutant {i+1:2}/{len(mutants)}  {m['op']:<14} @L{m['line']:<3} -> {status}")
        if built and _caught(v):
            live.append(m)
    return live


def ai_test_for(name: str, seed_src: str) -> tuple[str, bool]:
    """Run the SELF-IMPROVING proposer + renderer to get the AI's invariant test (build-repair)."""
    invs = propose_invariants(seed_src)
    print(f"    AI proposed {len(invs)} invariant(s): {[i.get('id') for i in invs]}")
    if not invs:
        return "", False
    test_src = render_tests(name, seed_src, invs)
    agent._setup_project(name, seed_src)
    built = False
    for attempt in range(4):
        built, out = agent._build(test_src)
        if built:
            break
        test_src = agent._ask(
            agent._repair_prompt(test_src, "\n".join(out.splitlines()[-25:]))
        )
    return test_src, built


def measure_recall(name: str, seed_src: str, live: list[dict]) -> tuple[float, str, bool]:
    """Build the AI test, sanity-check it holds on the correct seed, then measure how many
    gold-live mutants it catches. Returns (recall, transcript, baseline_ok)."""
    test_src, built = ai_test_for(name, seed_src)
    if not built:
        return 0.0, "AI test never built after repairs.", False

    _b, base_v = _grade(name, seed_src, test_src)
    base_ok = bool(base_v) and not _caught(base_v)  # invariant must HOLD on correct code

    caught, misses = 0, []
    for m in live:
        _b, v = _grade(name, m["src"], test_src)
        if _caught(v):
            caught += 1
        else:
            misses.append(f"{m['op']} @L{m['line']}")
    recall = caught / max(len(live), 1)

    transcript = (
        f"AI invariant tests CAUGHT {caught}/{len(live)} planted bugs (recall {recall:.2f}). "
        f"MISSED these real bugs: {misses or 'none'}. "
        f"(Sanity: tests hold on the correct contract = {base_ok}.) "
        "When a bug is MISSED, the usual cause is a VACUOUS check: its require() preconditions "
        "are only satisfiable at the trivial input from the contract's initial (zero) state, so "
        "the buggy code path is never exercised. FIX: before asserting a post-state invariant, "
        "EXERCISE a real sequence that establishes a non-trivial pre-state (e.g. stake(s) BEFORE "
        "unstake(u) with require(staked>=u)), so the operation under test actually runs."
    )
    return recall, transcript, base_ok


def main():
    print(f"recall_loop: grounding prompt self-improvement on MUTATION RECALL ({SEED_NAME})\n")
    mutants = generate_mutants(SEED)
    print(f"generated {len(mutants)} operator-swap/deletion mutants")
    print("labelling real bugs with the GOLD (non-vacuous) test:")
    live = find_live_mutants(SEED_NAME, mutants, GOLD_TEST)
    print(f"\n=> {len(live)} gold-live mutants (real conservation bugs the AI SHOULD catch)\n")
    if not live:
        print("no live mutants — gold test found nothing to catch; aborting.")
        return

    history = []
    for it in range(1, MAX_ITERS + 1):
        print(f"────────── iteration {it} ──────────")
        recall, transcript, base_ok = measure_recall(SEED_NAME, SEED, live)
        history.append(recall)
        print(f"  RECALL {recall:.2f}  (baseline holds on correct code: {base_ok})")
        print(f"  {transcript}\n")
        if recall >= THRESHOLD:
            print(f"  recall >= {THRESHOLD}: prompt is catching bugs — no rewrite needed.")
            break
        print(f"  recall < {THRESHOLD}: firing improve_if_weak on render + propose prompts…")
        pi.improve_if_weak("sol_invariant_render", recall, transcript, agent._ask)
        pi.improve_if_weak("sol_invariant_propose", recall, transcript, agent._ask)
        print()

    print("\n================ recall trajectory ================")
    print("  " + "  ->  ".join(f"{r:.2f}" for r in history))
    if len(history) > 1 and history[-1] > history[0]:
        print("  the prompt SELF-CORRECTED on a signal the old scores were blind to. ✓")
    elif history[0] >= THRESHOLD:
        print("  prompt already at/above threshold on this seed.")
    else:
        print("  recall did not improve within the iteration budget (see transcripts).")


if __name__ == "__main__":
    main()
