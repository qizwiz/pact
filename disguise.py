"""
disguise — the subtlety layer for the discovery benchmark.

A z3-verified defect is easy to find when it sits alone in a 15-line contract.
This layer CAMOUFLAGES the verified defect inside realistic, larger code so the
benchmark measures recall on something that looks like a real audit target —
WITHOUT ever weakening the ground truth.

Soundness is preserved by a hard gate, not by trust:
  - a DIFFERENT model (deepseek-v3, the strongest coder in the generator probe;
    non-Claude → it does not camouflage in a Claude-flavoured way) rewrites the
    contract: adds state, helpers, events, NatSpec, renames internals, expands
    plausible logic — but is told to leave the vulnerable computation UNCHANGED.
  - we then RE-RUN THE ORIGINAL PLANTED PoC against the disguised contract. If it
    still PASSES, the exact verified defect provably survived the rewrite. If the
    model accidentally fixed it (or broke the interface), forge fails → we reject
    and retry. The LLM can add camouflage; it cannot remove the bug and pass.

So the finder faces realistic code, but the ground truth is still the SAME bug
z3 proved + forge re-confirmed.

    .venv/bin/python disguise.py        # disguise the catalog, show survival
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bugspec  # noqa: E402
import gan  # noqa: E402

DISGUISE_MODEL = "deepseek/deepseek-chat"


def _prompt(spec: bugspec.BugSpec) -> str:
    return (
        "This is an audit-TRAINING exercise. Below is a small Solidity contract with a "
        "DELIBERATELY planted vulnerability, and a Foundry PoC that exploits it. Your job is to "
        "make the contract look like realistic production code WITHOUT removing or weakening the bug.\n\n"
        "REWRITE the contract to:\n"
        "  - add plausible extra state, helper/view functions, events, NatSpec comments, access "
        "modifiers on UNRELATED functions, and realistic surrounding logic;\n"
        "  - rename internal variables and reorganize so the flaw is not glaringly obvious.\n\n"
        "YOU MUST NOT:\n"
        "  - change the contract name (keep `Target`);\n"
        "  - change the signature of ANY function the PoC calls;\n"
        "  - fix, guard, or alter the vulnerable computation/ordering — the PoC must still pass "
        "EXACTLY as written. Camouflage the bug; do not remove it.\n\n"
        "Stay on pragma ^0.8.20. Return ONLY the Solidity source of the new Target.sol — no prose, "
        "no fences.\n\n"
        "CURRENT CONTRACT:\n"
        + spec.contract
        + "\n\nPoC THAT MUST STILL PASS:\n"
        + spec.poc
    )


def _repair(spec: bugspec.BugSpec, code: str, err: str) -> str:
    return (
        "Your rewrite failed: the planted PoC no longer passes against it.\n\nforge output:\n"
        + err
        + "\n\nYour contract:\n"
        + code
        + "\n\nFix it so the ORIGINAL PoC passes again — that means you must keep the contract named "
        "`Target`, keep every function signature the PoC uses, and keep the vulnerable behavior "
        "intact (do NOT fix the bug). Return ONLY the corrected Solidity — no prose, no fences."
    )


def disguise(spec: bugspec.BugSpec, max_tries: int = 4) -> bugspec.BugSpec | None:
    """Return a disguised copy of spec whose contract still fails the planted PoC,
    or None if a surviving disguise could not be produced."""
    code = gan._strip_fence(gan._ask(DISGUISE_MODEL, _prompt(spec), 3000))
    for attempt in range(1, max_tries + 1):
        ok, out = bugspec.confirm_contract(code, spec.poc)
        if ok:
            grew = len(code) / max(len(spec.contract), 1)
            print(
                f"  disguise: survived re-confirm on attempt {attempt} "
                f"({len(code)} chars, {grew:.1f}× original)"
            )
            return replace(spec, name=spec.name + "_disguised", contract=code)
        err = "\n".join(out.strip().splitlines()[-16:])
        print(f"  disguise: attempt {attempt} broke the PoC, repairing...")
        code = gan._strip_fence(
            gan._ask(DISGUISE_MODEL, _repair(spec, code, err), 3000)
        )
    print("  disguise: FAILED to produce a surviving disguise")
    return None


def _self_test() -> None:
    print(f"disguiser = {DISGUISE_MODEL}")
    for spec in bugspec.CATALOG:
        print(f"\n=== {spec.name} ({spec.bug_class}) ===")
        d = disguise(spec)
        if d is None:
            continue
        # the disguised bug is still the same z3-verified defect
        sym = bugspec.verify_symbolic(spec)
        vstr = "forge-only" if sym["verified"] is None else f"z3={sym['verified']}"
        print(f"  ground truth unchanged: {vstr}; planted PoC still passes ✓")


if __name__ == "__main__":
    _self_test()
