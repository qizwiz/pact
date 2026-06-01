"""
poc_gate — hands-off PoC gate: forge-std + compile-error repair loop.

The LLM writes a Foundry PoC (forge-std allowed — its native idiom) for a claimed
finding; forge RUNS it. If it doesn't compile, the forge error is fed back and the
LLM repairs, up to N tries. Verdicts:
  [PASS]              -> KEEP   (exploit actually executed)
  [FAIL...]           -> KILL   (compiled + ran, but the exploit failed = bug not real)
  no compile after N  -> KILL   (couldn't produce valid code)

No human hand on the PoC. (Residual: bug+contract still author-made — full un-rig
is the generator/contest stage.)
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

POC = os.path.join(HERE, "examples/solidity/poc")
FORGE = os.path.expanduser("~/.foundry/bin/forge")
_CLIENT = make_client()
_MODEL = resolve_model()
YIELDBANK = open(os.path.join(POC, "src/YieldBank.sol")).read()


def _llm(prompt: str) -> str:
    r = _CLIENT.messages.create(model=_MODEL, max_tokens=1700, messages=[{"role": "user", "content": prompt}])
    code = r.content[0].text if r.content else ""
    code = re.sub(r"^```[a-zA-Z]*\n?", "", code.strip())
    return re.sub(r"```\s*$", "", code).strip()


def _initial(finding: str) -> str:
    return (
        "Write a Foundry PoC to CONFIRM OR REFUTE a claimed vulnerability. You MAY use forge-std: "
        '`import "forge-std/Test.sol";` and `contract AutoPoC is Test { ... }`, with vm cheatcodes '
        "and assertions.\n\n"
        "Contract under test (src/YieldBank.sol):\n" + YIELDBANK + "\n"
        "A mock token exists at src/MockERC20.sol: mint(to,amt), approve(spender,amt), transfer, "
        "transferFrom, balanceOf. Construct the bank with `new YieldBank(IERC20(address(token)))`.\n\n"
        "CLAIMED VULNERABILITY:\n" + finding + "\n\n"
        'Write test/AutoPoC.t.sol importing {YieldBank, IERC20} from "../src/YieldBank.sol" and '
        'MockERC20 from "../src/MockERC20.sol". In a test function, ACTUALLY PERFORM the exploit, then '
        "assert the resulting BAD state so the test PASSES iff the vuln is genuinely exploitable and "
        "FAILS otherwise. No trivially-passing tests. Return ONLY Solidity — no prose, no fences."
    )


def _repair(code: str, err: str) -> str:
    return (
        "Your Foundry PoC failed. Here is the code:\n\n" + code + "\n\nforge output:\n" + err + "\n\n"
        "Fix it so it COMPILES and the test PASSES iff the vulnerability is genuinely real (FAILS if "
        "not). Return ONLY the corrected Solidity — no prose, no fences."
    )


def _forge() -> str:
    res = subprocess.run(
        [FORGE, "test", "--root", POC, "--match-path", "test/AutoPoC.t.sol", "-vv"],
        capture_output=True, text=True, timeout=150,
    )
    return res.stdout + res.stderr


def run_gate(label: str, finding: str, max_tries: int = 4) -> bool:
    code = _llm(_initial(finding))
    for attempt in range(1, max_tries + 1):
        with open(os.path.join(POC, "test/AutoPoC.t.sol"), "w") as f:
            f.write(code)
        out = _forge()
        if "[PASS]" in out:
            print(f"{label}: attempt {attempt} -> 🟢 KEEP (exploit executed)")
            return True
        if "[FAIL" in out:
            print(f"{label}: attempt {attempt} -> 🔴 KILL (ran, exploit FAILED — bug not real)")
            return False
        err = "\n".join(out.strip().splitlines()[-22:])
        print(f"{label}: attempt {attempt} -> compile error, repairing...")
        code = _llm(_repair(code, err))
    print(f"{label}: 🔴 KILL (no compiling PoC after {max_tries} tries)")
    return False


if __name__ == "__main__":
    ex = os.path.join(POC, "test/Exploit.t.sol")
    if os.path.exists(ex):
        os.remove(ex)
    run_gate(
        "FINDING A (real: withdraw accounting)",
        "withdraw() decrements totalAssets by shareAmount (the share count) instead of the asset "
        "payout. After addYield, payout > shareAmount, so totalAssets stays OVERSTATED above the "
        "contract's real token balance — the vault is left insolvent.",
    )
    run_gate(
        "FINDING B (suspected-false: donation inflation)",
        "A first depositor inflates share price via a donation attack: deposit 1 wei, then transfer "
        "tokens directly to the contract to inflate value-per-share, so a later depositor receives 0 "
        "shares and loses funds.",
    )
