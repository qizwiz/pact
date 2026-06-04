"""severity — the missing organ: is a formal violation EXPLOITABLE at realistic scale, or only
at absurd scale (= mitigated)? Not a soft judge — Halmos again, with a realistic donation:deposit
ratio bound. CAUGHT under the bound = exploitable; PROVED under the bound = mitigated (noise)."""
import os, sys
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
import invariant_agent as agent
from halmos_check import run_halmos

def harness(name, ratio):
    return f'''// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
import {{{name}, ERC20, IERC20}} from "../src/{name}.sol";
contract MockToken is ERC20 {{
    constructor() ERC20("M","M") {{}}
    function mint(address t,uint256 a) external {{ _mint(t,a); }}
}}
contract Invariants {{
    {name} c; MockToken token;
    constructor() {{
        token = new MockToken();
        c = new {name}(IERC20(address(token)));
        token.mint(address(this), type(uint192).max);
        token.approve(address(c), type(uint256).max);
    }}
    function check_inv(uint256 amount, uint256 d) public {{
        require(amount > 0 && amount < 1e24);
        require(d > 0 && d <= amount * {ratio});   // REALISTIC: donation at most {ratio}x the deposit
        c.deposit(1, address(this));
        token.transfer(address(c), d);
        uint256 bef = c.balanceOf(address(this));
        c.deposit(amount, address(this));
        assert(c.balanceOf(address(this)) - bef > 0);
    }}
}}
'''

def grade(name, src, ratio):
    agent._setup_project(name, src)
    built,_=agent._build(harness(name, ratio))
    if not built: return "BUILD_FAIL"
    v=run_halmos(agent.PROJECT)
    if not v: return "no_verdict"
    return "EXPLOITABLE" if any(not x["proved"] for x in v) else "mitigated(PROVED)"

CASES=[("VulnVault","/tmp/VulnVault_flat.sol"),("OffsetVault","/tmp/OffsetVault_flat.sol")]

# HONEST LIMITATION (verified 2026-06-03): this organ RANKS exploitability soundly on the SAT
# side — "EXPLOITABLE" = Halmos FOUND a counterexample under the ratio bound (fast, sound). But
# the "no_verdict" case is NOT a sound mitigation proof: OffsetVault@1e4 came back no_verdict and
# raw Halmos showed it was a TIMEOUT (proving absence/UNSAT times out on the offset mulDiv). So
# read no_verdict as "couldn't decide in the budget", NOT "safe". Severity = smallest ratio at
# which an exploit is FOUND; low = act on it, high = deprioritize. The mitigation half is
# timeout-bounded, not sound — symbolic execution's SAT-fast / UNSAT-slow asymmetry.

def main():
    print("severity = re-verify under realistic donation:deposit ratio bound\n")
    for ratio in (10000, 10_000_000):
        print(f"-- ratio bound  d <= amount * {ratio}:")
        for name,path in CASES:
            if not os.path.exists(path): continue
            print(f"    {name:<12} -> {grade(name, open(path).read(), ratio)}")
        print()

if __name__ == "__main__":
    main()


def prep_muldiv(src: str) -> str:
    """ARITHMETIC PREP that makes the mitigation/UNSAT side SOUND+tractable. Swaps OZ's 512-bit
    full-precision Math.mulDiv for the bounded-equivalent (x*y)/denominator. SOUND ONLY under the
    realistic bound (values where x*y < 2^256) — which is exactly the regime severity verifies in.
    Demonstrated: OffsetVault@1e4 UNSAT went from TIMEOUT(>300s) to PASS in 1.2s. The realistic
    bound does triple duty: defines exploitability, shrinks the solver, makes this swap sound."""
    sig = ("function mulDiv(uint256 x, uint256 y, uint256 denominator) "
           "internal pure returns (uint256 result)")
    i = src.find(sig)
    if i < 0:
        return src
    b = src.index("{", i)
    depth, j = 0, b
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[:i] + sig + " { return (x * y) / denominator; }" + src[j + 1:]
