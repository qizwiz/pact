"""
recall_loop — ground prompt self-improvement on MUTATION/ADVERSARIAL RECALL, agnostically.

The improve hooks in the live path scored prompts on skeptic-survival and build-success —
both rate a VACUOUS test 1.0 while it catches nothing. This grounds improvement on RECALL:
does the AI's proposed invariant test actually CATCH a known bug a gold oracle catches?

AGNOSTIC by construction: a FIXTURE declares
  - name        : contract name
  - propose_src : the source the AI proposes invariants FROM
  - targets     : (label, contract_src) pairs to TEST the AI's invariants against
  - gold        : a gold (non-vacuous, environment-modelling) test; the LIVE targets are
                  those the gold catches (VIOLATED). recall = live targets the AI also catches.

Two bug classes ship as fixtures, and the SAME loop drives both:
  * StakePool (in-function): propose from the CORRECT contract, targets = operator-swap
    mutants (sol_mutate). Lesson when recall is low: VACUITY — establish a non-trivial
    pre-state (stake before unstake) so the buggy path runs.
  * Vault (externally-triggered): propose from the vulnerable contract, target = itself,
    gold models a DONATION. Lesson when recall is low: model the bounded ADVERSARIAL
    ENVIRONMENT (a direct transfer-in / donation, a symbolic oracle read) — not only the
    contract's own functions.

Adding a bug class = adding a fixture. The improve loop rewrites the file-backed prompts
toward tests that catch bugs, across ALL fixtures, on the one meter that is real.

    .venv/bin/python recall_loop.py            # run the agnostic loop over all fixtures
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

THRESHOLD = getattr(pi, "THRESHOLD", 0.7)
MAX_ITERS = 3

# ───────────────────────── fixture 1: in-function (mutation) ─────────────────────────
STAKEPOOL = """// SPDX-License-Identifier: MIT
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

STAKEPOOL_GOLD = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {StakePool} from "../src/StakePool.sol";

contract Invariants {
    StakePool c;
    constructor() { c = new StakePool(); }

    function check_stake_conserves(uint256 s) public {
        c.stake(s);
        assert(c.totalStaked() == c.staked(address(this)));
    }
    function check_unstake_conserves(uint256 s, uint256 u) public {
        c.stake(s);                                  // non-trivial pre-state FIRST
        require(c.staked(address(this)) >= u);
        c.unstake(u);
        assert(c.totalStaked() == c.staked(address(this)));
    }
}
"""

# ──────────────────── fixture 2: externally-triggered (donation) ────────────────────
VAULT = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Vault {
    mapping(address => uint256) public shares;
    uint256 public totalShares;
    uint256 public totalAssets;

    function deposit(uint256 amount) external {
        uint256 minted = totalShares == 0 ? amount : amount * totalShares / totalAssets;
        shares[msg.sender] += minted;
        totalShares += minted;
        totalAssets += amount;
    }
    function redeem(uint256 s) external returns (uint256 amount) {
        amount = s * totalAssets / totalShares;
        shares[msg.sender] -= s;
        totalShares -= s;
        totalAssets -= amount;
    }
    function donate(uint256 amount) external { totalAssets += amount; }  // direct transfer-in
}
"""

VAULT_GOLD = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Vault} from "../src/Vault.sol";

contract Invariants {
    Vault c;
    constructor() { c = new Vault(); }

    // models the DONATION (the external trigger) inside an inductive single step
    function check_no_zero_share_deposit(uint256 d, uint256 amount) public {
        c.deposit(1);
        c.donate(d);                                   // bounded adversarial step
        require(amount > 0);
        uint256 before = c.shares(address(this));
        c.deposit(amount);
        assert(c.shares(address(this)) - before > 0);
    }
}
"""

FIXTURES = [
    {
        "name": "StakePool",
        "klass": "in-function (mutation)",
        "propose_src": STAKEPOOL,
        "gold": STAKEPOOL_GOLD,
        "targets": "mutants",  # generate operator-swap mutants of propose_src
        "lesson": (
            "When a bug is MISSED the usual cause is a VACUOUS check: its require() "
            "preconditions are only satisfiable at the trivial input from the contract's "
            "initial (zero) state, so the buggy path never runs. FIX: EXERCISE a real "
            "sequence that establishes a non-trivial pre-state (e.g. stake(s) BEFORE "
            "unstake(u) with require(staked>=u)) before asserting a post-state invariant."
        ),
    },
    {
        "name": "Vault",
        "klass": "externally-triggered (donation/inflation)",
        "propose_src": VAULT,
        "gold": VAULT_GOLD,
        "targets": "self",  # the vulnerable contract itself; the bug is latent + adversarial
        "lesson": (
            "When a bug is MISSED here the cause is that the test only calls the contract's "
            "OWN functions and ignores the adversarial ENVIRONMENT. FIX: model the bounded "
            "adversarial step the contract is exposed to — a DIRECT token transfer-in / "
            "donation that inflates an accounting ratio, or a symbolic oracle/price read — "
            "as ONE extra call inside the inductive step (e.g. deposit(1); donate(d); "
            "deposit(amount); assert minted>0). Keep it a single bounded step, not a full "
            "attacker contract, so it stays tractable for symbolic execution."
        ),
    },
]


def _caught(verdicts) -> bool:
    return bool(verdicts) and any(not v["proved"] for v in verdicts)


def _grade(name: str, contract_src: str, test_src: str):
    agent._setup_project(name, contract_src)
    built, _out = agent._build(test_src)
    if not built:
        return False, []
    return True, run_halmos(agent.PROJECT)


def _targets_for(fix: dict) -> list[dict]:
    if fix["targets"] == "mutants":
        return generate_mutants(fix["propose_src"])
    return [{"src": fix["propose_src"], "op": "latent-vuln", "line": 0}]


def _ai_test_for(name: str, src: str) -> tuple[str, bool]:
    invs = propose_invariants(src)
    if not invs:
        return "", False
    test_src = render_tests(name, src, invs)
    agent._setup_project(name, src)
    built = False
    for _ in range(4):
        built, out = agent._build(test_src)
        if built:
            break
        test_src = agent._ask(
            agent._repair_prompt(test_src, "\n".join(out.splitlines()[-25:]))
        )
    return test_src, built


def eval_fixture(fix: dict) -> tuple[float, str]:
    """Returns (recall, transcript) for one fixture, or (-1, reason) if unevaluable."""
    name = fix["name"]
    targets = _targets_for(fix)
    live = []
    for t in targets:
        built, v = _grade(name, t["src"], fix["gold"])
        if built and _caught(v):
            live.append(t)
    if not live:
        return -1.0, f"{name}: gold test caught no bug (no live targets) — skipped"

    test_src, built = _ai_test_for(name, fix["propose_src"])
    if not built:
        return 0.0, f"{name}: AI test never built. {fix['lesson']}"

    caught, misses = 0, []
    for t in live:
        _b, v = _grade(name, t["src"], test_src)
        if _caught(v):
            caught += 1
        else:
            misses.append(f"{t['op']}@L{t['line']}")
    recall = caught / len(live)
    transcript = (
        f"[{name} / {fix['klass']}] AI tests caught {caught}/{len(live)} bugs "
        f"(recall {recall:.2f}). MISSED: {misses or 'none'}. {fix['lesson']}"
    )
    return recall, transcript


def main():
    print("recall_loop (agnostic): grounding prompt improvement on RECALL across fixtures\n")
    for fix in FIXTURES:
        print(f"  fixture: {fix['name']:<10} class={fix['klass']}")
    print()

    history = []
    for it in range(1, MAX_ITERS + 1):
        print(f"────────── iteration {it} ──────────")
        recalls, transcripts = [], []
        for fix in FIXTURES:
            recall, transcript = eval_fixture(fix)
            print(f"  {transcript}")
            if recall >= 0:
                recalls.append(recall)
                if recall < THRESHOLD:
                    transcripts.append(transcript)
        if not recalls:
            print("  no evaluable fixtures; aborting.")
            return
        agg = sum(recalls) / len(recalls)
        history.append(agg)
        print(f"  AGGREGATE RECALL {agg:.2f} over {len(recalls)} fixture(s)\n")
        if agg >= THRESHOLD:
            print(f"  aggregate >= {THRESHOLD}: prompts catch bugs across fixtures. done.")
            break
        joined = "\n".join(transcripts)
        print(f"  aggregate < {THRESHOLD}: firing improve on render + propose prompts…")
        pi.improve_if_weak("sol_invariant_render", agg, joined, agent._ask)
        pi.improve_if_weak("sol_invariant_propose", agg, joined, agent._ask)
        print()

    print("\n================ aggregate recall trajectory ================")
    print("  " + "  ->  ".join(f"{r:.2f}" for r in history))
    if len(history) > 1 and history[-1] > history[0]:
        print("  prompts self-corrected across MULTIPLE bug classes on a real meter. ✓")


if __name__ == "__main__":
    main()
