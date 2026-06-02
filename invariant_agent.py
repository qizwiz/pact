"""
invariant_agent — the AI-proposer layer: pact proposes invariants, Halmos discharges them.

This closes the seam in pact's real engine. Given a Solidity contract, the model PROPOSES
the contract's global invariants (conservation, solvency, monotonicity, authorization,
no-overflow) and emits each as a Halmos `check_*` symbolic test. Halmos then proves it for
ALL inputs or returns a concrete EVM-real counterexample (BitVec256). The model proposes;
the symbolic engine decides — propose/prove, with the sound oracle holding the verdict.

Division of soundness (honest):
- Halmos is SOUND on "does the code satisfy the stated property" (proof or counterexample).
- The model is the judge of "is this the RIGHT property" — a VIOLATED invariant is a strong
  candidate finding (real counterexample) but its bug-ness depends on the invariant being
  legitimate intent. That's the auditor-in-the-loop seam, by design.

    .venv/bin/python invariant_agent.py [contract.sol] [ContractName]
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(HERE, ".env"))
from halmos_check import run_halmos  # noqa: E402
from llm import make_client, resolve_model  # noqa: E402

FORGE = os.path.expanduser("~/.foundry/bin/forge")
FORGE_STD = "/Users/jonathanhill/src/damn-vulnerable-defi/lib/forge-std/src/"
PROJECT = os.path.join(HERE, "examples/solidity/proposer")
_CLIENT = make_client()
_MODEL = resolve_model()


def _ask(prompt: str, mt: int = 2600) -> str:
    r = _CLIENT.messages.create(
        model=_MODEL, max_tokens=mt, messages=[{"role": "user", "content": prompt}]
    )
    txt = r.content[0].text if r.content else ""
    txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt.strip())
    return re.sub(r"```\s*$", "", txt).strip()


def _propose_prompt(name: str, src: str) -> str:
    return (
        "You are a smart-contract auditor using symbolic execution (Halmos). Given a Solidity "
        "contract, PROPOSE its key GLOBAL invariants and express EACH as a Halmos symbolic test. "
        "Halmos runs each `check_*` function with SYMBOLIC arguments and either proves the "
        "assertion for ALL inputs or returns a counterexample.\n\n"
        "Write ONE test contract:\n"
        "- SPDX + pragma ^0.8.20\n"
        f'- import {{{name}}} from "../src/{name}.sol";\n'
        f"- contract Invariants {{ {name} c; constructor() {{ c = new {name}(); }} ... }}\n"
        "- one or more `function check_<name>(<args>) public` — Halmos makes the args symbolic. "
        "Use `require(...)` for input assumptions (reverting paths are pruned); `assert(<inv>)` for "
        "the property that MUST hold.\n\n"
        "Propose invariants real audits care about, e.g.: CONSERVATION (an operation must not "
        "create/destroy value — total tracked == sum over a SMALL fixed set of accounts you control, "
        "since symbolic sums over all accounts are infeasible: e.g. assert totalX == c.bal(address(this)) "
        "+ c.bal(a)); SOLVENCY (contract asset balance >= recorded obligations); MONOTONICITY / "
        "no-free-value (a user can't end with more than they put in); AUTHORIZATION (a value-moving "
        "effect implies the caller was authorized); NO-OVERFLOW of a critical accumulator.\n\n"
        "The constructor runs as THIS test contract, so it is the initial owner/holder. Exercise the "
        "contract's state-changing functions with the symbolic args, THEN assert the invariant on the "
        "resulting state. One invariant per check_. Cover the main mutating functions.\n\n"
        "Return ONLY the Solidity source of the test contract — no prose, no fences.\n\n"
        f"CONTRACT (src/{name}.sol):\n{src}"
    )


def _repair_prompt(test_src: str, err: str) -> str:
    return (
        "Your Halmos test failed to build/run. Fix it. Keep the contract named `Invariants`, keep "
        "check_* functions with symbolic args + assert(invariant). Return ONLY corrected Solidity.\n\n"
        f"BUILD/RUN OUTPUT:\n{err}\n\nYOUR TEST:\n{test_src}"
    )


def _setup_project(name: str, contract_src: str) -> None:
    os.makedirs(os.path.join(PROJECT, "src"), exist_ok=True)
    os.makedirs(os.path.join(PROJECT, "test"), exist_ok=True)
    with open(os.path.join(PROJECT, "foundry.toml"), "w") as f:
        f.write(
            '[profile.default]\nsrc = "src"\ntest = "test"\nout = "out"\nlibs = []\n'
            "ast = true\n"  # Halmos needs AST in artifacts; without it it skips all tests
            f'remappings = ["forge-std/={FORGE_STD}"]\n'
        )
    with open(os.path.join(PROJECT, f"src/{name}.sol"), "w") as f:
        f.write(contract_src)


def _build(test_src: str) -> tuple[bool, str]:
    with open(os.path.join(PROJECT, "test/Invariants.t.sol"), "w") as f:
        f.write(test_src)
    r = subprocess.run(
        [FORGE, "build", "--root", PROJECT], capture_output=True, text=True, timeout=120
    )
    return (r.returncode == 0), (r.stdout + r.stderr)


def propose_and_check(contract_path: str, name: str, max_repair: int = 3) -> dict:
    src = open(contract_path).read()
    _setup_project(name, src)
    test_src = _ask(_propose_prompt(name, src))
    for attempt in range(max_repair + 1):
        ok, out = _build(test_src)
        if ok:
            break
        print(f"  build attempt {attempt+1} failed; repairing…")
        test_src = _ask(_repair_prompt(test_src, "\n".join(out.splitlines()[-25:])))
    else:
        return {"built": False, "test_src": test_src, "verdicts": []}
    checks = re.findall(r"function (check_\w+)", test_src)
    print(f"  AI proposed {len(checks)} invariant(s): {', '.join(checks)}")
    verdicts = run_halmos(PROJECT)
    return {"built": True, "test_src": test_src, "verdicts": verdicts}


_DEMO_NAME = "StakePool"
_DEMO = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// A staking pool. Users stake and unstake an internal token balance.
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
        // BUG: forgot `totalStaked -= amount;` — totalStaked drifts above the real sum
    }
}
"""

if __name__ == "__main__":
    if len(sys.argv) > 2:
        path, name = sys.argv[1], sys.argv[2]
    else:
        os.makedirs("/tmp/inv_demo", exist_ok=True)
        path = "/tmp/inv_demo/StakePool.sol"
        open(path, "w").write(_DEMO)
        name = _DEMO_NAME
    print(
        f"invariant_agent: proposing + discharging invariants for {name}  (finder={_MODEL})\n"
    )
    res = propose_and_check(path, name)
    if not res["built"]:
        print("could not build a valid Halmos test after repairs")
        sys.exit(1)
    print()
    for v in res["verdicts"]:
        if v["proved"]:
            print(f"  ✅ PROVED   {v['function']}")
        else:
            print(
                f"  🔴 VIOLATED {v['function']} — counterexample {v['counterexample']}"
            )
    bugs = [v for v in res["verdicts"] if not v["proved"]]
    print(
        f"\n{len(bugs)}/{len(res['verdicts'])} AI-proposed invariants VIOLATED with EVM-real counterexamples."
    )
    print(
        "(AI proposed the invariants; Halmos decided. Violations = candidate findings to review.)"
    )
