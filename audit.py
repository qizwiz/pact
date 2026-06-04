"""
audit — THE canonical Solidity invariant-audit chain. Consolidates the willy-nilly (three
overlapping proposers: invariant_agent, solidity_intent, broad_backtest) into ONE documented
path within the PROVEN reach (OpenZeppelin-style vaults/tokens). The variance tail (Solmate /
concrete token types / oracles) is the LABELED fallback (broad_backtest, meta_template) — NOT
this chain.

Roles, explicit and each grounded by a verifier:
  PROPOSE + SLOT-FILL  harness_grammar.propose_spec   (file-backed sol_harness_spec.md)
  INTENT GATE          intended(statement)            (file-backed sol_invariant_intent.md)
        ^ the PRECISION floor. The old skeptic was MIS-CAST: it judges current VALIDITY (= Halmos's
          sound job) so it would reject violated-but-intended invariants = real bugs. The right
          gate judges INTENT — keep "no-zero-share" (intended, violated = bug), reject
          "self-collateralized rewards" (never promised = false positive).
  EMIT                 harness_grammar.emit_harness   (correct-by-construction actor model)
  VERIFY               build + revert-vacuity teeth + Halmos (sound oracle)
  REPAIR               file-backed sol_harness_repair.md (compile / revert)

Context policy (deliberate, not accidental): every role here is STATELESS / one-shot — each is a
self-contained judgment the verifier checks. No retained context is needed; retention would only
matter for a cross-contract memory of past harnesses, which is a corpus/retrieval concern, later.

    .venv/bin/python audit.py
"""

from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness_grammar as hg
import invariant_agent as agent
import prompt_improve as pi
from halmos_check import run_halmos


def intended(name: str, src: str, statement: str) -> tuple[bool, str]:
    """PRECISION gate: is this a property the contract is SUPPOSED to guarantee? (intent, not
    current validity). Soft judgment — the irreducible semantic floor — but judging the RIGHT
    thing, unlike the validity-judging skeptic."""
    if not statement:
        return True, "no statement to judge"
    txt = agent._ask(
        pi.render(pi.load_prompt("sol_invariant_intent"), name=name, src=src, statement=statement),
        300,
    )
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        d = json.loads(m.group(0))
        return bool(d.get("intended", True)), d.get("reason", "")
    except Exception:
        return True, "intent parse fail -> default keep"


def _repair(harness: str, signal: str, kind: str) -> str:
    return agent._ask(
        pi.render(pi.load_prompt("sol_harness_repair"), kind=kind, signal=signal, harness=harness),
        3000,
    )


def audit_contract(name: str, src: str, max_repair: int = 4) -> dict:
    spec = hg.propose_spec(name, src)
    if not spec:
        return {"status": "no_spec"}
    stmt = spec.get("statement", "")
    ok, reason = intended(name, src, stmt)
    if not ok:
        return {"status": "rejected_unintended", "statement": stmt, "reason": reason}

    harness = hg.emit_harness(spec)
    for _ in range(max_repair):
        agent._setup_project(name, src)
        built, out = agent._build(harness)
        if not built:
            harness = _repair(harness, "\n".join(out.splitlines()[-20:]), "compile")
            continue
        verdicts = run_halmos(agent.PROJECT)
        if not verdicts:  # built but no pass/fail = revert-vacuity
            harness = _repair(harness, "every symbolic path reverted", "revert")
            continue
        caught = any(not v["proved"] for v in verdicts)
        return {"status": "CAUGHT" if caught else "PROVED", "statement": stmt, "verdicts": verdicts}
    return {"status": "exhausted", "statement": stmt}


def main():
    print("audit: canonical chain (propose -> intent gate -> emit -> verify -> repair)\n")
    # 1) end-to-end on the proven reach: should pass intent, build, and CATCH the inflation
    flat = open("/tmp/VulnVault_flat.sol").read()
    res = audit_contract("VulnVault", flat)
    print(f"[end-to-end] VulnVault -> {res.get('status')}   inv: \"{res.get('statement')}\"")
    for v in res.get("verdicts", []):
        print("    ", v.get("function"), "proved=" + str(v.get("proved")))
    print()
    # 2) intent-gate precision unit test: keep the real, reject the false positive
    print("[intent gate] precision unit test:")
    a = intended("VulnVault", flat, "a positive deposit must mint more than zero shares")
    print(f"    no-zero-share (vault)          -> intended={a[0]}  ({a[1]})   want True")
    dvs = open("/tmp/DVS_flat.sol").read() if os.path.exists("/tmp/DVS_flat.sol") else flat
    b = intended("DamnValuableStaking", dvs,
                 "the contract always holds enough DVT to cover all staked amounts plus accrued rewards")
    print(f"    self-collateralized rewards    -> intended={b[0]}  ({b[1]})   want False")


if __name__ == "__main__":
    main()
