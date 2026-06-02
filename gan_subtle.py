"""
gan_subtle — discovery benchmark on CAMOUFLAGED z3-verified bugs.

Same pipeline as gan_symbolic, but each challenge is first run through the
disguise layer (deepseek camouflages the bug in realistic code; the original
planted PoC re-confirms the defect survived). The finder then faces code that
looks like a real audit target — but the ground truth is still the SAME bug
z3 proved + forge re-confirmed.

This is the honest hard-recall test: the first place pact's proposer could
legitimately under-perform.

    .venv/bin/python gan_subtle.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bugspec  # noqa: E402
import disguise  # noqa: E402
import gan  # noqa: E402
import gan_symbolic  # noqa: E402


def main() -> None:
    print(
        f"GAN (subtle): disguiser={disguise.DISGUISE_MODEL}  "
        f"finder={gan.FINDER_MODEL}  specs={len(bugspec.CATALOG)}"
    )
    rounds = []
    for spec in bugspec.CATALOG:
        print(f"\n##### {spec.name} #####")
        d = disguise.disguise(spec)
        if d is None:
            print("  (no surviving disguise — skipping)")
            continue
        r = gan_symbolic.run_spec(d)
        if r is not None:
            rounds.append(r)

    if not rounds:
        print("\nno valid disguised challenges")
        return
    valid = len(rounds)
    found = sum(r["found"] for r in rounds)
    cands = sum(r["candidates"] for r in rounds)
    genuine = sum(r["genuine"] for r in rounds)
    twins = sum(r["has_twin"] for r in rounds)
    genuine_found = sum(r["genuine"] > 0 for r in rounds if r["has_twin"])
    print("\n" + "=" * 56)
    print(
        f"disguised challenges (defect survived):       {valid}/{len(bugspec.CATALOG)}"
    )
    print(f"recall (found the planted bug):               {found}/{valid}")
    print(f"  sound (differential-genuine, twin built):   {genuine_found}/{twins}")
    print(
        f"precision (differential-genuine / candidates): {genuine}/{cands}"
        if cands
        else "precision: 0/0"
    )


if __name__ == "__main__":
    main()
