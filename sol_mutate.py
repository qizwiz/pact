"""
sol_mutate — offline Solidity CTF factory: mutate a correct contract, keep the live bugs.

No LLM (works during the OpenRouter outage). The generator is deterministic operator
mutation on Solidity source; the grader is Halmos. A CTF challenge = a mutant that
COMPILES and where Halmos proves a known invariant now BREAKS (with a concrete EVM
counterexample = the auto-grade key). Equivalent / invariant-preserving mutants are
dropped — only real, verified bugs become challenges.

This monetizes pact's PROVEN half (generate verified bug + formally grade the solve),
not the unproven half (find novel bugs). Offline-capable; the LLM only adds realism
(disguise) later.

    .venv/bin/python sol_mutate.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from halmos_check import run_halmos  # noqa: E402

FORGE = os.path.expanduser("~/.foundry/bin/forge")
FORGE_STD = "/Users/jonathanhill/src/damn-vulnerable-defi/lib/forge-std/src/"
CTF = os.path.join(HERE, "examples/solidity/ctf")

# correct reference contract — the "before" the player never sees
BANK = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Bank {
    mapping(address => uint256) public balanceOf;
    uint256 public totalSupply;

    constructor() {
        totalSupply = 1000 ether;
        balanceOf[msg.sender] = totalSupply;
    }

    function transfer(address to, uint256 amount) external {
        require(balanceOf[msg.sender] >= amount, "insufficient");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
    }

    function burn(uint256 amount) external {
        require(balanceOf[msg.sender] >= amount, "insufficient");
        balanceOf[msg.sender] -= amount;
        totalSupply -= amount;
    }
}
"""

# the invariant grader — proves on the correct Bank, used to detect live mutants
INV = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Bank} from "../src/Bank.sol";

contract Invariants {
    Bank c;
    address a = address(0xA11CE);

    constructor() { c = new Bank(); }

    function _sum() internal view returns (uint256) {
        return c.balanceOf(address(this)) + c.balanceOf(a);
    }

    function check_transfer_conserves(uint256 amount) public {
        require(_sum() == c.totalSupply());
        require(a != address(this));
        c.transfer(a, amount);
        assert(_sum() == c.totalSupply());
    }

    function check_burn_conserves(uint256 amount) public {
        require(_sum() == c.totalSupply());
        c.burn(amount);
        assert(_sum() == c.totalSupply());
    }
}
"""

_SWAPS = [
    (" -= ", " += "),
    (" += ", " -= "),
    (" >= ", " > "),
    (" <= ", " < "),
    (" == ", " != "),
    (" && ", " || "),
]


def _replace_nth(s: str, old: str, new: str, n: int):
    idx = -1
    for _ in range(n + 1):
        idx = s.find(old, idx + 1)
        if idx == -1:
            return None, -1
    return s[:idx] + new + s[idx + len(old) :], idx


def generate_mutants(src: str) -> list[dict]:
    """Single-point operator swaps + require deletions. Each mutant changes ONE thing."""
    muts = []
    for old, new in _SWAPS:
        n = 0
        while True:
            m, idx = _replace_nth(src, old, new, n)
            if m is None:
                break
            line = src[:idx].count("\n") + 1
            muts.append(
                {"src": m, "op": f"{old.strip()} -> {new.strip()}", "line": line}
            )
            n += 1
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"\s*require\(", line):
            mutant = "\n".join(lines[:i] + lines[i + 1 :]) + "\n"
            muts.append({"src": mutant, "op": "deleted require", "line": i + 1})
    return muts


def _setup() -> None:
    os.makedirs(os.path.join(CTF, "src"), exist_ok=True)
    os.makedirs(os.path.join(CTF, "test"), exist_ok=True)
    with open(os.path.join(CTF, "foundry.toml"), "w") as f:
        f.write(
            '[profile.default]\nsrc = "src"\ntest = "test"\nout = "out"\nlibs = []\n'
            "ast = true\n"
            f'remappings = ["forge-std/={FORGE_STD}"]\n'
        )
    with open(os.path.join(CTF, "test/Invariants.t.sol"), "w") as f:
        f.write(INV)


def _grade(mutant_src: str) -> list:
    """Write the mutant as Bank.sol, build, run the invariant grader. Returns verdicts
    (empty if it doesn't compile)."""
    with open(os.path.join(CTF, "src/Bank.sol"), "w") as f:
        f.write(mutant_src)
    b = subprocess.run(
        [FORGE, "build", "--root", CTF], capture_output=True, text=True, timeout=120
    )
    if b.returncode != 0:
        return []
    return run_halmos(CTF)


def main() -> None:
    _setup()
    print("sanity: grading the CORRECT Bank (both invariants must PROVE)...")
    base = _grade(BANK)
    base_ok = base and all(v["proved"] for v in base)
    print(
        f"  {[ (v['function'], 'PROVED' if v['proved'] else 'VIOLATED') for v in base ]}"
    )
    if not base_ok:
        print("  baseline invariant does not hold on the correct contract — aborting.")
        return

    mutants = generate_mutants(BANK)
    print(
        f"\ngenerated {len(mutants)} single-point mutants; grading each with Halmos...\n"
    )
    ctfs = []
    for m in mutants:
        verdicts = _grade(m["src"])
        if not verdicts:
            continue  # didn't compile
        broken = [v for v in verdicts if not v["proved"]]
        if broken:
            m["breaks"] = [(v["function"], v["counterexample"]) for v in broken]
            ctfs.append(m)

    print("=" * 60)
    print(
        f"LIVE CTF challenges (compile + break a verified invariant): {len(ctfs)}/{len(mutants)}"
    )
    for i, c in enumerate(ctfs, 1):
        inv, witness = c["breaks"][0]
        print(f"\n[CTF {i}] mutation: {c['op']} @ line {c['line']}")
        print(f"         breaks: {inv}")
        print(f"         flag (Halmos witness): {witness}")
    print(
        "\n(Each = a contract with the bug hidden; the player must find it; the witness auto-grades the solve.)"
    )


if __name__ == "__main__":
    main()
