"""
ski_ca_loop_continue — resume from v5 with a sharpened embedding hint.

Diagnosis from v5: S-reduction fails at k≥3 because the LLM embeds the S
redex starting at root (cells[0]).  Root's neighbors are then filled by
the embedding chain itself, leaving no EMPTY neighbor for the z-copy.
The fix is structural: embed the S redex at an INTERIOR cell so the
host cell has many free neighbors after embedding.

Same loop shape as ski_ca_loop.py; pre-seeded with v5 as prior_src.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from invariant_agent import _ask  # noqa: E402
from ski_ca_loop import (  # noqa: E402
    PAPER_SPEC, GOLD_TABLE, MAX_ITERS as _MAX,
    grade, save_attempt, STAGING, ATTEMPTS_DIR,
)

MAX_CONTINUE_ITERS = 4
EMBEDDING_HINT = """
=== SHARPENED HINT FROM PLATEAU ANALYSIS ===
The prior attempt scored 9/12. ALL THREE FAILURES are S-reduction at
k=3, k=4, k=5. The root cause is the EMBEDDING strategy, not the S
algorithm itself. Here is the specific bug:

  - Your (test-reduction) probably embeds the S chain (((S x) y) z)
    starting at cells[0] (the ROOT of the tree).
  - The S chain occupies ~7 cells, INCLUDING root's IMMEDIATE CHILDREN
    (cells[1], cells[2], cells[3] for k=3, etc.).
  - When the S-reduction step then asks "which of root's neighbors is
    EMPTY?", the answer is NONE — because root's only neighbors ARE
    its children, and the embedding put the S chain there.
  - So S-reduction fails for ALL k, even when the paper says k≥3 should
    work via "the exponentially growing neighborhood provides sufficient
    surplus."

THE FIX: embed the S redex at an INTERIOR cell whose neighborhood will
have free EMPTY cells available after embedding.  Specifically:

  - Choose a host cell at depth d/2 or deeper (e.g. cells[n/2] where n is
    the total cell count).  An interior cell at k=3 has degree up to 7
    (parent + 3 children + 3 same-parent siblings + uncles), so even
    after embedding consumes 6-7 cells along ONE branch from the host,
    several siblings and the parent remain EMPTY.
  - The S chain (((S x) y) z) should occupy the host PLUS one chain of
    descendants — NOT the host plus all its immediate neighbors.
  - For example: put the root of the S chain at cells[idx_host], then
    walk DOWN one descendant chain — host → host's first child → that
    child's first child → ... — to lay out the rest of the chain.

After this fix, S at k=3 should PASS because the host's siblings and
parent remain EMPTY and one of them serves as the z-copy neighbor.
S at k=2 should still FAIL because k=2 host has degree 4 max and the
chain consumes more than the surplus allows.

ALSO: do NOT change the I and K embeddings (they work — 8/8 PASS) and
do NOT change the topology code (degree ranges already match the paper).
ONLY fix the S-embedding placement strategy.
"""


def main():
    seed_path = os.path.join(ATTEMPTS_DIR, "v5_score09.lisp")
    seed_out_path = os.path.join(ATTEMPTS_DIR, "v5_score09.sbcl-out")
    if not os.path.exists(seed_path):
        print(f"seed not found: {seed_path}")
        sys.exit(1)

    print("=" * 70)
    print("ski_ca_loop_continue — resuming from v5 with embedding hint")
    print("=" * 70)
    print(f"  seed     : {seed_path} (9/12)")
    print(f"  hint     : embed S redex at interior cell, not root")
    print(f"  cap      : {MAX_CONTINUE_ITERS} additional rounds")
    print()

    prior_src = open(seed_path).read()
    prior_out = open(seed_out_path).read()
    prior_score = 9
    prior_diffs = [
        "k=3 rule=S: expected PASS, got FAIL",
        "k=4 rule=S: expected PASS, got FAIL",
        "k=5 rule=S: expected PASS, got FAIL",
    ]
    history = [9]
    t0 = datetime.now()

    for it in range(6, 6 + MAX_CONTINUE_ITERS):
        round_t0 = datetime.now()
        print(f"────────── round {it} ──────────")
        prompt = (
            f"[round {it}] You are debugging a near-correct Common Lisp implementation of the "
            "SKI-CA paper.  Output a corrected complete .lisp file.  Tested with `sbcl --script`. "
            "Return ONLY Lisp source — no prose, no fences.\n\n"
            f"=== PAPER SPEC ===\n{PAPER_SPEC}"
            + EMBEDDING_HINT
            + f"\n=== PRIOR ATTEMPT (round {it - 1}, {prior_score}/12) ===\n"
            + f"DISAGREEMENT:\n"
            + "\n".join(f"  {d}" for d in prior_diffs)
            + f"\n\nSBCL OUTPUT:\n{prior_out[-1500:]}\n\n"
            + f"YOUR PRIOR SOURCE:\n{prior_src}\n\n"
            "Apply the embedding fix described in the SHARPENED HINT.  Preserve everything "
            "else.  Return the full corrected file."
        )
        try:
            src = _ask(prompt, mt=10000)
        except Exception as e:
            print(f"  LLM call failed: {e}")
            break
        elapsed = (datetime.now() - round_t0).total_seconds()
        loc = src.count("\n") + 1
        print(f"  proposed {loc} lines ({elapsed:.1f}s)")

        score, out, diffs = grade(src)
        history.append(score)
        save_attempt(it, src, score, out)
        print(f"  fitness: {score}/12")
        if diffs:
            print("  disagreements:")
            for d in diffs[:6]:
                print(f"    {d}")
        if score == 12:
            print(f"\n  ✓ CONVERGED at round {it}")
            break
        prior_src, prior_out, prior_score, prior_diffs = src, out, score, diffs
        print()

    elapsed = (datetime.now() - t0).total_seconds()
    print()
    print("=" * 70)
    print(f"fitness trajectory: {' → '.join(str(s) for s in history)}")
    print(f"continuation wall-clock: {elapsed:.1f}s")
    print(f"attempts saved under: {ATTEMPTS_DIR}")
    print("=" * 70)


if __name__ == "__main__":
    main()
