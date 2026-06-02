"""
learn_demo — smallest TRUE learning loop, grounded and on real code.

Claim under test: pact can LEARN — a PoC pattern EARNED (forge-proven) on one contract,
stored, then RETRIEVED, lets it recover a bug it previously KILLED on a DIFFERENT real
contract. Not prompt-tuning; capability transfer, measured by a KILLED→VERIFIED flip.

Target class: DoS via `require(balance == accounted)` bricked by a forced ETH send — the
class the PuppyRaffle triage KILLED (withdrawFees), because a DoS "exploit" is a REVERT,
which the old gate framing ("PASS iff bad return value") could not express.

Two fixes compose here:
  1. GATE FIX: the PoC prompt now allows asserting a REVERT / permanent-unusability
     (vm.expectRevert) as the exploit, not only a corrupted value.
  2. LEARNING: phase EARN proves a DoS PoC on a tiny scratch contract; that LLM-written,
     forge-PROVEN PoC becomes the corpus exemplar. Phase TRANSFER feeds it as few-shot to
     PuppyRaffle's withdrawFees DoS finding and checks whether it now VERIFIES.

Baseline (no exemplar) is run too, so the delta isolates the learned pattern's effect.
forge is the only oracle throughout.

    .venv/bin/python learn_demo.py
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
from llm import make_client, resolve_model  # noqa: E402

FORGE = os.path.expanduser("~/.foundry/bin/forge")
SCRATCH = os.path.join(HERE, "examples/solidity/gan")  # has forge-std remapping
PUPPY = "/Users/jonathanhill/src/4-puppy-raffle-audit"
_CLIENT = make_client()
_MODEL = resolve_model()


def _ask(prompt: str, mt: int = 2200) -> str:
    r = _CLIENT.messages.create(
        model=_MODEL, max_tokens=mt, messages=[{"role": "user", "content": prompt}]
    )
    return r.content[0].text if r.content else ""


def _strip(s: str) -> str:
    s = re.sub(r"^```[a-zA-Z]*\n?", "", s.strip())
    return re.sub(r"```\s*$", "", s).strip()


def _forge(proj: str, test_rel: str) -> str:
    res = subprocess.run(
        [FORGE, "test", "--root", proj, "--match-path", test_rel, "-vv"],
        capture_output=True,
        text=True,
        timeout=240,
    )
    return res.stdout + res.stderr


# the GATE-FIX instruction: a DoS/griefing exploit is a revert, not a bad value
_DOS_RULE = (
    "The exploit may be a DENIAL-OF-SERVICE: if the attack makes a critical function "
    "REVERT or become permanently unusable, that IS the exploit — perform the attack, "
    "then `vm.expectRevert(); target.theFunction(...)` (or assert funds are now locked). "
    "A revert caused by the attack is a passing exploit. "
)


def gate(
    proj: str,
    import_line: str,
    contract_src: str,
    finding: str,
    exemplar: str,
    tries: int = 4,
):
    ex = (
        "\nHere is a WORKING PoC for a similar bug class (use it as a pattern):\n"
        + exemplar
        + "\n"
        if exemplar
        else ""
    )
    prompt = (
        "Write a Foundry PoC confirming this vulnerability against the real contract. "
        '`import "forge-std/Test.sol";`, `contract AutoPoC is Test`, vm cheatcodes + assertions. '
        + import_line
        + " Deploy realistically, perform the exploit, and make the test PASS iff the vuln is real. "
        + _DOS_RULE
        + ex
        + "\nCONTRACT:\n"
        + contract_src
        + "\n\nVULNERABILITY:\n"
        + finding
        + "\n\nWrite test/AutoPoC.t.sol. Return ONLY Solidity — no prose, no fences."
    )
    code = _strip(_ask(prompt))
    last = ""
    for _ in range(tries):
        with open(os.path.join(proj, "test/AutoPoC.t.sol"), "w") as f:
            f.write(code)
        out = _forge(proj, "test/AutoPoC.t.sol")
        last = out
        if "[PASS]" in out:
            return "verified", code
        if "[FAIL" in out:
            return "killed", code
        err = "\n".join(out.strip().splitlines()[-22:])
        code = _strip(
            _ask(
                "Your PoC failed:\n"
                + err
                + "\nCode:\n"
                + code
                + "\nFix it. "
                + _DOS_RULE
                + " Return ONLY Solidity."
            )
        )
    return (
        "unproven",
        code + "\n/* last forge:\n" + "\n".join(last.splitlines()[-6:]) + "\n*/",
    )


_SCRATCH_CONTRACT = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Target {
    uint256 public accounted;
    function deposit() external payable { accounted += msg.value; }
    function withdrawAll(address payable to) external {
        require(address(this).balance == accounted, "balance mismatch");
        uint256 amt = accounted;
        accounted = 0;
        to.transfer(amt);
    }
}
"""

_PUPPY_DOS_FINDING = (
    "withdrawFees() has `require(address(this).balance == uint256(totalFees))`. An attacker can "
    "force ETH into the contract (e.g. selfdestruct of a funded helper) so address(this).balance "
    "exceeds totalFees forever, making withdrawFees() ALWAYS revert — fees are permanently locked (DoS)."
)


def main() -> None:
    print(f"learn_demo  finder={_MODEL}  (forge = only oracle)\n")

    # PHASE EARN: prove a DoS PoC on the scratch contract → earned exemplar
    for fn in os.listdir(os.path.join(SCRATCH, "test")):
        if fn.endswith(".sol"):
            os.remove(os.path.join(SCRATCH, "test", fn))
    with open(os.path.join(SCRATCH, "src/Target.sol"), "w") as f:
        f.write(_SCRATCH_CONTRACT)
    scratch_finding = (
        "withdrawAll() requires address(this).balance == accounted; forcing extra ETH in "
        "(selfdestruct of a funded helper) makes it revert forever → funds locked (DoS)."
    )
    verdict, earned = gate(
        SCRATCH,
        'import {Target} from "../src/Target.sol";',
        _SCRATCH_CONTRACT,
        scratch_finding,
        exemplar="",
    )
    print(f"PHASE EARN  (scratch DoS contract): {verdict}")
    if verdict != "verified":
        print("  could not earn a DoS exemplar; learning demo cannot proceed soundly.")
        print("  last attempt:\n" + earned[-600:])
        return
    print(f"  earned a forge-PROVEN DoS PoC pattern ({len(earned)} chars)\n")

    puppy_src = open(os.path.join(PUPPY, "src/PuppyRaffle.sol")).read()
    imp = 'import {PuppyRaffle} from "../src/PuppyRaffle.sol";'

    # BASELINE: PuppyRaffle DoS finding, NO exemplar (gate-fix only)
    base_v, _ = gate(PUPPY, imp, puppy_src, _PUPPY_DOS_FINDING, exemplar="")
    print(f"TRANSFER baseline (gate-fix, NO learned exemplar): {base_v}")

    # LEARNED: same finding, WITH the earned exemplar retrieved as few-shot
    learn_v, _ = gate(PUPPY, imp, puppy_src, _PUPPY_DOS_FINDING, exemplar=earned)
    print(f"TRANSFER learned  (gate-fix + retrieved exemplar): {learn_v}")

    for p in (PUPPY, SCRATCH):
        t = os.path.join(p, "test/AutoPoC.t.sol")
        if os.path.exists(t):
            os.remove(t)

    print("\n" + "=" * 56)
    print(f"original triage verdict on this finding: KILLED")
    print(f"baseline (gate-fix only):                {base_v}")
    print(f"with learned exemplar:                   {learn_v}")
    if learn_v == "verified" and base_v != "verified":
        print(
            "=> LEARNED: a pattern earned on contract A flipped a KILLED bug on real code."
        )
    elif base_v == "verified":
        print(
            "=> gate-fix alone recovered it; learning delta not isolated here (still a real-bug recovery)."
        )
    else:
        print("=> no flip; the ceiling is elsewhere (honest negative result).")


if __name__ == "__main__":
    main()
