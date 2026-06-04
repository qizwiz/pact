"""
adaptive_harness — the LLM-driven layer that absorbs real-contract VARIANCE (deploy pattern,
proxies, deps, attack shape) so the rigid scaffold stops breaking on every new contract.

Structure/semantics split (the session's law):
  - PLANNER (LLM, stochastic, untrusted)  : reads the contract, plans deploy+attack+invariant
    (prompts/sol_adaptive_harness.md). Handles upgradeable/proxy/dep variance by READING, not templates.
  - AUTO-APPLIER (deterministic macro)     : splices the ADMITTED bounded summary (Lean-discharged,
    summarize.REGISTRY) into the contract's nonlinear convert ops via a generated override subclass.
  - VERIFY (deterministic gate)            : forge build --ast (+ LLM repair loop) then halmos.
  - VALIDATE (deterministic gate, KEY)     : a halmos [FAIL] on a nonlinear contract can be a SPURIOUS
    counterexample (halmos disables nonlinear by default). So replay the cex CONCRETELY in a forge test;
    a finding counts ONLY if the assert really reverts on the concrete values. No spurious greens/reds.

  .venv/bin/python adaptive_harness.py        # smoke test: LLM plans the donation attack on
                                              # UnstoppableVault, auto-applies the summary, halmos
                                              # catches it, replay validates it's a REAL exploit.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import invariant_agent as agent
import prompt_improve as pi
import summarize

FORGE = os.path.expanduser("~/.foundry/bin/forge")
HALMOS = os.path.join(HERE, ".venv/bin/halmos")

REPAIR = """The harness below FAILS to compile. Fix ONLY what the errors report; keep the deploy
pattern, attack steps and assert intact. Return the FULL corrected Solidity file, no fences.

ERRORS:
{errors}

HARNESS:
{harness}
"""

# Deterministic auto-applier: the admitted bounded summary for ERC4626 convert ops, as an override
# subclass. Sound under a*s<2^256 (obligation discharged in lean/SummaryObligation.lean).
def summarized_subclass(name: str, import_path: str, ctor_sig: str, ctor_pass: str) -> tuple[str, str]:
    sub = name + "Summarized"
    src = f"""// SPDX-License-Identifier: MIT
pragma solidity =0.8.25;
import {{{name}}} from "{import_path}";
import {{ERC20}} from "solmate/tokens/ERC4626.sol";

contract {sub} is {name} {{
    constructor({ctor_sig}) {name}({ctor_pass}) {{}}
    function convertToShares(uint256 assets) public view override returns (uint256) {{
        uint256 supply = totalSupply;
        return supply == 0 ? assets : (assets * supply) / totalAssets();
    }}
    function convertToAssets(uint256 shares) public view override returns (uint256) {{
        uint256 supply = totalSupply;
        return supply == 0 ? shares : (shares * totalAssets()) / supply;
    }}
}}
"""
    return sub, src


def plan(name: str, src: str, context: str) -> dict:
    prompt = pi.render(open(os.path.join(HERE, "prompts/sol_adaptive_harness.md")).read(),
                       name=name, src=src, context=context)
    txt = agent._ask(prompt, 2500)
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def emit(p: dict, orig: str, target: str, target_import: str) -> str:
    """Assemble the harness from the plan, RETARGETED to the summarized subclass (orig -> target).
    Deploy/attack are the LLM's (variance-absorbing); the build-repair loop fixes compile slips."""
    args = ", ".join(p.get("symbolic_args") or ["uint256 a", "uint256 b"])
    fields = p.get("fields", "").replace(orig, target)
    setup = p.get("setup", "").replace(orig, target)
    extra = "\n".join(i for i in p.get("imports", [])
                      if ("ERC1967" in i or "DamnValuable" in i or "Mock" in i) and orig not in i)
    return f"""// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
import {{{target}}} from "{target_import}";
{extra}

contract Invariants {{
{fields}
    constructor() {{
{setup}
    }}
    function check_inv({args}) public {{
{p.get("attack_body","")}
    }}
}}
"""


def validate_replay(project_root: str, harness: str, p: dict, cex: dict) -> tuple[bool, str]:
    """THE soundness gate for blind findings: a halmos [FAIL] on a nonlinear contract may be a
    SPURIOUS counterexample. Replay the cex CONCRETELY in a forge test; the finding is REAL only if
    check_inv actually REVERTS (assert fails) on those concrete values."""
    # derive arg names+order from the BUILT harness signature (robust to repair renames), map cex by name
    msig = re.search(r"function\s+check_inv\(([^)]*)\)", harness)
    arg_names = [seg.strip().split()[-1] for seg in msig.group(1).split(",") if seg.strip()] if msig else []
    leftovers = list(cex.values())
    vals = [cex.get(n) or (leftovers[i] if i < len(leftovers) else "1") for i, n in enumerate(arg_names)]
    call = ", ".join(str(v) for v in vals)
    # imports MUST be at file top (after pragma) — appending after a contract is invalid Solidity and
    # silently false-rejects (the validator bug we caught). Inject the Test import after the pragma.
    h2 = re.sub(r"(pragma solidity[^\n]*\n)",
                r'\1import {Test} from "forge-std/Test.sol";\nimport {stdError} from "forge-std/StdError.sol";\n',
                harness, count=1)
    replay = h2 + f"""
contract Replay is Test {{
    function test_replay() public {{
        Invariants inv = new Invariants();
        // RIGOROUS: the revert must be the INVARIANT assertion (Panic 0x01), not an incidental
        // balance/require revert. (DVT mints type(uint256).max, so the cex is within balance.)
        vm.expectRevert(stdError.assertionError);
        inv.check_inv({call});
    }}
}}
"""
    test = os.path.join(project_root, "test", "_AdaptiveReplay.t.sol")
    try:
        open(test, "w").write(replay)
        r = subprocess.run([FORGE, "test", "--ast", "--root", project_root,
                            "--match-path", "test/_AdaptiveReplay.t.sol", "--match-test", "test_replay"],
                           capture_output=True, text=True, timeout=400)
        out = r.stdout + r.stderr
        return ("[PASS]" in out or "1 passed" in out), out[-800:]
    finally:
        if os.path.exists(test):
            os.remove(test)


def verify(project_root: str, harness: str, max_repair: int = 3) -> tuple[str, str, str]:
    """Returns (verdict, halmos_output, HARNESS_THAT_BUILT). The 3rd element is critical: the repair
    loop may rewrite the harness, and downstream replay-validation must use the version that actually
    compiled — not the original (a bug that silently false-rejected real findings)."""
    test = os.path.join(project_root, "test", "_Adaptive.t.sol")
    try:
        for _ in range(max_repair + 1):
            open(test, "w").write(harness)
            b = subprocess.run([FORGE, "build", "--ast", "--root", project_root],
                               capture_output=True, text=True, timeout=400)
            if b.returncode != 0:
                errs = "\n".join(l for l in (b.stdout + b.stderr).splitlines()
                                 if re.search(r"Error|error\[", l))[:1500]
                nh = agent._ask(REPAIR.format(errors=errs, harness=harness), 2500)
                nh = re.sub(r"^```[a-zA-Z]*\n?|```$", "", nh.strip())
                if "contract Invariants" not in nh:
                    return "BUILD_FAIL", errs, harness
                harness = nh
                continue
            h = subprocess.run([HALMOS, "--root", project_root, "--contract", "Invariants",
                                "--function", "check", "--solver-timeout-assertion", "90000"],
                               capture_output=True, text=True, timeout=400)
            out = h.stdout + h.stderr
            if "[FAIL]" in out:
                return "CAUGHT", out, harness
            if "[PASS]" in out:
                return "PROVED", out, harness
            return "exhausted", out[-1500:], harness
        return "BUILD_FAIL", "repair budget exhausted", harness
    finally:
        if os.path.exists(test):
            os.remove(test)


def extract_cex(halmos_out: str) -> dict:
    # parse `p_<name>_uint256_... = 0x...` counterexample assignments
    cex = {}
    for m in re.finditer(r"p_(\w+?)_uint256_\w+\s*=\s*(0x[0-9a-fA-F]+)", halmos_out):
        cex.setdefault(m.group(1), m.group(2))
    return cex


def main():
    DVD = "/Users/jonathanhill/src/damn-vulnerable-defi"
    name = "UnstoppableVault"
    rel = "src/unstoppable/UnstoppableVault.sol"
    src = open(os.path.join(DVD, rel)).read()
    context = ("Foundry project (DVD). solmate ERC4626 base. Canonical ERC20 = DamnValuableToken "
               "(import ../src/DamnValuableToken.sol), mints supply to deployer. totalAssets() = "
               "asset.balanceOf(this). Constructor: (ERC20 _token, address _owner, address _feeRecipient).")
    print("adaptive_harness: LLM plans -> auto-apply summary -> verify -> replay-validate (one button)\n")
    p = plan(name, src, context)
    if not p:
        print("  no plan produced"); return
    print(f"  deploy_kind : {p.get('deploy_kind')}")
    print(f"  invariant   : {p.get('invariant_statement')}")
    print(f"  nonlinear   : {p.get('nonlinear_ops')}", flush=True)

    # AUTO-APPLIER: splice the admitted bounded summary (ERC4626 convert ops) via override subclass
    target, subsrc = summarized_subclass(
        name, "./UnstoppableVault.sol",
        "ERC20 _token, address _owner, address _feeRecipient", "_token, _owner, _feeRecipient")
    subpath = os.path.join(DVD, "src/unstoppable", target + ".sol")
    target_import = "../src/unstoppable/" + target + ".sol"
    open(subpath, "w").write(subsrc)
    try:
        harness = emit(p, name, target, target_import)
        verdict, out, built = verify(DVD, harness)   # `built` = the harness that actually compiled
        print(f"\n  verify -> {verdict}", flush=True)
        if verdict == "CAUGHT":
            cex = extract_cex(out)
            print(f"  counterexample: {cex}", flush=True)
            valid, rout = validate_replay(DVD, built, p, cex)
            print(f"  concrete replay -> {'REAL EXPLOIT (finding validated)' if valid else 'SPURIOUS (halmos nonlinear artifact) — rejected'}")
        elif verdict == "PROVED":
            print("  no counterexample (contract safe under this invariant)")
        else:
            print(f"  {verdict} (see output)")
    finally:
        if os.path.exists(subpath):
            os.remove(subpath)


if __name__ == "__main__":
    main()
