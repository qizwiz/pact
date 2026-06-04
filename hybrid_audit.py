"""
hybrid_audit — the scaffold-macro (a Rails generator for Halmos harnesses).

The free-form proposer ASKED the LLM for the whole harness (structure + semantics tangled =
PHP spaghetti) and EXHAUSTED on real contracts. The fix is the Rails discipline: a GENERATOR
emits the structure correct-by-construction — deploy the constructor's REAL deps (in-place,
so they resolve), fund+approve the actor — and leaves ONE hole: the invariant body. The LLM
fills only the hole (small, low-risk ASK), gated by intent, verified by Halmos in-place.

  scaffold (deterministic macro)        : deploy real deps + fund actor + check_inv shell
  fill     (stochastic ask, gated)      : the body of check_inv (setup + assert)
  verify   (sound, in-place)            : forge build + halmos in the contract's own project

    .venv/bin/python hybrid_audit.py     # tests on DVD's DamnValuableStaking, in-place
"""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import invariant_agent as agent
import audit
import audit_in_place as aip
import meta_template as mt

_TOKEN = re.compile(r"IERC20|ERC20|Token|token")
_IFACE = re.compile(r"^I[A-Z]")


def build_scaffold(name: str, import_path: str, params: list[dict]) -> tuple[str, list[str]]:
    """THE MACRO: deterministic. From the constructor signature, emit imports + deploys + the
    funded actor + an empty check_inv to be filled. In-place advantage: deploy the project's
    REAL concrete deps (new DamnValuableToken()), only mock genuine interfaces."""
    # the generator OWNS structural requirements: forge-std Test provides `vm` (warp/assume/etc.)
    imports = {name: import_path, "Test": "forge-std/Test.sol"}
    fields, deploys, fund, args, assets, mock_defs = "", "", "", [], [], ""
    for i, p in enumerate(params):
        t = p.get("type", "").strip()
        an = f"asset{i}"
        if _TOKEN.search(t):
            if _IFACE.match(t):  # genuine interface -> mock (OZ ERC20 default)
                mock_defs = ("contract AssetMock is ERC20 {\n"
                             '    constructor() ERC20("A","A") {}\n'
                             "    function mint(address to,uint256 a) external { _mint(to,a); }\n}\n\n")
                imports["ERC20"] = "openzeppelin-contracts/contracts/token/ERC20/ERC20.sol"
                imports[t] = "openzeppelin-contracts/contracts/token/ERC20/IERC20.sol"
                fields += f"    AssetMock {an};\n"
                deploys += f"        {an} = new AssetMock();\n"
                fund += (f"        {an}.mint(address(this), type(uint192).max);\n"
                         f"        {an}.approve(address(c), type(uint256).max);\n")
                args.append(f"{t}(address({an}))")
            else:  # concrete deployable token in the project -> deploy the REAL one
                imports[t] = f"../src/{t}.sol"
                fields += f"    {t} {an};\n"
                deploys += f"        {an} = new {t}();\n"   # real token mints to deployer (us)
                fund += f"        {an}.approve(address(c), type(uint256).max);\n"
                args.append(an)
            assets.append(an)
        elif "uint" in t:
            args.append("1")
        elif "address" in t:
            args.append("address(this)")
        elif "bool" in t:
            args.append("false")
        else:
            args.append(f"{t}(address(0))")
    imp = ", ".join(imports.keys())
    imp_lines = "\n".join(f'import {{{k}}} from "{v}";' for k, v in imports.items())
    preamble = f"""// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
{imp_lines}

{mock_defs}contract Invariants is Test {{
    {name} c;
{fields}
    constructor() {{
{deploys}        c = new {name}({", ".join(args)});
{fund}    }}

    function check_inv(uint256 a, uint256 b) public {{
__BODY__
    }}
}}
"""
    return preamble, assets


FILL = """A Halmos test harness is ALREADY built: the contract-under-test `c` is deployed, and the
caller address(this) is funded with the asset and has approved `c`. The asset token(s): {assets}.
Symbolic args available: uint256 a, uint256 b.

Write ONLY the BODY of `function check_inv(uint256 a, uint256 b)`: bound the args with require(),
establish a non-trivial pre-state by calling c's functions (as address(this)), optionally model ONE
bounded adversarial step (a direct asset transfer to address(c)), then assert ONE invariant the
contract GENUINELY guarantees as a self-property — principal conservation/solvency, no-zero-share,
monotonicity — NOT externally-funded rewards or external assumptions.

Return exactly this format, no fences:
STATEMENT: <one-line plain-english invariant>
BODY:
<the Solidity statements of the body only — no function signature, no contract>

CONTRACT ({name}):
{src}
"""


def fill_body(name: str, src: str, assets: list[str], feedback: str = "") -> tuple[str, str]:
    prompt = FILL.format(name=name, src=src, assets=", ".join(assets) or "(none)")
    if feedback:
        prompt += f"\n\nA previous invariant was REJECTED as not-intended: \"{feedback}\". Choose a DIFFERENT, genuinely-promised invariant."
    txt = agent._ask(prompt, 1200)
    sm = re.search(r"STATEMENT:\s*(.+)", txt)
    bm = re.search(r"BODY:\s*(.*)", txt, re.S)
    stmt = sm.group(1).strip() if sm else ""
    body = bm.group(1).strip() if bm else ""
    body = re.sub(r"^```[a-zA-Z]*\n?|```$", "", body).strip()
    return stmt, body


def audit_hybrid(project_root: str, contract_rel: str, name: str, candidates: int = 3) -> dict:
    src = open(os.path.join(project_root, contract_rel)).read()
    params = mt.extract_ctor(name, src)
    preamble, assets = build_scaffold(name, "../" + contract_rel, params)
    proved, last_stmt, feedback = False, "", ""
    for _ in range(candidates):
        stmt, body = fill_body(name, src, assets, feedback)
        if not body:
            continue
        last_stmt = stmt
        ok, reason = audit.intended(name, src, stmt)
        if not ok:
            feedback = reason
            continue
        harness = preamble.replace("__BODY__", "\n".join("        " + l for l in body.splitlines()))
        status = aip.verify_in_place(project_root, harness)
        print(f"    [gate PASSED] verify -> {status}  | {stmt[:64]}")
        if status == "CAUGHT":
            return {"status": "CAUGHT", "statement": stmt}
        if status == "PROVED":
            proved, last_stmt = True, stmt
    return {"status": "PROVED" if proved else "no_result", "statement": last_stmt}


def main():
    dvd = "/Users/jonathanhill/src/damn-vulnerable-defi"
    print("hybrid_audit: scaffold-macro (deploy real deps + fund) + LLM fills only the invariant\n")
    res = audit_hybrid(dvd, "src/DamnValuableStaking.sol", "DamnValuableStaking")
    print(f"\n  DamnValuableStaking (solmate, in-place, HYBRID) -> {res.get('status')}")
    print(f"    invariant: {res.get('statement')}")


if __name__ == "__main__":
    main()
