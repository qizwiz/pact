"""
gan — adversarial discovery benchmark for pact's verified-finder.

The honest test the PoC-gate work could NOT answer: can the finder discover a
bug *nobody told it about*? This measures that, with no soft judging in the
critical path — forge is the only oracle.

Three roles, no same-level collusion:

  GENERATOR  (a DIFFERENT model family — openai/gpt-4o via OpenRouter, so it does
             NOT share Claude's blind spots) plants ONE subtle, exploitable bug in
             a realistic contract AND writes a Foundry PoC that PASSES iff the bug
             is real. We then RUN that PoC: if forge doesn't confirm it, the
             challenge is rejected and regenerated. So every planted bug is
             forge-verified real and non-trivial before the finder ever sees it.

  FINDER     (Claude — pact's proposer) sees ONLY the contract source. Blind: never
             sees the generator's reasoning, PoC, or bug summary. It proposes
             candidate vulnerabilities.

  ORACLE     (forge) for each finder candidate, Claude auto-writes a PoC (with a
             compile-repair loop) and forge RUNS it. [PASS] = the finder produced a
             genuinely executable exploit. This is sound — execution, not opinion.

  MATCH      an ISOLATED judge decides whether a forge-verified finder exploit is the
             SAME root cause as the planted bug. This is the one soft step; it only
             distinguishes "found THE planted bug" from "found a different real bug",
             and is reported separately and labelled as soft.

Metrics over N rounds:
  recall    = rounds where the finder forge-verified an exploit / valid rounds
  on_target = rounds where that exploit matched the planted bug (soft judge)
  precision = forge-verified candidates / total candidates proposed

    .venv/bin/python gan.py [N_ROUNDS]
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

GAN = os.path.join(HERE, "examples/solidity/gan")
FORGE = os.path.expanduser("~/.foundry/bin/forge")
GEN_MODEL = (
    "openai/gpt-4o"  # different family from the Claude finder — decorrelated bias
)
FINDER_MODEL = resolve_model()  # anthropic/claude-* by default
_CLIENT = make_client()


# --------------------------------------------------------------------------- #
# llm helpers
# --------------------------------------------------------------------------- #
def _ask(model: str, prompt: str, max_tokens: int = 2600) -> str:
    r = _CLIENT.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text if r.content else ""


def _strip_fence(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
    return re.sub(r"```\s*$", "", s).strip()


def _section(text: str, tag: str) -> str:
    # pull the block after ===TAG=== up to the next ===...=== or EOF
    m = re.search(rf"==={tag}===\s*(.*?)(?=\n===[A-Z]+===|\Z)", text, re.S)
    return _strip_fence(m.group(1)) if m else ""


# --------------------------------------------------------------------------- #
# forge
# --------------------------------------------------------------------------- #
def _write(rel: str, content: str) -> None:
    with open(os.path.join(GAN, rel), "w") as f:
        f.write(content)


def _forge(test_file: str) -> str:
    res = subprocess.run(
        [FORGE, "test", "--root", GAN, "--match-path", f"test/{test_file}", "-vv"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return res.stdout + res.stderr


def _clean_tests() -> None:
    td = os.path.join(GAN, "test")
    for fn in os.listdir(td):
        if fn.endswith(".sol"):
            os.remove(os.path.join(td, fn))


def _normalize_poc(code: str) -> str:
    # Models drift on the Target import path; force the one our scaffold expects.
    code = re.sub(
        r'import\s+(\{[^}]*\}\s+from\s+)?"[^"]*Target\.sol"\s*;',
        'import {Target} from "../src/Target.sol";',
        code,
    )
    return code


# --------------------------------------------------------------------------- #
# GENERATOR  (different model)
# --------------------------------------------------------------------------- #
GEN_PROMPT = (
    "You are designing a smart-contract audit CHALLENGE. Write a realistic Solidity contract "
    "(a vault, token, staking pool, lending market, or auction — real state, real value at stake, "
    "~40-100 lines) that LOOKS correct to a careless reviewer but contains EXACTLY ONE genuine, "
    "exploitable vulnerability. Then write a Foundry PoC that ACTUALLY EXPLOITS it.\n\n"
    "CRITICAL — the bug must be real on Solidity ^0.8.20, where arithmetic is CHECKED (overflow and "
    "underflow REVERT). Do NOT rely on silent overflow/underflow, and do NOT make the test pass via "
    "`vm.expectRevert` — a revert is not an exploit. The PoC must SUCCEED: perform attacker actions, "
    "then ASSERT a BAD end-state (attacker extracted value they shouldn't have, or a core invariant "
    "like solvency / sum(balances)==totalSupply is broken). The test PASSES because the exploit WORKED.\n\n"
    "Pick a REAL bug class, e.g.: reentrancy (state updated AFTER an external call / CEI violation); "
    "broken accounting (wrong variable decremented, shares-vs-assets mismatch, double counting); "
    "first-depositor / donation share-price inflation; rounding/precision loss an attacker farms; "
    "missing or wrong access control on a value-moving function; price/reserve read from a manipulable "
    "source; a wrong comparison/operator/off-by-one in a critical guard; unprotected (re)initializer.\n\n"
    "RIGHT shape (illustration, do NOT copy): a vault whose withdraw() sends ETH BEFORE zeroing the "
    "balance → PoC: attacker re-enters in receive(), withdraws twice, asserts attacker balance > deposit.\n\n"
    "HARD RULES:\n"
    "- Main contract named EXACTLY `Target`, pragma ^0.8.20, compiles standalone (helper/mock/interface "
    "contracts allowed in the same file; include a minimal mock token if you need one).\n"
    '- PoC: `import "forge-std/Test.sol";`, `import {Target} from "../src/Target.sol";`, '
    "`contract PlantedPoC is Test { function test_exploit() public { ... } }`. Use vm cheatcodes "
    "(vm.deal, vm.prank, makeAddr). The single test function must be named `test_exploit`.\n"
    "- EXACTLY ONE exploitable bug; no accidental second bug. The fixed version would simply lack this flaw.\n\n"
    "Respond in EXACTLY this format, nothing else:\n"
    "===CONTRACT===\n<solidity source of Target.sol>\n"
    "===POC===\n<solidity source of the Foundry test>\n"
    "===BUGTYPE===\n<2-5 word class, e.g. 'reentrancy on withdraw'>\n"
    "===SUMMARY===\n<one sentence: where the bug is and why it's exploitable>\n"
)

GEN_REPAIR = (
    "Your challenge failed forge. Output:\n\n{out}\n\n"
    "Fix it. The contract must compile (pragma ^0.8.20, contract named `Target`) and the PoC must "
    "COMPILE and PASS (it passes iff the planted bug is genuinely exploitable). Re-emit ALL four "
    "sections in the same ===CONTRACT===/===POC===/===BUGTYPE===/===SUMMARY=== format, nothing else."
)


def generate_challenge(max_tries: int = 3) -> dict | None:
    raw = _ask(GEN_MODEL, GEN_PROMPT, 4000)
    for attempt in range(1, max_tries + 1):
        contract = _section(raw, "CONTRACT")
        poc = _section(raw, "POC")
        bugtype = _section(raw, "BUGTYPE")
        summary = _section(raw, "SUMMARY")
        if not (contract and poc):
            raw = _ask(
                GEN_MODEL, GEN_REPAIR.format(out="(could not parse 4 sections)"), 4000
            )
            continue
        _clean_tests()
        _write("src/Target.sol", contract)
        _write("test/Planted.t.sol", _normalize_poc(poc))
        out = _forge("Planted.t.sol")
        if "[PASS]" in out:
            print(f"  generator: planted bug VALID (forge-verified) [{bugtype}]")
            return {
                "contract": contract,
                "poc": poc,
                "bugtype": bugtype,
                "summary": summary,
            }
        tail = "\n".join(out.strip().splitlines()[-22:])
        print(f"  generator: attempt {attempt} not forge-verified, repairing...")
        raw = _ask(GEN_MODEL, GEN_REPAIR.format(out=tail), 4000)
    print("  generator: FAILED to produce a forge-verified challenge")
    return None


# --------------------------------------------------------------------------- #
# FINDER  (Claude, blind — sees only the contract)
# --------------------------------------------------------------------------- #
def finder_propose(contract: str, n: int = 3) -> list[dict]:
    p = (
        f"Audit this Solidity contract. Propose up to {n} candidate vulnerabilities — ONLY ones you "
        "can back with a concrete, reachable exploit. No style nits, no speculation. Return ONLY a "
        'JSON array of objects {"title","where","claim","exploit"}.\n\nCONTRACT (src/Target.sol):\n'
        + contract
    )
    txt = _strip_fence(_ask(FINDER_MODEL, p, 1600))
    m = re.search(r"\[.*\]", txt, re.S)
    try:
        out = json.loads(m.group(0) if m else txt)
        return out if isinstance(out, list) else []
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# ORACLE  (forge) — Claude auto-writes a PoC for the finder's candidate
# --------------------------------------------------------------------------- #
def _poc_initial(contract: str, finding: dict) -> str:
    return (
        "Write a Foundry PoC that CONFIRMS this claimed vulnerability against the contract. "
        '`import "forge-std/Test.sol";`, `import {Target} from "../src/Target.sol";`, '
        "`contract FinderPoC is Test { function test_exploit() public { ... } }`. PERFORM the exploit "
        "and ASSERT the resulting bad state so the test PASSES iff the bug is genuinely exploitable.\n\n"
        "CONTRACT (src/Target.sol):\n"
        + contract
        + "\n\nCLAIMED VULNERABILITY:\n"
        + json.dumps(finding)
        + "\n\nReturn ONLY Solidity — no prose, no fences."
    )


def _poc_repair(code: str, err: str) -> str:
    return (
        "Your Foundry PoC failed:\n\n" + err + "\n\nCode:\n" + code + "\n\n"
        "Fix it so it COMPILES and PASSES iff the bug is genuinely exploitable. "
        "Return ONLY corrected Solidity — no prose, no fences."
    )


def finder_gate(contract: str, finding: dict, max_tries: int = 4) -> bool:
    code = _strip_fence(_ask(FINDER_MODEL, _poc_initial(contract, finding)))
    for _ in range(max_tries):
        _write("src/Target.sol", contract)
        _write("test/FinderPoC.t.sol", _normalize_poc(code))
        out = _forge("FinderPoC.t.sol")
        if "[PASS]" in out:
            return True
        if "[FAIL" in out:
            return False  # ran but exploit failed → not real
        err = "\n".join(out.strip().splitlines()[-20:])
        code = _strip_fence(_ask(FINDER_MODEL, _poc_repair(code, err)))
    return False


# --------------------------------------------------------------------------- #
# MATCH  (isolated soft judge — only "same bug?" not "is it real")
# --------------------------------------------------------------------------- #
def judge_match(planted_summary: str, finding: dict) -> bool:
    p = (
        "Two security findings about the same contract. Are they the SAME underlying bug (same root "
        'cause / same vulnerable code path)? Answer ONLY JSON {"same": true|false}.\n\n'
        "PLANTED BUG:\n"
        + planted_summary
        + "\n\nFINDER CLAIM:\n"
        + json.dumps({k: finding.get(k) for k in ("title", "where", "claim")})
    )
    try:
        m = re.search(r"\{.*\}", _strip_fence(_ask(FINDER_MODEL, p, 200)), re.S)
        return bool(json.loads(m.group(0)).get("same")) if m else False
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# round / main
# --------------------------------------------------------------------------- #
def run_round(i: int) -> dict | None:
    print(f"\n=== round {i} ===")
    ch = generate_challenge()
    if ch is None:
        return None
    cands = finder_propose(ch["contract"])
    print(f"  finder: proposed {len(cands)} candidate(s) (blind)")
    verified = 0
    on_target = False
    for c in cands:
        ok = finder_gate(ch["contract"], c)
        verified += ok
        tag = "🟢 forge-VERIFIED" if ok else "🔴 unverified   "
        hit = ""
        if ok and judge_match(ch["summary"], c):
            on_target = True
            hit = "  [matches planted bug]"
        print(f"    {tag}  {c.get('title','?')}{hit}")
    return {
        "candidates": len(cands),
        "verified": verified,
        "found": verified > 0,
        "on_target": on_target,
        "planted": ch["bugtype"],
    }


def main(n_rounds: int) -> None:
    print(f"GAN: generator={GEN_MODEL}  finder={FINDER_MODEL}  rounds={n_rounds}")
    rounds = [r for i in range(1, n_rounds + 1) if (r := run_round(i)) is not None]
    if not rounds:
        print("\nno valid rounds (generator never cleared forge)")
        return
    valid = len(rounds)
    found = sum(r["found"] for r in rounds)
    on_target = sum(r["on_target"] for r in rounds)
    cands = sum(r["candidates"] for r in rounds)
    verified = sum(r["verified"] for r in rounds)
    print("\n" + "=" * 56)
    print(f"valid rounds (planted bug forge-verified): {valid}/{n_rounds}")
    print(f"recall   (finder forge-verified an exploit): {found}/{valid}")
    print(f"on-target (matched planted bug, soft judge): {on_target}/{valid}")
    pr = f"{verified}/{cands}" if cands else "0/0"
    print(f"precision (verified / proposed candidates):  {pr}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    main(n)
