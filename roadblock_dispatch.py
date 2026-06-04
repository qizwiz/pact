"""
roadblock_dispatch — make the AI work the roadblocks: detect a roadblock, route it to the GATED
workflow that dissolves it, apply only if the gate confirms soundness, loop. Autonomous.

This is JH's self-improvement-structure principle at the META level: the system fixes its own
roadblocks; humans build the scaffold and OWN the trust kernel. The inviolable line — auto-adapt
SEMANTICS, never auto-bless SOUNDNESS: a novel roadblock needing a new trust primitive ESCALATES
(human/Lean), it is not auto-accepted.

Every dispatch logs a grounded CASE (roadblock signal -> workflow -> gate outcome) to a case library.
That log is the seed for a future LEARNING layer (retrieve-by-similarity / structured policy over
roadblock->workflow), grounded on the GATE'S verdict — not surface similarity.

  .venv/bin/python roadblock_dispatch.py        # autonomous dispatch on UnstoppableVault
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import adaptive_harness as A
import summarize
import invariant_agent as agent
import prompt_improve as pi

CASE_LIB = os.path.join(HERE, "states", "roadblock_cases.jsonl")


def classify(verdict: str, out: str) -> str:
    if "Skipped" in out and "[PASS]" not in out and "[FAIL]" not in out:
        return "SKIPPED"
    return {"BUILD_FAIL": "BUILD_FAIL", "exhausted": "EXHAUSTED",
            "CAUGHT": "CAUGHT", "PROVED": "PROVED"}.get(verdict, "NOVEL")


def log_case(rb: str, signal: str, workflow: str, gate_outcome: str, sound: bool):
    os.makedirs(os.path.dirname(CASE_LIB), exist_ok=True)
    rec = {"roadblock": rb, "signal": signal[:400], "workflow": workflow,
           "gate_outcome": gate_outcome, "sound": sound}
    with open(CASE_LIB, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def novel_workflow(signal: str, context: str) -> dict:
    """Roadblock with no known handler -> propose a GATED workflow. Respects the trust boundary:
    a fix needing a new trust primitive is flagged escalate=true (human/Lean), never auto-applied."""
    prompt = pi.render(open(os.path.join(HERE, "prompts/sol_roadblock_workflow.md")).read(),
                       signal=signal, context=context)
    txt = agent._ask(prompt, 1500)
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


# known roadblock -> gated handler. Each returns (gate_outcome:str, sound:bool, workflow:str).
def handle_caught(root, out, p, built):
    cex = A.extract_cex(out)
    sound, _ = A.validate_replay(root, built, p, cex)
    return ("REAL EXPLOIT (replay-validated)" if sound else "spurious cex (rejected)"), sound, \
        "bounded-summary + concrete-replay-validation"


def dispatch_unstoppable():
    DVD = "/Users/jonathanhill/src/damn-vulnerable-defi"
    name, rel = "UnstoppableVault", "src/unstoppable/UnstoppableVault.sol"
    src = open(os.path.join(DVD, rel)).read()
    context = ("DVD, solmate ERC4626, canonical ERC20 = DamnValuableToken (mints uint256.max), "
               "totalAssets()=asset.balanceOf(this), ctor (ERC20,address,address).")
    print("roadblock_dispatch: autonomous detect -> gated workflow -> log case\n")
    p = A.plan(name, src, context)
    target, subsrc = A.summarized_subclass(
        name, "./UnstoppableVault.sol",
        "ERC20 _token, address _owner, address _feeRecipient", "_token, _owner, _feeRecipient")
    subpath = os.path.join(DVD, "src/unstoppable", target + ".sol")
    open(subpath, "w").write(subsrc)
    try:
        harness = A.emit(p, name, target, "../src/unstoppable/" + target + ".sol")
        verdict, out, built = A.verify(DVD, harness)
        rb = classify(verdict, out)
        print(f"  roadblock detected: {rb}  (verify={verdict})", flush=True)
        if rb == "CAUGHT":
            outcome, sound, wf = handle_caught(DVD, out, p, built)
        elif rb == "EXHAUSTED":
            outcome, sound, wf = "nonlinear -> route to summary gate (summarize.gate)", \
                summarize.gate()[0] == "admitted", "Lean-gated bounded summary"
        elif rb == "NOVEL":
            nw = novel_workflow(out, context)
            outcome = f"proposed '{nw.get('roadblock_class')}' ({'ESCALATE->human/Lean' if nw.get('escalate') else 'semantic'})"
            sound = not nw.get("escalate", True)  # semantic fixes can be auto-gated; trust primitives escalate
            wf = nw.get("action", "novel")
        else:
            outcome, sound, wf = f"verdict={verdict}", verdict == "PROVED", "n/a"
        print(f"  gated workflow -> {wf}\n  outcome -> {outcome}  (sound={sound})", flush=True)
        rec = log_case(rb, signal=out, workflow=wf, gate_outcome=outcome, sound=sound)
        print(f"  case logged -> {CASE_LIB}  (seed for the learning layer)")
        return rec
    finally:
        if os.path.exists(subpath):
            os.remove(subpath)


if __name__ == "__main__":
    dispatch_unstoppable()
