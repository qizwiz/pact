"""
bugspec — symbolic-first bug construction for the discovery benchmark.

Instead of asking an LLM to *invent* a real bug (fragile, capability-bound, and
correlated with the finder's blind spots), we MODEL the bug symbolically and let
z3 prove it is real BEFORE any Solidity exists — the project's z3-first rule.

A BugSpec is a typed node:
    invariant   : a predicate that must always hold (e.g. conservation)
    correct/def : two renderings of one operation — sound, and defect-injected
    witness      : the action sequence that drives the defect to break the invariant

Pipeline per spec:
    1. SYMBOLIC GATE (z3): prove correct ⇒ invariant holds (UNSAT to break),
       and defective ⇒ invariant violable (SAT, z3 returns the witness).
       Only specs passing BOTH are real bugs — proven, not asserted.
    2. RENDER: emit a realistic Target.sol carrying the verified defect + a PoC
       built from the witness.
    3. CONFIRM (forge): run the PoC concretely. Symbolic proof AND execution.

The finder then faces Target.sol blind. Because the bug was constructed by a
non-LLM process, the finder shares no authoring bias with it.

This v0 ships ONE node — the accounting/conservation class — reusing the already
proven `conservation_invariant` z3 template. Subtlety and more classes (reentrancy,
donation inflation, access control) are the growth axis.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from contract_templates import render_z3_template  # noqa: E402

GAN = os.path.join(HERE, "examples/solidity/gan")
FORGE = os.path.expanduser("~/.foundry/bin/forge")


# --------------------------------------------------------------------------- #
# spec
# --------------------------------------------------------------------------- #
@dataclass
class BugSpec:
    name: str
    bug_class: str
    invariant: str  # human-readable; what must hold
    template_kind: str  # z3 template that models this class
    contract: str  # rendered Target.sol carrying the defect
    poc: str  # Foundry PoC realising the witness


# --------------------------------------------------------------------------- #
# z3 symbolic gate (reuses the proven conservation template)
# --------------------------------------------------------------------------- #
def _run_z3(kind: str, params: dict) -> dict:
    src = render_z3_template(kind, params)
    proc = subprocess.run(
        [sys.executable, "-c", src], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        return {"status": "error", "explanation": proc.stderr.strip()[:200]}
    return json.loads(proc.stdout.strip())


def verify_symbolic(spec: BugSpec) -> dict:
    """Prove the defect is real at the model level: correct holds, defective breaks."""
    correct = _run_z3(spec.template_kind, {"preserves_sum": True})
    defective = _run_z3(spec.template_kind, {"preserves_sum": False})
    sound_baseline = correct.get("status") == "unsat"
    defect_breaks = defective.get("status") == "sat"
    return {
        "verified": sound_baseline and defect_breaks,
        "sound_baseline": sound_baseline,  # correct ⇒ invariant holds
        "defect_breaks": defect_breaks,  # defective ⇒ invariant violable
        "witness": defective.get("counterexample"),
    }


# --------------------------------------------------------------------------- #
# forge concrete gate
# --------------------------------------------------------------------------- #
def _write(rel: str, content: str) -> None:
    path = os.path.join(GAN, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def confirm_forge(spec: BugSpec, test_name: str = "Planted.t.sol") -> tuple[bool, str]:
    for fn in os.listdir(os.path.join(GAN, "test")):
        if fn.endswith(".sol"):
            os.remove(os.path.join(GAN, "test", fn))
    _write("src/Target.sol", spec.contract)
    _write(f"test/{test_name}", spec.poc)
    res = subprocess.run(
        [FORGE, "test", "--root", GAN, "--match-path", f"test/{test_name}", "-vv"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = res.stdout + res.stderr
    return ("[PASS]" in out), out


# --------------------------------------------------------------------------- #
# v0 catalog — accounting / conservation
# --------------------------------------------------------------------------- #
# The defect: transfer() under-debits the sender (debits amount/2) while crediting
# the receiver the full amount → sum(balances) grows above totalSupply = value minted.
# Symbolically this IS preserves_sum=False on the conservation template.
_ACCOUNTING_TARGET = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// A minimal share-tracking token. Looks ordinary; transfer() is wrong.
contract Target {
    mapping(address => uint256) public balanceOf;
    uint256 public totalSupply;

    constructor() {
        totalSupply = 1_000_000 ether;
        balanceOf[msg.sender] = totalSupply;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        require(balanceOf[msg.sender] >= amount, "insufficient");
        // BUG: sender is debited only half of what the receiver is credited,
        // so the conservation invariant sum(balanceOf) == totalSupply breaks.
        balanceOf[msg.sender] -= amount / 2;
        balanceOf[to] += amount;
        return true;
    }
}
"""

_ACCOUNTING_POC = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {Target} from "../src/Target.sol";

contract PlantedPoC is Test {
    Target t;
    address attacker = makeAddr("attacker");
    address sink = makeAddr("sink");

    function setUp() public {
        t = new Target();
        // give the attacker some balance to move
        t.transfer(attacker, 1000 ether);
    }

    function test_exploit() public {
        // Witness: a single transfer mints value — sum(balances) exceeds totalSupply.
        uint256 supply = t.totalSupply();
        uint256 before = t.balanceOf(address(this)) + t.balanceOf(attacker)
            + t.balanceOf(sink);
        vm.prank(attacker);
        t.transfer(sink, 1000 ether);
        uint256 afterSum = t.balanceOf(address(this)) + t.balanceOf(attacker)
            + t.balanceOf(sink);
        // conservation broken: value was created from nothing
        assertGt(afterSum, before, "no value minted");
        assertGt(afterSum, supply, "sum still within supply");
    }
}
"""

CATALOG = [
    BugSpec(
        name="accounting_underdebit",
        bug_class="broken accounting / conservation",
        invariant="sum(balanceOf) == totalSupply",
        template_kind="conservation_invariant",
        contract=_ACCOUNTING_TARGET,
        poc=_ACCOUNTING_POC,
    ),
]


def _self_test() -> None:
    for spec in CATALOG:
        print(f"\n=== {spec.name} ({spec.bug_class}) ===")
        sym = verify_symbolic(spec)
        print(
            f"  symbolic: verified={sym['verified']} "
            f"(sound_baseline={sym['sound_baseline']}, defect_breaks={sym['defect_breaks']})"
        )
        ok, out = confirm_forge(spec)
        print(f"  forge:    planted bug PASS={ok}")
        if not ok:
            print("\n".join(out.strip().splitlines()[-12:]))


if __name__ == "__main__":
    _self_test()
