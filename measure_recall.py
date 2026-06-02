"""
measure_recall — does the AI-proposer + Halmos hold beyond the obvious toy?

Runs invariant_agent (AI proposes invariants -> Halmos discharges) over a panel of
REALISTIC, multi-function, un-commented 0.8.x contracts with real invariant-class bugs,
plus a CORRECT control. Measures:
  recall    = buggy contracts where a VIOLATED invariant surfaced (with EVM counterexample)
  precision = correct contracts that stayed all-PROVED (no false alarm)

HONEST SCOPE: invariant-EXPRESSIBLE bug classes only (conservation/solvency/accounting/
overflow) — NOT reentrancy/oracle/access. Contracts are author-written-but-realistic
(not yet fresh wild contest code). Halmos-tractable (no heavy loops/external calls). This
is the step from "obvious toy" toward real recall, not the final word.

    .venv/bin/python measure_recall.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import invariant_agent as agent  # noqa: E402

PANEL = [
    {
        "name": "Vault",
        "has_bug": True,
        "note": "first-depositor/donation share inflation -> later deposit rounds to 0 shares",
        "src": """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
contract Vault {
    mapping(address => uint256) public shares;
    uint256 public totalShares;
    uint256 public totalAssets;
    function deposit(uint256 assets) external returns (uint256 minted) {
        if (totalShares == 0) { minted = assets; }
        else { minted = assets * totalShares / totalAssets; }
        shares[msg.sender] += minted;
        totalShares += minted;
        totalAssets += assets;
    }
    function donate(uint256 assets) external { totalAssets += assets; }
}
""",
    },
    {
        "name": "LendingPool",
        "has_bug": True,
        "note": "repay under-reduces totalDebt (amount/2) -> totalDebt drifts above sum(debt)",
        "src": """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
contract LendingPool {
    mapping(address => uint256) public debt;
    uint256 public totalDebt;
    function borrow(uint256 amount) external {
        debt[msg.sender] += amount;
        totalDebt += amount;
    }
    function repay(uint256 amount) external {
        require(debt[msg.sender] >= amount, "too much");
        debt[msg.sender] -= amount;
        totalDebt -= amount / 2;
    }
}
""",
    },
    {
        "name": "FeeToken",
        "has_bug": True,
        "note": "transfer debits full amount, credits amount-fee, fee uncollected -> sum(bal) < totalSupply",
        "src": """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
contract FeeToken {
    mapping(address => uint256) public balanceOf;
    uint256 public totalSupply;
    constructor() {
        totalSupply = 1_000_000 ether;
        balanceOf[msg.sender] = totalSupply;
    }
    function transfer(address to, uint256 amount) external {
        require(balanceOf[msg.sender] >= amount, "insufficient");
        uint256 fee = amount / 100;
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount - fee;
    }
}
""",
    },
    {
        "name": "CleanVault",
        "has_bug": False,
        "note": "correct: deposit/withdraw keep total == sum(bal). control for precision.",
        "src": """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
contract CleanVault {
    mapping(address => uint256) public bal;
    uint256 public total;
    function deposit(uint256 a) external { bal[msg.sender] += a; total += a; }
    function withdraw(uint256 a) external {
        require(bal[msg.sender] >= a, "insufficient");
        bal[msg.sender] -= a;
        total -= a;
    }
}
""",
    },
]


def main() -> None:
    print(f"measure_recall: AI-proposer + Halmos on {len(PANEL)} realistic contracts\n")
    os.makedirs("/tmp/recall_panel", exist_ok=True)
    rows = []
    for c in PANEL:
        # clear stale src so only this contract compiles
        srcdir = os.path.join(agent.PROJECT, "src")
        if os.path.isdir(srcdir):
            for fn in os.listdir(srcdir):
                if fn.endswith(".sol"):
                    os.remove(os.path.join(srcdir, fn))
        path = f"/tmp/recall_panel/{c['name']}.sol"
        open(path, "w").write(c["src"])
        print(
            f"=== {c['name']} ({'BUGGY' if c['has_bug'] else 'correct'}) — {c['note']} ==="
        )
        res = agent.propose_and_check(path, c["name"])
        if not res["built"]:
            print("  (could not build a valid Halmos test)\n")
            rows.append((c["name"], c["has_bug"], "no-build", []))
            continue
        violated = [v["function"] for v in res["verdicts"] if not v["proved"]]
        proved = [v["function"] for v in res["verdicts"] if v["proved"]]
        for v in res["verdicts"]:
            mark = "✅ PROVED  " if v["proved"] else "🔴 VIOLATED"
            print(f"    {mark} {v['function']}")
        rows.append((c["name"], c["has_bug"], "ok", violated))
        print()

    print("=" * 60)
    buggy = [r for r in rows if r[1]]
    clean = [r for r in rows if not r[1]]
    recall_hits = [r for r in buggy if r[3]]  # buggy with >=1 violation
    precision_ok = [
        r for r in clean if r[2] == "ok" and not r[3]
    ]  # clean, no violation
    print("RESULT (invariant-expressible classes, realistic 0.8.x contracts):")
    print(
        f"  recall:    {len(recall_hits)}/{len(buggy)} buggy contracts surfaced a violation"
    )
    for r in buggy:
        print(
            f"    {'🟢' if r[3] else '🔴'} {r[0]}: {'violated ' + str(r[3]) if r[3] else 'MISSED (no violation surfaced)'}"
        )
    print(
        f"  precision: {len(precision_ok)}/{len(clean)} correct contracts stayed all-proved"
    )
    for r in clean:
        print(
            f"    {'🟢' if (r[2]=='ok' and not r[3]) else '🔴'} {r[0]}: {'clean' if not r[3] else 'FALSE ALARM ' + str(r[3])}"
        )


if __name__ == "__main__":
    main()
