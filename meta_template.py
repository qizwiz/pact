"""
meta_template — the template that derives the correct per-contract template.

harness_grammar's emitter encoded ONE shape (erc20 vault) and walled on anything else (e.g. a
constructor taking a concrete token type). Hand-patching per shape is whack-a-mole — the human
doing the structural learning. This goes up a level: DERIVE the structural scaffold (which mocks
to deploy, how to wire the constructor, how to fund the actor) from the CONTRACT'S OWN STRUCTURE
(its constructor signature). One meta-rule set covers all shapes instead of the ones anticipated.

The key general meta-rule: for a token-like constructor param of ANY type T (interface IERC20 OR
a concrete token like DamnValuableToken), deploy a generic ERC20 mock and pass T(address(mock)).
Compiles (address cast) and works at runtime for the ERC20 methods the contract actually calls.
Scalars -> a concrete value; addresses -> address(this).

Split of labor (consistent with the whole approach):
  - STRUCTURE (mocks/deploys/ctor-wiring/actor-funding) = DERIVED deterministically -> correct
    by construction for any constructor shape. The COMPILE + non-revert check is the verifier.
  - SEMANTICS (which invariant, which adversarial step) = the LLM fills (file-backed prompt
    sol_harness_semantic.md), graded by recall.

Validation (anti-rigging): one meta-template derives correct scaffolds for TWO different
constructor shapes — VulnVault(IERC20) and DamnValuableStaking(DamnValuableToken, uint256).

    .venv/bin/python meta_template.py
"""

from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import invariant_agent as agent
import prompt_improve as pi
from halmos_check import run_halmos

_TOKEN_TYPE = re.compile(r"IERC20|ERC20|Token|token")


def extract_ctor(name: str, src: str) -> list[dict]:
    """LLM extracts contract {name}'s constructor params [{name, type}] (compile-verified
    downstream). A structural parse, not a quality-sensitive generation."""
    prompt = (
        f"In this Solidity source, find the constructor of contract `{name}` and list its "
        f"parameters IN ORDER. Return ONLY JSON: {{\"params\": [{{\"name\": ..., \"type\": ...}}]}}. "
        f"If there is no constructor or it takes no parameters, return {{\"params\": []}}.\n\n{src}"
    )
    txt = agent._ask(prompt, 400)
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return json.loads(m.group(0)).get("params", []) if m else []
    except Exception:
        return []


def derive_scaffold(name: str, params: list[dict]) -> dict:
    """DETERMINISTIC: from the constructor params, derive mocks/deploys/ctor-args/actor-funding.
    Correct-by-construction for any shape — this is the meta-template's output."""
    fields, deploys, fund, ctor_args, assets, imports = "", "", "", [], [], set()
    ti = 0
    for p in params:
        t = p.get("type", "").strip()
        if _TOKEN_TYPE.search(t):  # token-like param of ANY type
            an = f"asset{ti}"
            ti += 1
            assets.append(an)
            fields += f"    AssetMock {an};\n"
            deploys += f"        {an} = new AssetMock();\n"
            fund += (
                f"        {an}.mint(address(this), type(uint192).max);\n"
                f"        {an}.approve(address(c), type(uint256).max);\n"
            )
            ctor_args.append(f"{t}(address({an}))")  # the general meta-rule: mock + cast to T
            imports.add(t.replace("[]", ""))
        elif "uint" in t:
            ctor_args.append("1")
        elif "address" in t:
            ctor_args.append("address(this)")
        elif "bool" in t:
            ctor_args.append("false")
        else:
            ctor_args.append(f"{t}(address(0))")
    mock_defs = ""
    if assets:
        mock_defs = (
            "contract AssetMock is ERC20 {\n"
            '    constructor() ERC20("A", "A") {}\n'
            "    function mint(address to, uint256 a) external { _mint(to, a); }\n"
            "}\n\n"
        )
        imports.add("ERC20")
    return {
        "mock_defs": mock_defs,
        "fields": fields,
        "deploys": deploys,
        "fund": fund,
        "ctor_args": ", ".join(ctor_args),
        "assets": assets,
        "imports": sorted(imports),
    }


def propose_semantic(name: str, src: str, assets: list[str]) -> dict | None:
    """LLM fills only the SEMANTIC slots (file-backed, improve-decorated prompt)."""
    txt = agent._ask(
        pi.render(pi.load_prompt("sol_harness_semantic"), name=name, src=src,
                  assets=", ".join(assets) or "(none)"),
        1500,
    )
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def emit(name: str, src_import: str, scaffold: dict, sem: dict) -> str:
    imp = ", ".join([name] + scaffold["imports"])
    sig = ", ".join(sem.get("params", []))
    bounds = "\n".join(f"        require({b});" for b in sem.get("bounds", []))
    setup = "\n".join(f"        {s}" for s in sem.get("setup", []))
    adv = f"        {sem['adversarial']}\n" if sem.get("adversarial") else ""
    action = "\n".join(f"        {s}" for s in sem.get("action", []))
    return f"""// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
import {{{imp}}} from "{src_import}";

{scaffold['mock_defs']}contract Invariants {{
    {name} c;
{scaffold['fields']}
    constructor() {{
{scaffold['deploys']}        c = new {name}({scaffold['ctor_args']});
{scaffold['fund']}    }}

    function check_inv({sig}) public {{
{bounds}
{setup}
{adv}{action}
        assert({sem['assert_expr']});
    }}
}}
"""


def run_meta(name: str, src: str):
    params = extract_ctor(name, src)
    scaffold = derive_scaffold(name, params)
    sem = propose_semantic(name, src, scaffold["assets"])
    if not sem:
        return "NO_SEMANTIC", None, scaffold, params
    harness = emit(name, f"../src/{name}.sol", scaffold, sem)
    agent._setup_project(name, src)
    built, out = agent._build(harness)
    if not built:
        return "BUILD_FAIL", out, scaffold, params
    verdicts = run_halmos(agent.PROJECT)
    if not verdicts:
        return "NO_VERDICT(revert?)", verdicts, scaffold, params
    caught = any(not v["proved"] for v in verdicts)
    return ("CAUGHT" if caught else "PROVED"), verdicts, scaffold, params


def main():
    targets = [
        ("VulnVault", "/tmp/VulnVault_flat.sol"),         # ctor: IERC20
        ("DamnValuableStaking", "/tmp/DVS_flat.sol"),     # ctor: DamnValuableToken, uint256
    ]
    print("meta_template: ONE meta-rule set, derive scaffolds for DIFFERENT constructor shapes\n")
    for name, path in targets:
        if not os.path.exists(path):
            print(f"  {name}: src missing ({path}) — skip"); continue
        src = open(path).read()
        r, _v, scaffold, params = run_meta(name, src)
        ctor = ", ".join(f"{p.get('type')}" for p in params) or "(none)"
        print(f"  {name:<22} ctor=({ctor})")
        print(f"      derived assets={scaffold['assets']} ctor_args=[{scaffold['ctor_args']}]")
        print(f"      -> {r}\n")


if __name__ == "__main__":
    main()
