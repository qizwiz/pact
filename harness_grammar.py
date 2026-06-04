"""
harness_grammar — emit Halmos invariant harnesses CORRECT-BY-CONSTRUCTION.

The proposer's free-form harnesses fail structurally (actor-model revert-vacuity: it minted
to one account but called from another, so every path reverted). This makes the harness
STRUCTURE a deterministic template and lets the caller fill only SEMANTIC slots. The actor
model — the caller is ALWAYS the funded+approved account — is *generated*, not prompted, so
revert-vacuity is impossible by construction.

This is the least scaffold: it encodes WHERE things go (deploy, fund, pre-state, optional
bounded adversarial step, action, assert), never WHAT the bug is. Bug-agnostic. The eventual
goal is to INDUCE this grammar from the recall-labeled harness corpus (grammar inference);
this hand-authored version is throwaway scaffolding to bootstrap that corpus.

A harness SPEC (the typed slots):
  contract     name of the contract-under-test
  imports      extra names to import from the src file (e.g. ERC20, IERC20)
  mocks        [{name, kind}] kind in {"erc20"} -> deployed; the CALLER funded+approved
  ctor_args    constructor argument expression (may reference mocks)
  params       symbolic args, e.g. ["uint256 amount", "uint256 donation"]
  bounds       require() preconditions on params
  setup        statements (run as the funded caller) establishing a non-trivial pre-state
  adversarial  optional ONE bounded environment step (donation / oracle write) — a generic slot
  action       the operation(s) under test
  assert_expr  the invariant that must hold afterward

VALIDATION (anti-rigging): the SAME emitter is run on two structurally-different classes —
externally-triggered inflation (mock asset + donation slot) and in-function conservation
(no asset, no adversarial step). If both come out non-reverting and catching, it's structure.

    .venv/bin/python harness_grammar.py
"""

from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import invariant_agent as agent  # _setup_project, _build, PROJECT
import prompt_improve as pi  # file-backed, improve-decorated prompts
from halmos_check import run_halmos


def propose_spec(name: str, src: str) -> dict | None:
    """LLM fills the grammar's SEMANTIC slots (file-backed prompt). Structural correctness
    (actor model, funding, approval) is the emitter's job, not the model's."""
    txt = agent._ask(pi.render(pi.load_prompt("sol_harness_spec"), name=name, src=src), 1500)
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def emit_harness(spec: dict) -> str:
    name = spec["contract"]
    src_import = spec.get("src_import", f"../src/{name}.sol")
    sig = ", ".join(spec.get("params", []))
    # Structural requirement the EMITTER owns, not the LLM: if we generate an ERC20 mock,
    # ERC20 (+ IERC20 for ctor args) MUST be imported. The proposer forgot ERC20 and the
    # build failed — exactly the class of bug a correct-by-construction scaffold removes.
    imports = list(spec.get("imports", []))
    if any(m.get("kind") == "erc20" for m in spec.get("mocks", [])):
        for needed in ("ERC20", "IERC20"):
            if needed not in imports:
                imports.append(needed)
    imp = ", ".join([name] + imports)

    mock_defs, mock_fields, mock_deploys, mock_fund = "", "", "", ""
    for m in spec.get("mocks", []):
        if m["kind"] == "erc20":
            T = m["name"].capitalize() + "Mock"
            mock_defs += (
                f"contract {T} is ERC20 {{\n"
                f'    constructor() ERC20("M", "M") {{}}\n'
                f"    function mint(address to, uint256 a) external {{ _mint(to, a); }}\n"
                f"}}\n\n"
            )
            mock_fields += f"    {T} {m['name']};\n"
            mock_deploys += f"        {m['name']} = new {T}();\n"
            # THE production that kills revert-vacuity: fund + approve the CALLER (address(this))
            mock_fund += (
                f"        {m['name']}.mint(address(this), type(uint192).max);\n"
                f"        {m['name']}.approve(address(c), type(uint256).max);\n"
            )

    bounds = "\n".join(f"        require({b});" for b in spec.get("bounds", []))
    setup = "\n".join(f"        {s}" for s in spec.get("setup", []))
    adv = f"        {spec['adversarial']}\n" if spec.get("adversarial") else ""
    action = "\n".join(f"        {s}" for s in spec.get("action", []))

    return f"""// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
import {{{imp}}} from "{src_import}";

{mock_defs}contract Invariants {{
    {name} c;
{mock_fields}
    constructor() {{
{mock_deploys}        c = new {name}({spec.get('ctor_args', '')});
{mock_fund}    }}

    function check_inv({sig}) public {{
{bounds}
{setup}
{adv}{action}
        assert({spec['assert_expr']});
    }}
}}
"""


def _grade(name: str, contract_src: str, harness: str):
    agent._setup_project(name, contract_src)
    built, out = agent._build(harness)
    if not built:
        return "BUILD_FAIL", out
    verdicts = run_halmos(agent.PROJECT)
    if not verdicts:
        return "NO_VERDICT(revert?)", verdicts
    caught = any(not v["proved"] for v in verdicts)
    return ("CAUGHT" if caught else "PROVED"), verdicts


# ── the two structurally-different specs, filled for ONE general emitter ──────────────
INFLATION_SPEC = {
    "contract": "VulnVault",
    "imports": ["ERC20", "IERC20"],
    "mocks": [{"name": "token", "kind": "erc20"}],
    "ctor_args": "IERC20(address(token))",
    "params": ["uint256 amount", "uint256 donation"],
    "bounds": ["amount > 0 && amount < type(uint64).max",
               "donation > 0 && donation < type(uint128).max"],
    "setup": ["uint256 s1 = c.deposit(1, address(this)); require(s1 > 0);"],
    "adversarial": "token.transfer(address(c), donation);",   # generic bounded env step
    "action": ["uint256 bef = c.balanceOf(address(this)); c.deposit(amount, address(this));"],
    "assert_expr": "c.balanceOf(address(this)) - bef > 0",
}

STAKEPOOL_SPEC = {
    "contract": "StakePool",
    "imports": [],
    "mocks": [],            # no external asset
    "ctor_args": "",
    "params": ["uint256 s", "uint256 u"],
    "bounds": ["s > 0", "u > 0"],
    "setup": ["c.stake(s); require(c.staked(address(this)) >= u);"],
    "adversarial": None,    # in-function: no environment step
    "action": ["c.unstake(u);"],
    "assert_expr": "c.totalStaked() == c.staked(address(this))",
}


def main():
    import recall_loop as R
    flat = open("/tmp/VulnVault_flat.sol").read()
    sp_correct = R.STAKEPOOL
    sp_buggy = R.STAKEPOOL.replace("        totalStaked -= amount;\n", "        // bug\n")

    print("harness_grammar: ONE emitter, two structurally-different classes (anti-rigging)\n")

    print("[A] externally-triggered INFLATION (mock asset + donation slot):")
    r, _ = _grade("VulnVault", flat, emit_harness(INFLATION_SPEC))
    print(f"    on vulnerable VulnVault -> {r}   (want CAUGHT, not revert)\n")

    print("[B] in-function CONSERVATION (no asset, no adversarial step):")
    r1, _ = _grade("StakePool", sp_correct, emit_harness(STAKEPOOL_SPEC))
    print(f"    on CORRECT StakePool   -> {r1}   (want PROVED)")
    r2, _ = _grade("StakePool", sp_buggy, emit_harness(STAKEPOOL_SPEC))
    print(f"    on BUGGY  StakePool    -> {r2}   (want CAUGHT)\n")

    ok = (r == "CAUGHT" and r1 == "PROVED" and r2 == "CAUGHT")
    print("=" * 60)
    print("ONE general emitter handled BOTH classes correct-by-construction: "
          + ("YES — structural, not rigged. ✓" if ok else "NO — see results above."))


if __name__ == "__main__":
    main()
