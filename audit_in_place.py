"""
audit_in_place — the canonical chain, but built IN the contract's OWN Foundry project so its
deps resolve (no flatten, no solmate-vs-OZ variance). Proven locally: DamnValuableStaking (the
contract that exhausted the flatten chain on solmate) builds + runs in-place in 0.04s.

For in-place, the harness is FREE-FORM (the LLM reads the contract + its imports and deploys the
project's REAL deps, e.g. a concrete token), because the dep set is contract-specific. The
correctness organs stay: INTENT gate (precision — rejects not-promised invariants like
self-collateralized rewards) + VERIFY (build + Halmos, scoped to our contract) + revert/compile
repair. The grammar's correct-by-construction actor model is enforced by the prompt + caught by
the revert-vacuity repair.

    .venv/bin/python audit_in_place.py   # tests on DVD's DamnValuableStaking, in-place
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
import audit  # reuse the intent gate

FORGE = os.path.expanduser("~/.foundry/bin/forge")
HALMOS = os.path.join(HERE, ".venv/bin/halmos")

PROMPT = """Write ONE Halmos symbolic test for the contract below. It lives in an existing
Foundry project — its dependencies RESOLVE, do not redefine library types.

Return ONE JSON object: {{"statement": "<one-line plain-English invariant>", "harness": "<full Solidity>"}}

HARNESS RULES (violate one and the test is worthless):
- The harness is `contract Invariants {{ ... }}` with one or more `check_<name>(<symbolic args>) public`.
- Import the contract under test from "{import_path}". Import any dependency TYPES you need from the
  SAME paths the contract imports them (read its `import` lines).
- DEPLOY the constructor dependencies: if a dep is a concrete deployable contract in the project
  (e.g. a token with a usable constructor), deploy the REAL one; otherwise define a minimal mock with
  the dep's interface AND the correct base-constructor arity.
- FUND and APPROVE address(this) BEFORE any call, or every path reverts and the test finds nothing.
- Each check_: establish a NON-TRIVIAL pre-state, optionally model ONE bounded adversarial step
  (a direct token transfer-in / donation), then assert the invariant (conservation / solvency /
  no-zero-share / monotonicity). Pick the invariant most likely to expose a real economic bug.

CONTRACT ({name}):
{src}
"""

REPAIR = """Your Halmos test failed ({kind}). Fix it, return ONE JSON object
{{"statement": "...", "harness": "<corrected full Solidity contract Invariants>"}}.
- compile: fix the error (watch ERC20 base constructor arity: OZ (name,symbol); Solmate (name,symbol,decimals)).
- revert: every path reverted -> fund+approve address(this) before calls; establish a real pre-state.
SIGNAL:
{signal}
CURRENT TEST:
{harness}
"""


def _parse(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None, ""
    try:
        d = json.loads(m.group(0))
        return d.get("harness"), d.get("statement", "")
    except Exception:
        return None, ""


def verify_in_place(project_root, harness, max_repair=3):
    test_path = os.path.join(project_root, "test", "_AuditInPlace.t.sol")
    try:
        for _ in range(max_repair + 1):
            with open(test_path, "w") as f:
                f.write(harness)
            b = subprocess.run([FORGE, "build", "--root", project_root],
                               capture_output=True, text=True, timeout=400)
            if b.returncode != 0:
                errs = "\n".join(l for l in (b.stdout + b.stderr).splitlines()
                                 if re.search(r"Error|error\[", l))[-1200:]
                nh, _ = _parse(agent._ask(REPAIR.format(kind="compile", signal=errs, harness=harness), 3500))
                if not nh:
                    return "BUILD_FAIL"
                harness = nh
                continue
            h = subprocess.run([HALMOS, "--root", project_root, "--contract", "Invariants",
                                "--function", "check"], capture_output=True, text=True, timeout=400)
            out = h.stdout + h.stderr
            if "[FAIL]" in out:
                return "CAUGHT"
            if "[PASS]" in out:
                return "PROVED"
            # timeout / error / all-revert -> repair as revert-vacuity
            nh, _ = _parse(agent._ask(REPAIR.format(kind="revert", signal="all paths reverted/timed out",
                                                    harness=harness), 3500))
            if not nh:
                return "no_verdict"
            harness = nh
        return "exhausted"
    finally:
        if os.path.exists(test_path):
            os.remove(test_path)


def audit_in_place(project_root, contract_rel, name, candidates=3):
    src = open(os.path.join(project_root, contract_rel)).read()
    import_path = "../" + contract_rel
    proved, last_stmt, feedback = False, "", ""
    for _ in range(candidates):
        prompt = PROMPT.format(name=name, src=src, import_path=import_path)
        if feedback:
            # intent gate as TEACHER, not just filter: steer away from the rejected class
            prompt += (f"\n\nA previous invariant was REJECTED as not-intended: \"{feedback}\". "
                       "Propose a DIFFERENT invariant the contract GENUINELY guarantees as a "
                       "self-property — e.g. about PRINCIPAL only, not externally-funded rewards, "
                       "and not depending on external assumptions the contract doesn't control.")
        harness, stmt = _parse(agent._ask(prompt, 3500))
        if not harness:
            continue
        last_stmt = stmt
        ok, reason = audit.intended(name, src, stmt)
        if not ok:
            feedback = reason
            continue
        status = verify_in_place(project_root, harness)
        print(f"    [gate PASSED] verify_in_place -> {status}  | inv: {stmt[:70]}")
        if status == "CAUGHT":
            return {"status": "CAUGHT", "statement": stmt}
        if status == "PROVED":
            proved, last_stmt = True, stmt
    return {"status": "PROVED" if proved else "no_result", "statement": last_stmt,
            "last_verify": status if 'status' in dir() else "n/a"}


def main():
    dvd = "/Users/jonathanhill/src/damn-vulnerable-defi"
    print("audit_in_place: canonical chain built IN the contract's own project (deps resolve)\n")
    res = audit_in_place(dvd, "src/DamnValuableStaking.sol", "DamnValuableStaking")
    print(f"  DamnValuableStaking (solmate, in-place) -> {res.get('status')}")
    print(f"    invariant: {res.get('statement')}")


if __name__ == "__main__":
    main()
