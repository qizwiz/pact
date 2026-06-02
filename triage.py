"""
triage — run the verified-finder method on a REAL Solidity project.

No synthetic generator. A real, production-shaped contract goes in; Claude proposes
candidate findings on the actual source; forge PROVES or KILLS each by executing an
auto-written PoC. Output is a triage report of forge-verified findings — the kind of
artifact an auditor produces, with execution (not opinion) as the gate.

Honesty: on a famous target (e.g. PuppyRaffle) the proposer is not blind — this tests
the METHOD on real-complexity code and its PRECISION (does forge kill weak proposals?),
not novel discovery. Novel discovery needs an unseen target.

    .venv/bin/python triage.py <project_root> <src_file_rel> [n_findings]
"""

from __future__ import annotations

import json
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
_CLIENT = make_client()
_MODEL = resolve_model()


def _ask(prompt: str, max_tokens: int = 2200) -> str:
    r = _CLIENT.messages.create(
        model=_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text if r.content else ""


def _strip(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
    return re.sub(r"```\s*$", "", s).strip()


def propose(src: str, n: int) -> list[dict]:
    p = (
        f"You are auditing this Solidity contract. Propose up to {n} candidate vulnerabilities — "
        "ONLY ones you can back with a concrete, reachable on-chain exploit. No gas-golf, no style "
        'nits. Return ONLY a JSON array of {"title","where","claim","exploit"}.\n\nCONTRACT:\n'
        + src
    )
    txt = _strip(_ask(p, 1800))
    m = re.search(r"\[.*\]", txt, re.S)
    try:
        out = json.loads(m.group(0) if m else txt)
        return out if isinstance(out, list) else []
    except Exception:
        return []


def _poc_initial(proj: str, src_rel: str, src: str, finding: dict) -> str:
    contract_name = os.path.splitext(os.path.basename(src_rel))[0]
    return (
        "Write a Foundry PoC that CONFIRMS this claimed vulnerability against the real contract. "
        '`import "forge-std/Test.sol";` and `contract AutoPoC is Test`, vm cheatcodes + assertions. '
        f'Import the contract under test from "../{src_rel}". Deploy it realistically (read the '
        "constructor), set up any players/state needed, PERFORM the exploit, and ASSERT the resulting "
        "BAD state so the test PASSES iff the vuln is genuinely exploitable.\n\n"
        f"CONTRACT ({src_rel}, named {contract_name}):\n"
        + src
        + "\n\nCLAIMED VULNERABILITY:\n"
        + json.dumps(finding)
        + "\n\nWrite test/AutoPoC.t.sol. Return ONLY Solidity — no prose, no fences."
    )


def _poc_repair(code: str, err: str) -> str:
    return (
        "Your Foundry PoC failed:\n\n" + err + "\n\nCode:\n" + code + "\n\n"
        "Fix it so it COMPILES and PASSES iff the vulnerability is genuinely exploitable. "
        "Return ONLY corrected Solidity — no prose, no fences."
    )


def _forge(proj: str) -> str:
    res = subprocess.run(
        [FORGE, "test", "--root", proj, "--match-path", "test/AutoPoC.t.sol", "-vv"],
        capture_output=True,
        text=True,
        timeout=240,
    )
    return res.stdout + res.stderr


def gate(proj: str, src_rel: str, src: str, finding: dict, max_tries: int = 4) -> str:
    code = _strip(_ask(_poc_initial(proj, src_rel, src, finding)))
    for _ in range(max_tries):
        with open(os.path.join(proj, "test/AutoPoC.t.sol"), "w") as f:
            f.write(code)
        out = _forge(proj)
        if "[PASS]" in out:
            return "verified"
        if "[FAIL" in out:
            return "killed"  # ran, exploit failed → not real (as claimed)
        err = "\n".join(out.strip().splitlines()[-22:])
        code = _strip(_ask(_poc_repair(code, err)))
    return "unproven"  # never produced a compiling PoC


def main(proj: str, src_rel: str, n: int) -> None:
    src = open(os.path.join(proj, src_rel)).read()
    name = os.path.basename(src_rel)
    print(f"TRIAGE: {name}  finder={_MODEL}  (forge = the only oracle)")
    cands = propose(src, n)
    print(f"proposed {len(cands)} candidate finding(s)\n")
    results = []
    for i, c in enumerate(cands, 1):
        verdict = gate(proj, src_rel, src, c)
        mark = {
            "verified": "🟢 VERIFIED (forge ran the exploit)",
            "killed": "🔴 KILLED   (ran, exploit failed)",
            "unproven": "⚪ UNPROVEN (no compiling PoC in 4 tries)",
        }[verdict]
        print(f"[{i}] {mark}\n     {c.get('title','?')}")
        results.append((c.get("title", "?"), verdict))
    os.path.exists(os.path.join(proj, "test/AutoPoC.t.sol")) and os.remove(
        os.path.join(proj, "test/AutoPoC.t.sol")
    )
    verified = sum(1 for _, v in results if v == "verified")
    print("\n" + "=" * 56)
    print(f"forge-VERIFIED findings: {verified}/{len(results)}")
    print("(verified = a real exploit executed against the real contract)")


if __name__ == "__main__":
    proj = sys.argv[1]
    src_rel = sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    main(proj, src_rel, n)
