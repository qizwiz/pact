"""
measure — the first SYSTEM-LEVEL measurement of the canonical chain (audit.py).

All session we've said "N=1, never measured precision/recall at scale." This closes that on
the labeled data we have: run audit_contract on contracts with KNOWN ground truth (buggy vs
clean) and tally recall + precision. It also tests the intent gate's unmeasured downside — does
it ever reject a REAL bug (a false negative from over-rejection)?

Honest scope: small N, and only 1-2 clean contracts -> precision is weakly estimated. But it's
a real number, not a vibe, and each run is a labeled (contract -> verdict) corpus seed.

    .venv/bin/python measure.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import audit
import recall_loop as R

SP = R.STAKEPOOL
SP_missing_decr = SP.replace("        totalStaked -= amount;\n", "        // BUG: missing decrement\n")
SP_unstake_plus = SP.replace("        staked[msg.sender] -= amount;\n",
                             "        staked[msg.sender] += amount;\n", 1)  # -= -> += in unstake
VAULT = R.VAULT
FLAT = open("/tmp/VulnVault_flat.sol").read() if os.path.exists("/tmp/VulnVault_flat.sol") else None

# (label, contract_name, src, ground_truth)  truth in {"bug","clean"}
CASES = [
    ("StakePool (correct)",        "StakePool", SP,              "clean"),
    ("StakePool (missing decr)",   "StakePool", SP_missing_decr, "bug"),
    ("StakePool (unstake -=>+=)",  "StakePool", SP_unstake_plus, "bug"),
    ("Vault (inflation toy)",      "Vault",     VAULT,           "bug"),
]
if FLAT:
    CASES.append(("VulnVault (real OZ inflation)", "VulnVault", FLAT, "bug"))


def main():
    print("measure: canonical chain (audit.py) on labeled data — first real recall/precision\n")
    tp = fp = fn = tn = 0
    intent_rejects_real = []
    for label, name, src, truth in CASES:
        res = audit.audit_contract(name, src)
        status = res.get("status")
        caught = status == "CAUGHT"
        flagged = "FLAGGED" if caught else status
        print(f"  {label:<32} truth={truth:<5} -> {flagged}")
        if status == "rejected_unintended" and truth == "bug":
            intent_rejects_real.append(label)  # intent gate killed a real bug = bad
        if truth == "bug":
            if caught:
                tp += 1
            else:
                fn += 1
        else:  # clean
            if caught:
                fp += 1
            else:
                tn += 1

    bugs = tp + fn
    clean = fp + tn
    recall = tp / bugs if bugs else float("nan")
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    print("\n" + "=" * 60)
    print(f"  bugs={bugs}  clean={clean}")
    print(f"  TP={tp}  FN={fn}  FP={fp}  TN={tn}")
    print(f"  RECALL    {recall:.2f}  (caught {tp}/{bugs} known bugs)")
    print(f"  PRECISION {precision:.2f}  (of flagged, {tp}/{tp+fp} were real)")
    if intent_rejects_real:
        print(f"  ⚠ intent gate REJECTED real bugs (false negatives): {intent_rejects_real}")
    else:
        print("  intent gate did not reject any real bug (no recall cost observed)")


if __name__ == "__main__":
    main()
