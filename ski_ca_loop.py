"""
ski_ca_loop — plumbline verifier-discharge applied OUTSIDE Solidity.

The pact/plumbline thesis: shape (human) + fluency (LLM) + soundness (verifier).
The same loop drives any domain where the verifier is sound and the fitness
signal is granular.  Here:

  - SHAPE     = the SKI-CA paper §2.1 (cell struct) + §2.2 (graph topology) +
                §2.3 (reduction rules) + §3.1 (expected output table). Fixed.
  - FLUENCY   = the LLM proposes a Common Lisp implementation.
  - SOUNDNESS = SBCL execution + diff against §3.1 Table 1 (12 expected
                PASS/FAIL bits). The verifier emits the fitness directly.

Same iteration shape as recall_loop.py: propose → grade → re-propose with
the disagreement fed back, capped at MAX_ITERS rounds.

The pact bits we REUSE:
  - invariant_agent._ask  (Anthropic API call with code-fence stripping)
The pact bits we DROP (Solidity-specific):
  - _setup_project / _build / run_halmos / generate_mutants / sol_intent

Run:  .venv/bin/python ski_ca_loop.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from invariant_agent import _ask  # noqa: E402  (same Anthropic client as the Solidity loop)

SBCL = "/opt/homebrew/bin/sbcl"
STAGING = os.path.expanduser("~/Desktop/ski-staging/ski-ca")
ATTEMPTS_DIR = os.path.join(STAGING, "attempts")
MAX_ITERS = 5
SBCL_TIMEOUT = 120

# ─────────────────────────  PAPER §3.1 — THE GOLD ORACLE  ─────────────────────────
# 12 expected (k, rule, pass) bits.  S at k=2 must FAIL (connectivity threshold).
GOLD_TABLE = [
    (2, "I", True),  (2, "K", True),  (2, "S", False),
    (3, "I", True),  (3, "K", True),  (3, "S", True),
    (4, "I", True),  (4, "K", True),  (4, "S", True),
    (5, "I", True),  (5, "K", True),  (5, "S", True),
]

# Paper sections (the SHAPE) held verbatim and fed to the proposer.
PAPER_SPEC = """
[§2.1 Cell structure]
defstruct cell:
  state in {EMPTY, S, K, I, VAR, APP}
  aux  — variable name when state=VAR
  left, right — APP child pointers
  used — flag for whether cell holds active data
All fields are double-buffered: write into next-* slots, atomic swap at step boundary.

[§2.2 Graph topology]
Regular trees with branching factor k, depth d, with SIBLING LINKS.
Adjacency = tree edges (parent ↔ children) UNION sibling edges.
Total cells = (k^(d+1) - 1) / (k - 1).  For d=6: k=2 → 127, k=3 → 1093,
k=4 → 5461, k=5 → 19531.
Degree ranges over INTERIOR cells (d=6):
  k=2 → 2..5
  k=3 → 3..7
  k=4 → 3..8
  k=5 → 3..9
The degree-range constraint is a SIGNAL: if your topology produces max-degree 4
at k=2, your sibling-link construction is too narrow.  A construction that
matches the paper: each cell is sibling-linked to ALL its same-parent siblings
AND to its parent's siblings (uncles).  That gives degree at interior
non-root non-leaf cell = parent + k children + (k-1) same-parent siblings + ...

[§2.3 Reduction rules]
I x        → x.        Replace root with copy of arg.
K x y      → x.        Replace root with copy of first arg.
S x y z    → (x z)(y z).
                       Step 1: find a FREE neighboring cell (state=EMPTY) for the z-copy.
                       Step 2: write APP(x, z) into one slot, APP(y, z-copy) into another.
                       Step 3: rewire root to point to those two APPs.
                       If no free neighbor exists, S-reduction FAILS — that's
                       the connectivity threshold.

[§3.1 Expected output table — the FITNESS SIGNAL]
Sweep k ∈ {2,3,4,5}, d ∈ {3,4,5,6}, test each of I/K/S.
Expected PASS/FAIL per (k, rule), independent of depth:
  k=2: I=PASS  K=PASS  S=FAIL
  k=3: I=PASS  K=PASS  S=PASS
  k=4: I=PASS  K=PASS  S=PASS
  k=5: I=PASS  K=PASS  S=PASS
Total: 11 PASS, 1 FAIL out of 12.

[Output protocol your harness MUST use]
Your (run-paper-table) function MUST print exactly one line per (k, rule)
combination in the format:
    k=K rule=R result=PASS
or
    k=K rule=R result=FAIL
Use uppercase PASS/FAIL.  Any deviation in format will not be parsed and your
score will be 0.  Print one such line for each of the 12 combinations.
"""


def propose_prompt(round_no: int, prior_src: str | None,
                   prior_out: str | None, prior_score: int | None,
                   prior_diffs: list[str] | None) -> str:
    base = (
        f"[round {round_no}/{MAX_ITERS}] Implement the SKI-CA paper's cellular-automaton "
        "in Common Lisp.  Output a single complete .lisp file that defines the cell struct, "
        "builds a tree-CA with the specified topology, implements I/K/S reductions, and "
        "calls (run-paper-table) at file end with the exact output protocol below.\n\n"
        "Tested with SBCL via `sbcl --script <file>`.  Return ONLY the Lisp source — no "
        "prose, no fences.\n\n"
        f"=== PAPER SPEC ===\n{PAPER_SPEC}"
    )
    if prior_src is not None:
        base += (
            f"\n=== PRIOR ATTEMPT (round {round_no - 1}) ===\n"
            f"FITNESS: {prior_score}/12\n"
            f"PER-ROW DISAGREEMENT:\n"
            + "\n".join(f"  {d}" for d in (prior_diffs or []))
            + f"\n\nSBCL OUTPUT (tail):\n{(prior_out or '')[-2000:]}\n\n"
            f"YOUR PRIOR SOURCE:\n{prior_src}\n\n"
            "Fix the disagreement.  The expected table is fixed; the implementation must change.  "
            "Focus on the FAILED rows above.  If the score is 0, the most common causes are "
            "(a) output format doesn't match `k=K rule=R result=PASS/FAIL` exactly, "
            "(b) reduce-step never fires because head-of-app or spine-args is buggy, "
            "(c) sibling-link construction is too narrow so degree range is wrong."
        )
    return base


def parse_results(stdout: str) -> dict[tuple[int, str], bool]:
    out = {}
    for m in re.finditer(r"k=(\d+)\s+rule=([IKS])\s+result=(PASS|FAIL)", stdout):
        out[(int(m.group(1)), m.group(2))] = (m.group(3) == "PASS")
    return out


def grade(lisp_src: str) -> tuple[int, str, list[str]]:
    """Write the Lisp, run sbcl, parse output, return (score, raw, diff_lines)."""
    os.makedirs(STAGING, exist_ok=True)
    path = os.path.join(STAGING, "ski-ca.lisp")
    with open(path, "w") as f:
        f.write(lisp_src)
    try:
        r = subprocess.run(
            [SBCL, "--script", path],
            capture_output=True, text=True, timeout=SBCL_TIMEOUT,
        )
        out = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return 0, f"TIMEOUT after {SBCL_TIMEOUT}s", ["TIMEOUT"]
    except FileNotFoundError:
        return 0, f"SBCL not found at {SBCL}", ["sbcl missing"]
    results = parse_results(out)
    score = 0
    diffs = []
    for k, rule, expected in GOLD_TABLE:
        got = results.get((k, rule))
        if got == expected:
            score += 1
        elif got is None:
            diffs.append(f"k={k} rule={rule}: expected {'PASS' if expected else 'FAIL'}, "
                         f"got NO OUTPUT")
        else:
            diffs.append(f"k={k} rule={rule}: expected {'PASS' if expected else 'FAIL'}, "
                         f"got {'PASS' if got else 'FAIL'}")
    return score, out, diffs


def save_attempt(round_no: int, src: str, score: int, out: str) -> None:
    os.makedirs(ATTEMPTS_DIR, exist_ok=True)
    base = os.path.join(ATTEMPTS_DIR, f"v{round_no}_score{score:02d}")
    with open(base + ".lisp", "w") as f:
        f.write(src)
    with open(base + ".sbcl-out", "w") as f:
        f.write(out)


def main():
    print("=" * 70)
    print("ski_ca_loop — plumbline verifier-discharge synthesis, applied to Lisp")
    print("=" * 70)
    print(f"  shape    : SKI-CA paper §2.1/§2.2/§2.3/§3.1 (held verbatim)")
    print(f"  fluency  : LLM via pact's invariant_agent._ask")
    print(f"  verifier : {SBCL}")
    print(f"  fitness  : 12-bit match against paper Table 1")
    print(f"  cap      : {MAX_ITERS} rounds")
    print(f"  staging  : {STAGING}")
    print(f"  attempts : {ATTEMPTS_DIR}")
    print()

    prior_src, prior_out, prior_score, prior_diffs = None, None, None, None
    history = []
    t0 = datetime.now()

    for it in range(1, MAX_ITERS + 1):
        round_t0 = datetime.now()
        print(f"────────── round {it} ──────────")
        prompt = propose_prompt(it, prior_src, prior_out, prior_score, prior_diffs)
        try:
            src = _ask(prompt, mt=8000)
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
            if len(diffs) > 6:
                print(f"    ...and {len(diffs) - 6} more")
        if score == 12:
            print(f"\n  ✓ CONVERGED at round {it}")
            break
        prior_src, prior_out, prior_score, prior_diffs = src, out, score, diffs
        print()

    elapsed = (datetime.now() - t0).total_seconds()
    print()
    print("=" * 70)
    print(f"fitness trajectory: {' → '.join(str(s) for s in history)}")
    print(f"total wall-clock: {elapsed:.1f}s")
    print(f"attempts saved under: {ATTEMPTS_DIR}")
    print("=" * 70)


if __name__ == "__main__":
    main()
