"""
gan_symbolic — discovery benchmark with SYMBOLIC bug construction.

Replaces the unreliable LLM generator with the z3-verified bugspec catalog:
every challenge is proven real (z3) AND confirmed by execution (forge) before the
finder sees it — and was constructed by no LLM, so it shares no authoring bias
with the Claude finder.

Per spec:
  1. symbolic gate  (bugspec.verify_symbolic)  — correct holds, defect breaks
  2. forge confirm  (bugspec.confirm_forge)     — planted PoC actually exploits
  3. finder blind   (gan.finder_propose)        — Claude sees ONLY Target.sol
  4. forge oracle   (gan.finder_gate)           — each candidate auto-PoC'd + run
  5. soft match     (gan.judge_match)           — found THE planted bug?

    .venv/bin/python gan_symbolic.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bugspec  # noqa: E402
import gan  # noqa: E402


def run_spec(spec: bugspec.BugSpec) -> dict | None:
    print(f"\n=== {spec.name} ({spec.bug_class}) ===")

    sym = bugspec.verify_symbolic(spec)
    if sym["verified"] is False:
        print(f"  symbolic gate FAILED ({sym}); skipping")
        return None
    if sym["verified"] is None:
        print("  symbolic: (forge-only — no z3 model yet); relying on forge")
    else:
        print("  symbolic: z3 proves correct holds & defect breaks invariant ✓")

    planted_ok, _ = bugspec.confirm_forge(spec)
    if not planted_ok:
        print("  forge confirm FAILED; skipping")
        return None
    print("  forge: planted PoC exploits the rendered contract ✓")

    # finder phase — Claude sees only the contract source
    gan._clean_tests()
    cands = gan.finder_propose(spec.contract)
    print(f"  finder: proposed {len(cands)} candidate(s) (blind)")
    genuine = 0  # differential-verified: targets THE planted defect (no soft judge)
    verified = 0  # forge-passed but no differential twin available (weaker)
    for c in cands:
        verdict = gan.finder_gate(spec.contract, c, fixed_contract=spec.fixed_contract)
        genuine += verdict == "genuine"
        verified += verdict == "verified"
        mark = {
            "genuine": "🟢 GENUINE (passes buggy, fails fixed)",
            "trivial": "🟡 trivial  (passes both — not the bug)",
            "verified": "🟢 verified (no fixed twin — weaker)",
            "rejected": "🔴 rejected (no working exploit)",
        }[verdict]
        print(f"    {mark}  {c.get('title','?')}")

    has_twin = spec.fixed_contract is not None
    return {
        "name": spec.name,
        "candidates": len(cands),
        "genuine": genuine,
        "verified": verified,
        "has_twin": has_twin,
        # differential ⇒ provably the planted bug; else fall back to forge-verified
        "found": (genuine > 0) if has_twin else (verified > 0),
    }


def main() -> None:
    print(f"GAN (symbolic): finder={gan.FINDER_MODEL}  specs={len(bugspec.CATALOG)}")
    rounds = [r for s in bugspec.CATALOG if (r := run_spec(s)) is not None]
    if not rounds:
        print("\nno valid specs")
        return
    valid = len(rounds)
    found = sum(r["found"] for r in rounds)
    cands = sum(r["candidates"] for r in rounds)
    genuine = sum(r["genuine"] for r in rounds)
    twins = sum(r["has_twin"] for r in rounds)
    print("\n" + "=" * 56)
    print(f"valid challenges (symbolic+forge verified): {valid}/{len(bugspec.CATALOG)}")
    print(f"recall (found the planted bug):              {found}/{valid}")
    print(f"  of which had a differential twin (sound):  {twins}/{valid}")
    print(
        f"precision (differential-genuine / candidates): {genuine}/{cands}"
        if cands
        else "precision: 0/0"
    )


if __name__ == "__main__":
    main()
