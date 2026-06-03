"""
sol_ctf — turn a CORRECT ERC20-like contract into verified CTF challenges, offline.

Generalizes sol_mutate (which hardcoded `Bank`) to ANY token/vault-shaped contract that
sol_filter passed: it detects the balance-mapping, the supply scalar, and a transfer-like
function, TEMPLATES the conservation invariant to those names, sanity-checks that the
ORIGINAL proves it, then mutates + Halmos-grades — keeping mutants that break conservation
(with a witness = the auto-grade flag).

Honest scope: the conservation template fits the ERC20/vault class (mapping(address=>uint)
balance + a *supply/total* scalar + a (address,uint) transfer). Other contract shapes need
a *fitted* invariant — that's the LLM proposer (solidity_intent), which needs credits. This
is the offline, robust slice: token-conservation CTFs from real 0.8 contracts.

    .venv/bin/python sol_ctf.py [correct_contract.sol]   # default: examples Bank
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from halmos_check import run_halmos  # noqa: E402
from sol_mutate import generate_mutants  # noqa: E402

FORGE = os.path.expanduser("~/.foundry/bin/forge")
FORGE_STD = "/Users/jonathanhill/src/damn-vulnerable-defi/lib/forge-std/src/"
WORK = os.path.join(HERE, "examples/solidity/ctf")


def detect_erc20(src: str):
    """Return (contract, balance_getter, supply_getter, transfer_fn, uint_fns) or None.
    uint_fns = single-(uint256) external functions (burn/mint-shape) to also exercise.
    """
    cm = re.search(r"\bcontract\s+(\w+)", src)
    bm = re.search(
        r"mapping\s*\(\s*address\s*=>\s*uint256\s*\)\s*(?:public\s+)?(\w+)", src
    )
    sup = None
    for m in re.finditer(r"\buint256\s+(?:public\s+)?(\w+)\s*[;=]", src):
        if re.search(r"supply|total", m.group(1), re.I):
            sup = m.group(1)
            break
    tr = re.search(
        r"\bfunction\s+(\w+)\s*\(\s*address\s+\w+\s*,\s*uint256\s+\w+\s*\)", src
    )
    uint_fns = [
        m.group(1)
        for m in re.finditer(
            r"\bfunction\s+(\w+)\s*\(\s*uint256\s+\w+\s*\)\s*(?:external|public)", src
        )
    ]
    if cm and bm and sup and tr:
        return cm.group(1), bm.group(1), sup, tr.group(1), uint_fns
    return None


def invariant_test(contract, bal, sup, transfer, uint_fns) -> str:
    checks = [f"""    function check_conservation_{transfer}(uint256 amount) public {{
        require(_sum() == c.{sup}());
        require(a != address(this));
        c.{transfer}(a, amount);
        assert(_sum() == c.{sup}());
    }}"""]
    for fn in uint_fns:
        checks.append(f"""    function check_conservation_{fn}(uint256 amount) public {{
        require(_sum() == c.{sup}());
        c.{fn}(amount);
        assert(_sum() == c.{sup}());
    }}""")
    body = "\n\n".join(checks)
    return f"""// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {{{contract}}} from "../src/{contract}.sol";

contract Invariants {{
    {contract} c;
    address a = address(0xA11CE);
    constructor() {{ c = new {contract}(); }}
    function _sum() internal view returns (uint256) {{
        return c.{bal}(address(this)) + c.{bal}(a);
    }}

{body}
}}
"""


def _setup(contract: str, inv: str) -> None:
    os.makedirs(os.path.join(WORK, "src"), exist_ok=True)
    os.makedirs(os.path.join(WORK, "test"), exist_ok=True)
    with open(os.path.join(WORK, "foundry.toml"), "w") as f:
        f.write(
            '[profile.default]\nsrc = "src"\ntest = "test"\nout = "out"\nlibs = []\n'
            "ast = true\n"
            f'remappings = ["forge-std/={FORGE_STD}"]\n'
        )
    with open(os.path.join(WORK, "test/Invariants.t.sol"), "w") as f:
        f.write(inv)


def _grade(contract: str, contract_src: str):
    with open(os.path.join(WORK, f"src/{contract}.sol"), "w") as f:
        f.write(contract_src)
    b = subprocess.run(
        [FORGE, "build", "--root", WORK], capture_output=True, text=True, timeout=120
    )
    if b.returncode != 0:
        return []
    return run_halmos(WORK)


def main(path: str) -> None:
    src = open(path).read()
    det = detect_erc20(src)
    if not det:
        print(
            "not ERC20-like (need balance mapping + *supply* scalar + (address,uint) transfer)."
        )
        print("=> needs a fitted invariant (LLM proposer) — out of offline scope.")
        return
    contract, bal, sup, transfer, uint_fns = det
    print(
        f"detected: contract={contract} balance={bal} supply={sup} "
        f"transfer={transfer} uint_fns={uint_fns}\n"
    )
    # ensure the src file is named <contract>.sol in WORK
    _setup(contract, invariant_test(contract, bal, sup, transfer, uint_fns))

    base = _grade(contract, src)
    if not (base and all(v["proved"] for v in base)):
        print(
            f"baseline conservation does NOT hold on the input (already buggy?): "
            f"{[(v['function'], v['proved']) for v in base]}"
        )
        print("=> sol_ctf needs a CORRECT contract to mutate from.")
        return
    print("baseline: original PROVES conservation ✓\n")

    mutants = generate_mutants(src)
    print(f"{len(mutants)} mutants; grading...\n")
    ctfs = []
    for m in mutants:
        verdicts = _grade(contract, m["src"])
        if verdicts and any(not v["proved"] for v in verdicts):
            wit = next(v["counterexample"] for v in verdicts if not v["proved"])
            ctfs.append((m, wit))
    print("=" * 56)
    print(f"LIVE CTFs from {os.path.basename(path)}: {len(ctfs)}/{len(mutants)}")
    for i, (m, wit) in enumerate(ctfs, 1):
        print(f"  [CTF {i}] {m['op']} @ line {m['line']}  | flag(witness): {wit}")


if __name__ == "__main__":
    p = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(HERE, "examples/solidity/ctf/src/Bank.sol")
    )
    # Bank.sol may have been overwritten by a prior run; regenerate the canonical one if missing
    if not os.path.exists(p):
        from sol_mutate import BANK

        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(BANK)
    main(p)
