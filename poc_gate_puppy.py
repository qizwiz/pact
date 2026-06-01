"""
poc_gate_puppy — PoC gate at REAL complexity: PuppyRaffle reentrancy.

Same hands-off gate (forge-std + repair loop) but targeting the real, multi-file,
OZ-dependent PuppyRaffle project. Tests whether the LLM can auto-write a forge PoC
that COMPILES + RUNS + confirms a real bug against real code.

HONEST: PuppyRaffle is famous (memorized) -> this tests the GATE's real-complexity
scaling + PoC-gen, NOT discovery. Discovery is the GAN with un-authored bugs.
"""

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(HERE, ".env"))
from llm import make_client, resolve_model  # noqa: E402

PROJ = "/Users/jonathanhill/src/4-puppy-raffle-audit"
FORGE = os.path.expanduser("~/.foundry/bin/forge")
_CLIENT = make_client()
_MODEL = resolve_model()
SRC = open(os.path.join(PROJ, "src/PuppyRaffle.sol")).read()[:15000]

FINDING = (
    "Reentrancy in PuppyRaffle.refund(): it sends the entrance fee to msg.sender via a "
    "low-level call BEFORE zeroing players[playerIndex], so a malicious contract can "
    "re-enter refund() in its receive() and drain the contract of multiple refunds for one entry."
)


def _llm(prompt: str) -> str:
    r = _CLIENT.messages.create(model=_MODEL, max_tokens=2000, messages=[{"role": "user", "content": prompt}])
    code = r.content[0].text if r.content else ""
    code = re.sub(r"^```[a-zA-Z]*\n?", "", code.strip())
    return re.sub(r"```\s*$", "", code).strip()


def _initial() -> str:
    return (
        "Write a Foundry PoC (forge-std allowed: `import \"forge-std/Test.sol\";`, `contract X is Test`, "
        "vm cheatcodes, assertions) that CONFIRMS this vulnerability against the real contract.\n\n"
        "Contract (src/PuppyRaffle.sol):\n" + SRC + "\n\n"
        "VULNERABILITY:\n" + FINDING + "\n\n"
        'Write test/AutoPoC.t.sol. Import PuppyRaffle from "../src/PuppyRaffle.sol". Deploy it with a '
        "valid entranceFee, enter several players (use vm.deal for ETH), deploy a malicious attacker "
        "contract that enters then calls refund() and re-enters in receive(), and ASSERT the contract "
        "was drained beyond the attacker's single entry — so the test PASSES iff the reentrancy is real. "
        "Return ONLY Solidity — no prose, no fences."
    )


def _repair(code: str, err: str) -> str:
    return (
        "Your Foundry PoC failed. Code:\n\n" + code + "\n\nforge output:\n" + err + "\n\n"
        "Fix it so it COMPILES and PASSES iff the reentrancy is genuinely exploitable. "
        "Return ONLY corrected Solidity — no prose, no fences."
    )


def _forge() -> str:
    res = subprocess.run(
        [FORGE, "test", "--root", PROJ, "--match-path", "test/AutoPoC.t.sol", "-vv"],
        capture_output=True, text=True, timeout=180,
    )
    return res.stdout + res.stderr


def main():
    code = _llm(_initial())
    for attempt in range(1, 6):
        with open(os.path.join(PROJ, "test/AutoPoC.t.sol"), "w") as f:
            f.write(code)
        out = _forge()
        if "[PASS]" in out:
            print(f"attempt {attempt} -> 🟢 KEEP: reentrancy CONFIRMED by forge on real PuppyRaffle")
            return
        if "[FAIL" in out:
            print(f"attempt {attempt} -> 🔴 ran but exploit FAILED")
            print("\n".join(out.strip().splitlines()[-6:]))
            return
        err = "\n".join(out.strip().splitlines()[-22:])
        print(f"attempt {attempt} -> compile error, repairing...")
        code = _llm(_repair(code, err))
    print("🔴 no compiling PoC after 5 tries")


if __name__ == "__main__":
    main()
