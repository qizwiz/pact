"""
pact_check — the wieldable entry point: point it at ANY contract + (optional) invariant hypothesis
and get a fast, SOUND verdict. This is the autonomous gated engine generalized off the UnstoppableVault
fixture, so an auditor can actually use it in a contest.

Flow (all gated):
  1. PLAN          : adaptive planner reads the contract, handles deploy variance (constructor /
                     initializer / ERC1967 proxy), proposes the attack scenario + safety invariant.
  2. VERIFY (raw)  : forge build --ast (+ LLM repair) then halmos on the real contract.
  3. CLASSIFY      : CAUGHT / PROVED / EXHAUSTED / SKIPPED / BUILD_FAIL.
  4. RESOLVE       : CAUGHT -> concrete-replay-validate (a finding counts ONLY if the cex makes the
                     invariant assert revert). EXHAUSTED on an ERC4626-convert shape -> auto-apply the
                     Lean-gated bounded summary and re-verify. Anything needing a new trust primitive
                     ESCALATES (never auto-blessed).
  CATCHING needs only raw-cex + replay (sound even on nonlinear); the summary is for the PROVING side.

  .venv/bin/python pact_check.py <project_root> <contract_rel> <ContractName> ["context notes"]
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from plumbline import adaptive_harness as A
import summarize
from roadblock_dispatch import classify, log_case


def _erc4626_convert_shaped(src: str) -> bool:
    return ("convertToShares" in src or "convertToAssets" in src) and "mulDiv" in src.replace(" ", "")


def check(project_root: str, contract_rel: str, name: str, context: str = "") -> dict:
    src = open(os.path.join(project_root, contract_rel)).read()
    print(f"pact_check: {name}  ({contract_rel})\n", flush=True)
    p = A.plan(name, src, context)
    if not p:
        return {"verdict": "no_plan"}
    print(f"  plan: deploy={p.get('deploy_kind')}  invariant={p.get('invariant_statement')}", flush=True)
    import_path = "../" + contract_rel

    # STAGE 1: raw verify
    harness = A.emit(p, name, name, import_path)
    v1, out1, built1 = A.verify(project_root, harness)
    rb1 = classify(v1, out1)
    if rb1 == "CAUGHT":
        sound, _ = A.validate_replay(project_root, built1, p, A.extract_cex(out1))
        verdict = "FINDING (replay-validated)" if sound else "spurious cex -> needs summary/escalate"
        print(f"  raw -> CAUGHT, replay sound={sound}", flush=True)
        if sound:
            log_case("CAUGHT", out1, "raw-cex + replay", verdict, True)
            return {"verdict": "FINDING", "statement": p.get("invariant_statement"), "sound": True}

    # STAGE 2: nonlinear roadblock -> auto-apply Lean-gated summary IF the shape matches; else report
    if rb1 in ("EXHAUSTED", "SKIPPED") or (rb1 == "CAUGHT"):
        if not _erc4626_convert_shaped(src):
            print(f"  raw -> {rb1}; not an ERC4626-convert shape -> no admitted summary applies -> ESCALATE")
            log_case(rb1, out1, "none (no admitted summary for shape)", "escalate", False)
            return {"verdict": rb1, "escalate": True}
        if summarize.gate()[0] != "admitted":
            return {"verdict": rb1, "escalate": True, "reason": "summary obligation not Lean-discharged"}
        print(f"  raw -> {rb1}; applying Lean-gated ERC4626 summary", flush=True)
        target, subsrc = A.summarized_subclass(
            name, "./" + os.path.basename(contract_rel),
            "ERC20 _t, address _o, address _f", "_t, _o, _f")
        subpath = os.path.join(project_root, os.path.dirname(contract_rel), target + ".sol")
        open(subpath, "w").write(subsrc)
        try:
            sub_import = import_path.replace(name + ".sol", target + ".sol")
            v2, out2, built2 = A.verify(project_root, A.emit(p, name, target, sub_import))
            rb2 = classify(v2, out2)
            if rb2 == "CAUGHT":
                sound, _ = A.validate_replay(project_root, built2, p, A.extract_cex(out2))
                verdict = "FINDING (summarized, replay-validated)" if sound else "spurious"
                log_case(f"{rb1}->summary->CAUGHT", out2, "Lean-summary + replay", verdict, sound)
                return {"verdict": "FINDING" if sound else "spurious", "sound": sound}
            log_case(f"{rb1}->summary->{rb2}", out2, "Lean-summary", v2, v2 == "PROVED")
            return {"verdict": rb2}
        finally:
            if os.path.exists(subpath):
                os.remove(subpath)

    log_case(rb1, out1, "n/a", v1, v1 == "PROVED")
    return {"verdict": rb1}


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(0)
    root, rel, nm = sys.argv[1], sys.argv[2], sys.argv[3]
    ctx = sys.argv[4] if len(sys.argv) > 4 else ""
    res = check(root, rel, nm, ctx)
    print(f"\n=> {res}")
