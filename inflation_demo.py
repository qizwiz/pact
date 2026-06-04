"""
inflation_demo — prove the ceiling is the TEST HARNESS, not Halmos.

Claim (from the readiness analysis): externally-triggered bugs look "out of lens" only
because the proposer tests a contract's own functions in isolation. If the harness MODELS
the adversarial environment (here: a direct donation that inflates share price), Halmos
catches the bug with the SAME invariant.

Fixture: a minimal vault with the classic first-depositor / donation INFLATION bug —
shares minted = amount * totalShares / totalAssets (integer division). An attacker deposits
1, DONATES to inflate totalAssets, and a later depositor's shares round to 0 (pays assets,
gets nothing). `donate()` models a raw token transfer-in to the vault.

Same invariant ("a positive deposit must mint > 0 shares"), two harnesses:
  - NAIVE   : deposit, then deposit again, assert minted > 0   -> no donation modelled
  - ADVERSARIAL: deposit, DONATE, then deposit, assert minted > 0  -> models the donation

Expected: NAIVE proves (misses the bug); ADVERSARIAL is VIOLATED (catches it). Same engine,
same property — only the environment the test models differs.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import invariant_agent as agent  # _setup_project, _build, PROJECT
from halmos_check import run_halmos

NAME = "Vault"
VAULT = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Vault {
    mapping(address => uint256) public shares;
    uint256 public totalShares;
    uint256 public totalAssets;   // asset balance the vault tracks

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

    // models a direct token transfer INTO the vault (a donation) — the external trigger
    function donate(uint256 amount) external {
        totalAssets += amount;
    }
}
"""

# SAME invariant ("paying a positive amount must mint at least one share"), two harnesses.
NAIVE = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Vault} from "../src/Vault.sol";

contract Invariants {
    Vault c;
    constructor() { c = new Vault(); }

    // no donation modelled: only the vault's own functions are exercised
    function check_no_zero_share_deposit(uint256 amount) public {
        c.deposit(1);                                  // bootstrap 1 share
        require(amount > 0);
        uint256 before = c.shares(address(this));
        c.deposit(amount);
        assert(c.shares(address(this)) - before > 0);  // paying assets must mint shares
    }
}
"""

ADVERSARIAL = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Vault} from "../src/Vault.sol";

contract Invariants {
    Vault c;
    constructor() { c = new Vault(); }

    // SAME invariant, but the harness MODELS the adversarial environment: a donation
    function check_no_zero_share_deposit(uint256 d, uint256 amount) public {
        c.deposit(1);                                  // attacker bootstraps 1 share
        c.donate(d);                                   // <-- the external trigger Halmos can model
        require(amount > 0);
        uint256 before = c.shares(address(this));
        c.deposit(amount);
        assert(c.shares(address(this)) - before > 0);  // victim pays `amount`, must get shares
    }
}
"""


def grade(label: str, harness: str):
    agent._setup_project(NAME, VAULT)
    built, out = agent._build(harness)
    if not built:
        print(f"  {label}: BUILD FAILED\n{out[-300:]}")
        return
    verdicts = run_halmos(agent.PROJECT)
    for v in verdicts:
        mark = "PROVED " if v["proved"] else "VIOLATED"
        ce = "" if v["proved"] else f"  counterexample={v['counterexample']}"
        print(f"  {label}: {mark} {v['function']}{ce}")
    if not verdicts:
        print(f"  {label}: (no verdicts)")


def main():
    print("inflation_demo: same invariant, two harnesses — does modelling the donation matter?\n")
    print("[1] NAIVE harness (no donation modelled):")
    grade("naive", NAIVE)
    print("\n[2] ADVERSARIAL harness (models the donation):")
    grade("adversarial", ADVERSARIAL)
    print(
        "\nIf naive PROVES and adversarial is VIOLATED: the bug was always reachable by Halmos —"
        "\nthe ceiling is what the TEST models, not the engine."
    )


if __name__ == "__main__":
    main()
