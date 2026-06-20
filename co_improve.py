"""
co_improve — the STRUCTURE for self-improvement (not hand-patching). The gate AND the body are
prompts; both self-improve on GROUNDED, CORRECTLY-ATTRIBUTED signals derived from measured truth.

The misattribution trap (last night): a hand-written transcript blamed the wrong layer. Fixed
structurally here — we VERIFY each invariant on (clean, buggy) REGARDLESS of the gate's opinion,
then route the signal by the confusion between gate-decision and measured-discrimination:

  gate REJECT + actually discriminates  -> gate over-rejected   -> improve GATE  (sol_invariant_intent)
  gate KEEP   + proves on both          -> body weak / mismatch  -> improve BODY  (sol_body_fill)
  gate KEEP   + fails on clean (FP)      -> gate let an FP through -> improve GATE

Each component is scored 0 only when IT erred (correct attribution) -> improve_if_weak fires on the
right prompt. No human intervention: the structure fixes its own gate and body. Whether it CONVERGES
is the measured question (the laws still apply: semantics-only, signal must be discoverable).

    .venv/bin/python co_improve.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import invariant_agent as agent
from plumbline import prompt_improve as pi
import meta_template as mt
import hybrid_audit as H
import audit
import body_catch_loop as B   # reuse make_bug + run_one

DVD = B.DVD
ROUNDS = 4

# reset the body prompt to a NEUTRAL, dimension-diverse baseline so the run starts from a known
# state (not the confused trained one). The gate stays as committed (the over-rejecting one we
# want the STRUCTURE to fix on its own).
BODY_BASELINE = """A Halmos harness is built: `c` is deployed, address(this) is funded with the asset
and approved. Asset token(s): {{assets}}. Symbolic args: uint256 a, uint256 b.

The harness provides NO cheatcodes — `vm` is NOT available. Use require() only; do NOT call vm.warp,
vm.prank, or any cheatcode (the test will not compile).

Write the BODY of check_inv(uint256 a, uint256 b): bound args with require(); establish a non-trivial
pre-state by calling c's functions as address(this); assert ONE conservation/solvency invariant that
HOLDS on a correct contract but is VIOLATED if it mis-accounts. The assert MUST faithfully encode your
STATEMENT. Consider relating different quantities (held tokens vs issued shares, withdrawable vs
deposited, no-zero-share, conservation). Minimal valid Solidity, require() only.

Return exactly:
STATEMENT: <one-line>
BODY:
<solidity statements only>

CONTRACT ({{name}}):
{{src}}
"""


def round_eval(clean_src):
    params = mt.extract_ctor("DamnValuableStaking", clean_src)
    _pre, assets = H.build_scaffold("DamnValuableStaking", "../src/DamnValuableStaking.sol", params)
    stmt, body = H.fill_body("DamnValuableStaking", clean_src, assets)
    if not body:
        return None
    gate_keep, gate_reason = audit.intended("DamnValuableStaking", clean_src, stmt)
    clean_v = B.run_one("DamnValuableStaking", "src/DamnValuableStaking.sol", params, body)
    buggy_v = B.run_one("DamnValuableStakingBug", "src/DamnValuableStakingBug.sol", params, body)
    discriminates = (clean_v == "PROVED" and buggy_v == "CAUGHT")
    fp_on_clean = (clean_v == "CAUGHT")
    # THIRD routing arm: BUILD_FAIL is STRUCTURAL -> the macro's job, NOT a prompt-improvement
    # target. Self-improvement fixes semantics, not structure; do not misattribute it to gate/body.
    structural = ("BUILD_FAIL" in (clean_v, buggy_v))
    gate_sig = body_sig = None
    if structural:
        return dict(stmt=stmt, gate_keep=gate_keep, clean=clean_v, buggy=buggy_v,
                    discriminates=False, gate_sig=None, body_sig=None, structural=True)
    if discriminates and not gate_keep:
        gate_sig = (f"You REJECTED '{stmt[:60]}' — but it PROVES on the correct contract and CATCHES a "
                    "real bug. It is a legitimate principal/conservation self-property, NOT "
                    "reward-dependent. Stop rejecting principal-conservation invariants; reject only "
                    "claims that genuinely depend on EXTERNAL funding the contract doesn't control.")
    # MEASURED body-properties -> correct attribution (grounded, not hand-diagnosed per round).
    # The verdict alone (proves-both) is too thin to localize the body's error; these deterministic
    # checks on the emitted body name the masking causes so the grounded signal pinpoints the layer.
    donates = ("transfer(address(c)" in body) or ("address(c)," in body and ".transfer" in body)
    reward_dep = any(k in body for k in ("earned(", "reward", "pending", "Reward"))
    proves_both = (clean_v == "PROVED" and buggy_v == "PROVED")
    if proves_both and not gate_keep and reward_dep:
        # gate REJECTED correctly (reward-dependent) AND it wouldn't have caught anyway -> fix BODY,
        # not the gate. The gate is doing its job; the body keeps adding reward terms.
        body_sig = (f"Your invariant '{stmt[:55]}' was REJECTED because it depends on REWARDS (you "
                    "referenced earned/reward terms) AND it would not catch the bug. Assert PURE "
                    "PRINCIPAL only: relate the underlying tokens the contract HOLDS to the SHARES it "
                    "ISSUED (totalSupply) — drop every earned/reward/pending term entirely.")
    elif gate_keep and not discriminates and not fp_on_clean:
        why = ""
        if donates:
            why = (" You TRANSFERRED extra tokens to the contract before asserting — that pre-funding "
                   "MASKS a share-issuance bug (the padded balance covers inflated shares). Remove all "
                   "donations; stake ONCE and assert immediately after.")
        if reward_dep:
            why += " Drop the earned/reward terms — assert pure principal (balance vs totalSupply)."
        body_sig = (f"Your invariant '{stmt[:55]}' passed the gate but PROVED on the BUGGY contract too "
                    f"(clean={clean_v}, buggy={buggy_v}) — it did NOT catch the violation.{why} Relate the "
                    "tokens HELD to the SHARES ISSUED (totalSupply), and make the ASSERT match the STATEMENT.")
    if gate_keep and fp_on_clean:
        gate_sig = (f"You KEPT '{stmt[:60]}' but it is VIOLATED on the CORRECT contract = a false "
                    "positive. Reject invariants that don't actually hold on correct behavior.")
    # catch-all: ANY proves-both round is a BODY recall failure (it didn't catch). Always teach the
    # body, with whatever masking cause we MEASURED, so no proves-both round is wasted.
    if proves_both and not body_sig:
        why = ""
        if donates:
            why += (" You pre-funded the contract (transfer to address(c)); that MASKS share-issuance "
                    "bugs — remove donations, stake once, assert immediately.")
        if reward_dep:
            why += " Drop earned/reward/pending terms — assert PURE principal."
        body_sig = (f"Your invariant '{stmt[:55]}' PROVED on the BUGGY contract — it did NOT catch the "
                    f"violation.{why} Relate tokens HELD to SHARES ISSUED (totalSupply); ASSERT must "
                    "match STATEMENT.")
    return dict(stmt=stmt, gate_keep=gate_keep, clean=clean_v, buggy=buggy_v,
                discriminates=discriminates, gate_sig=gate_sig, body_sig=body_sig)


def main():
    print("co_improve: the STRUCTURE — gate AND body self-improve on measured-truth attribution\n")
    pi.save_prompt("sol_body_fill", BODY_BASELINE)  # known start; gate left as committed
    B.make_bug()
    clean_src = open(DVD + "/src/DamnValuableStaking.sol").read()
    try:
        history = []
        for r in range(1, ROUNDS + 1):
            print(f"────────── round {r} ──────────")
            res = round_eval(clean_src)
            if res is None:
                print("  no body; skip"); history.append(0); continue
            print(f"  gate={'KEEP' if res['gate_keep'] else 'REJECT'}  clean={res['clean']}  "
                  f"buggy={res['buggy']}  -> {'DISCRIMINATES' if res['discriminates'] else 'no'}")
            print(f"    inv: {res['stmt'][:70]}")
            history.append(1 if res["discriminates"] else 0)
            if res["discriminates"]:
                print("  ✓ proves clean AND catches bug — discrimination achieved.")
                break
            if res.get("structural"):
                print("    -> STRUCTURAL (BUILD_FAIL): the macro's job, NOT a prompt. "
                      "Not routing to gate/body self-improvement.")
                print()
                continue
            # ROUTE the grounded signal to the component that erred (correct attribution)
            if res["gate_sig"]:
                print("    -> improving GATE (it mis-judged)")
                pi.improve_if_weak("sol_invariant_intent", 0.0, res["gate_sig"], agent._ask)
            if res["body_sig"]:
                print("    -> improving BODY (assert too weak / mismatched)")
                pi.improve_if_weak("sol_body_fill", 0.0, res["body_sig"], agent._ask)
            print()
        print("\n================ discrimination trajectory ================")
        print("  " + "  ->  ".join(str(x) for x in history))
        if history and history[-1] == 1 and history[0] == 0:
            print("  the STRUCTURE self-corrected its own gate+body to discriminate. ✓ (no hand-patch)")
        elif history and history[0] == 1:
            print("  discriminated on round 1.")
        else:
            print("  no convergence in budget — honest. (which component kept erring? see rounds)")
    finally:
        if os.path.exists(DVD + "/src/DamnValuableStakingBug.sol"):
            os.remove(DVD + "/src/DamnValuableStakingBug.sol")


if __name__ == "__main__":
    main()
