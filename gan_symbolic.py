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
    if not sym["verified"]:
        print(f"  symbolic gate FAILED ({sym}); skipping")
        return None
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
    verified = 0
    on_target = False
    for c in cands:
        ok = gan.finder_gate(spec.contract, c)
        verified += ok
        tag = "🟢 forge-VERIFIED" if ok else "🔴 unverified   "
        hit = ""
        if ok and gan.judge_match(f"{spec.invariant} — {spec.bug_class}", c):
            on_target = True
            hit = "  [matches planted bug]"
        print(f"    {tag}  {c.get('title','?')}{hit}")

    return {
        "name": spec.name,
        "candidates": len(cands),
        "verified": verified,
        "found": verified > 0,
        "on_target": on_target,
    }


def main() -> None:
    print(f"GAN (symbolic): finder={gan.FINDER_MODEL}  specs={len(bugspec.CATALOG)}")
    rounds = [r for s in bugspec.CATALOG if (r := run_spec(s)) is not None]
    if not rounds:
        print("\nno valid specs")
        return
    valid = len(rounds)
    found = sum(r["found"] for r in rounds)
    on_target = sum(r["on_target"] for r in rounds)
    cands = sum(r["candidates"] for r in rounds)
    verified = sum(r["verified"] for r in rounds)
    print("\n" + "=" * 56)
    print(f"valid challenges (symbolic+forge verified): {valid}/{len(bugspec.CATALOG)}")
    print(f"recall   (finder forge-verified an exploit): {found}/{valid}")
    print(f"on-target (matched planted bug, soft judge): {on_target}/{valid}")
    print(
        f"precision (verified / proposed candidates):  " f"{verified}/{cands}"
        if cands
        else "precision: 0/0"
    )


if __name__ == "__main__":
    main()
