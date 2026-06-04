"""
broad_backtest — broad (any library/shape), not narrow. Free-form LLM harness + a verifier
with TEETH: it catches not just compile errors but REVERT-VACUITY (all paths reverted = the
actor-model bug), and repairs both. The old pipeline swallowed revert-vacuity as "0/0" — that
blindness WAS the narrowness. The structural lesson (fund+approve the caller) is enforced by
DETECTION + repair, not by a rigid template, so the LLM is free to handle library variance
(solmate vs OZ, concrete token types) by reading the contract.

Verifier-at-every-floor: compile (repair) -> halmos -> revert-vacuity check (repair) -> verdict.

    .venv/bin/python broad_backtest.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import invariant_agent as agent
from halmos_check import run_halmos

PROMPT = """You write ONE Halmos symbolic test for the contract below. Return ONLY Solidity:
`contract Invariants {{ ... }}` with one or more `check_<name>(<symbolic args>) public` functions.

RULES (violate one and the test is worthless):
- The test contract IS the acting account. DEPLOY whatever the constructor needs — READ the
  contract to get exact dependency types and their constructors. If it needs an ERC20 asset,
  define a mock that extends the SAME ERC20 base the contract imports, with the CORRECT
  constructor arguments for that base (OpenZeppelin ERC20 is (name, symbol); Solmate ERC20 is
  (name, symbol, decimals)). Look at the source to tell which.
- FUND and APPROVE address(this) BEFORE any call, or every path reverts and the test finds nothing.
- Each check_: take symbolic args, establish a NON-TRIVIAL pre-state, optionally model ONE bounded
  adversarial step (a direct token transfer-in / donation to the contract), then assert the
  invariant (conservation / solvency / no-zero-share / monotonicity).

CONTRACT ({name}):
{src}
"""

REPAIR_COMPILE = """Your Halmos test failed to COMPILE. Fix it. Keep `contract Invariants` with
check_ functions. Pay attention to the mock's ERC20 base constructor arity (OZ=2 args, Solmate=3).
COMPILE ERRORS:
{err}

YOUR TEST:
{test}

Return ONLY the corrected Solidity."""

REPAIR_REVERT = """Your test COMPILED but EVERY symbolic path REVERTED — Halmos found nothing, so
the test is vacuous. The usual cause: the acting account (address(this)) was not funded/approved
before the call, OR a require()/precondition is unsatisfiable from the contract's initial state.
FIX THE ACTOR MODEL: mint tokens to address(this) and approve the contract in the constructor
BEFORE any deposit; establish a real non-zero pre-state inside the check function. Keep the same
invariant.

YOUR TEST:
{test}

Return ONLY the corrected Solidity."""


def run_broad(name: str, src: str, max_iter: int = 5):
    test = agent._ask(PROMPT.format(name=name, src=src), 3000)
    history = []
    for i in range(max_iter):
        agent._setup_project(name, src)
        built, out = agent._build(test)
        if not built:
            history.append("compile-fail->repair")
            test = agent._ask(
                REPAIR_COMPILE.format(err="\n".join(out.splitlines()[-20:]), test=test), 3000
            )
            continue
        verdicts = run_halmos(agent.PROJECT)
        if not verdicts:  # built but no pass/fail verdicts == revert-vacuity (the actor-model bug)
            history.append("revert-vacuity->repair")
            test = agent._ask(REPAIR_REVERT.format(test=test), 3000)
            continue
        caught = any(not v["proved"] for v in verdicts)
        return ("CAUGHT" if caught else "PROVED"), verdicts, history
    return "EXHAUSTED", None, history


def main():
    targets = [
        ("VulnVault", "/tmp/VulnVault_flat.sol"),       # OZ ERC20, known inflation bug -> want CAUGHT
        ("DamnValuableStaking", "/tmp/DVS_flat.sol"),   # SOLMATE ERC20, reward-accounting, unknown
    ]
    print("broad_backtest: free-form + verifier-with-teeth (compile + revert-vacuity repair)")
    print("Testing BOTH libraries to show broad != fantasy.\n")
    for name, path in targets:
        if not os.path.exists(path):
            print(f"  {name}: src missing — skip"); continue
        src = open(path).read()
        r, verdicts, history = run_broad(name, src)
        print(f"  {name:<22} -> {r}    (repairs: {history or 'none'})")
        for v in (verdicts or []):
            mark = "PROVED" if v.get("proved") else "VIOLATED"
            print(f"        {mark} {v.get('function')}")
        print()


if __name__ == "__main__":
    main()
