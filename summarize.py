"""summarize — the machinery UNDER the summarizer metaprompt.

Split (the session's law, one level up):
  - metaprompt (sol_summary_meta.md): proposes a CANDIDATE summary (stub + axioms + obligation).
    Stochastic, untrusted.
  - GATE (discharge_*, below): the ROOT OF TRUST. Hand-written, deterministic. PROVES the
    obligation with z3 before a summary is ever registered. Never generated, never an LLM call.
    If it can't prove the obligation, the summary is REJECTED — no false proofs slip in.
  - REGISTRY: only obligation-discharged summaries.
  - apply_summary: deterministic source transform (next step).

The obligation clauses are: floor((a*s)/T) satisfies the Euclidean bracket, the zero case, floor
monotonicity, and (under no-overflow) product monotonicity.

MEASURED FINDING (2026-06-04): bounded z3 CANNOT be the sound gate here. Proving each clause is an
UNSAT query over bit-blasted multiply/divide, which hits an exponential wall: floor_bracket proves
in 0.1s @ 8-bit, 2.7s @ 12-bit, TIMES OUT @ 16-bit. A 12-bit certificate (operands < 4096) is
worthless for ~1e18 token amounts. z3 inherits the very nonlinear-UNSAT incapacity we are abstracting
away. So the division of labour is:
  - z3  = fast REFUTATION pre-filter (find counterexamples to UNSOUND axioms cheaply; SAT is fast)
  - LEAN = the sound ROOT OF TRUST that PROVES the universal claim, width-independent. The four
    clauses are Mathlib one-liners: Nat.div_add_mod (bracket), trivial (zero), Nat.div_le_div_right
    (floor-monotone), Nat.mul_le_mul_right (product-monotone). ADMISSION REQUIRES the Lean proof;
    z3 only screens. (Lean gate: TODO — the honest next piece.)
"""
from __future__ import annotations

from z3 import BitVec, ZeroExt, UDiv, UGT, UGE, ULT, ULE, Or, Solver, unsat, set_param

_TIMEOUT_MS = 30000  # per-clause; a discharge that times out is NOT a proof -> reject

# z3 bit-blasting of 256-bit UDiv/mul TIMES OUT. So z3 discharges a BOUNDED-WIDTH certificate (sound
# for operands < 2^WIDTH); the clauses are width-INDEPENDENT integer theorems, so the proper UNBOUNDED
# root of trust is Lean/Mathlib one-liners (named per clause below). The gate is SOUND either way: it
# admits only what is actually discharged, and rejects on timeout. WIDTH is the z3 certificate width.
WIDTH = 32        # z3 certifies operands < 2^32 (fast); raise toward 256 only as z3 allows
PWIDTH = 16       # product clause operands < 2^16 (product fits 2*PWIDTH, no wrap)


def _proved(sol) -> bool:
    sol.set("timeout", _TIMEOUT_MS)
    return sol.check() == unsat  # negation UNSAT => property holds for ALL operands at this width


def discharge_floor_bracket(bits=WIDTH) -> bool:
    """(1)/(2) q*T <= n < (q+1)*T  where q = floor(n/T).  Holds for ANY n (n = a*s is one such n),
    so no product is modelled. Remainder form 0 <= n - q*T < T avoids (q+1)*T overflow. q*T uses one
    multiply; q <= n so it cannot overflow at width `bits`.  UNBOUNDED: Nat.div_add_mod / Nat.lt_div."""
    n, T = BitVec("n", bits), BitVec("T", bits)
    q = UDiv(n, T)
    sol = Solver()
    sol.add(UGT(T, 0))
    sol.add(Or(UGT(q * T, n), UGE(n - q * T, T)))  # negation of the bracket
    return _proved(sol)


def discharge_zero_case(bits=256) -> bool:
    """(3) n == 0 => floor(n/T) == 0.  (a == 0 => a*s == 0 => shares == 0.)  No multiply; fast at 256."""
    n, T = BitVec("n", bits), BitVec("T", bits)
    sol = Solver()
    sol.add(UGT(T, 0), n == 0, UDiv(n, T) != 0)  # negation
    return _proved(sol)


def discharge_floor_monotone(bits=WIDTH) -> bool:
    """(4a) x2 >= x1 => floor(x2/T) >= floor(x1/T).  No multiply (256-bit UDiv still bit-blasts slow).
    UNBOUNDED: Nat.div_le_div_right."""
    x1, x2, T = BitVec("x1", bits), BitVec("x2", bits), BitVec("T", bits)
    sol = Solver()
    sol.add(UGT(T, 0), UGE(x2, x1), ULT(UDiv(x2, T), UDiv(x1, T)))  # negation
    return _proved(sol)


def discharge_product_monotone(bits=PWIDTH) -> bool:
    """(4b) a2 >= a1 => a2*s >= a1*s  — SOUND ONLY under no-overflow (the a*s < 2^256 bound): unsigned
    multiply is NOT monotone under wraparound. Proved on the WIDENED product (2*bits) so the proof's
    own arithmetic can't wrap; certifies the bound regime.  UNBOUNDED: Nat.mul_le_mul_right."""
    W = 2 * bits
    a1, a2, s = BitVec("a1", bits), BitVec("a2", bits), BitVec("s", bits)
    ext = lambda x: ZeroExt(W - bits, x)
    sol = Solver()
    sol.add(UGE(a2, a1), ULT(ext(a2) * ext(s), ext(a1) * ext(s)))  # negation
    return _proved(sol)


# obligation clause -> the deterministic z3 proof that discharges it
DISCHARGERS = {
    "floor_bracket": discharge_floor_bracket,
    "zero_case": discharge_zero_case,
    "floor_monotone": discharge_floor_monotone,
    "product_monotone": discharge_product_monotone,
}


_SCREEN_BITS = 8  # tiny width where z3 is FAST — used only to REFUTE bad axioms, never to admit


def screen(width=_SCREEN_BITS) -> tuple[bool, dict]:
    """z3 REFUTATION pre-filter (fast). At tiny width a clause either proves (survives) or yields a
    counterexample (refuted -> axiom UNSOUND -> reject the summary cheaply). Surviving does NOT admit;
    it only promotes the summary to the Lean obligation."""
    report = {}
    for k, fn in DISCHARGERS.items():
        bits = max(4, width // 2) if k == "product_monotone" else width
        report[k] = fn(bits)  # True = survived (proved at tiny width); False = refuted
    return all(report.values()), report


# Obligations DISCHARGED in Lean = the sound root of trust. An op is ADMITTED only if its obligation
# is registered here with a Lean proof verified (0 errors, 0 sorry) via:
#   cd ~/src/rule30 && lake env lean <abs path to lean/SummaryObligation.lean>
# Verified 2026-06-04 against Rule 30's Mathlib env. z3 only SCREENS; LEAN ADMITS.
REGISTRY = {
    "mulDiv/convertToShares": {
        "obligation": "floor bracket + zero-case + floor-monotone + product-monotone (over ℕ, a*s<2^256)",
        "lean_proof": "lean/SummaryObligation.lean",
        "status": "lean-discharged",
    },
}


def gate(op_key: str = "mulDiv/convertToShares") -> tuple[str, dict]:
    """THE GATE / root of trust. Two honest stages:
      1. z3 REFUTATION screen (fast) — kills unsound candidates outright.
      2. LEAN proof (sound, unbounded) — the ONLY thing that ADMITS. A summary is NEVER applied on a
         z3-only basis (unsound: z3 can't prove the 256-bit claim — measured exponential wall).
    Verdicts: 'rejected' (z3 refuted an axiom) | 'screened-pending-lean' (survived screen, no Lean
    proof yet) | 'admitted' (obligation discharged in Lean, in REGISTRY). Returns (verdict, report)."""
    survived, report = screen()
    if not survived:
        return "rejected", report
    entry = REGISTRY.get(op_key)
    if entry and entry.get("status") == "lean-discharged":
        return "admitted", report
    return "screened-pending-lean", report


if __name__ == "__main__":
    print("GATE: z3 refutation screen for the convertToShares / mulDiv obligation\n", flush=True)
    verdict, report = gate()
    for label, ok in report.items():
        print(f"  [{'survived' if ok else 'REFUTED'}]  {label}", flush=True)
    print(f"\n  => verdict: {verdict.upper()}")
    if verdict == "admitted":
        print("     (z3 screened + obligation DISCHARGED in Lean (lean/SummaryObligation.lean) = sound)")
    else:
        print("     (z3 screens; ADMISSION requires the Lean proof — the sound root of trust.)")
    # TEETH: a deliberately FALSE axiom must FAIL to prove (else the gate is decorative).
    # False claim: q*T > n (wrong direction; truly q*T <= n). "Prove" it => check its negation
    # ULE(q*T, n) is UNSAT. It is NOT unsat (it's satisfiable, in fact valid), so the gate cannot
    # prove the false claim -> correctly rejected.
    def proves_false_claim(bits=WIDTH) -> bool:
        n, T = BitVec("n", bits), BitVec("T", bits)
        q = UDiv(n, T)
        sol = Solver(); sol.set("timeout", _TIMEOUT_MS)
        sol.add(UGT(T, 0), ULE(q * T, n))  # negation of the FALSE claim "q*T > n"
        return sol.check() == unsat        # unsat would mean the false claim was "proved"
    print(f"  teeth-check (a false axiom must NOT prove): "
          f"{'correctly REJECTED' if not proves_false_claim() else 'WRONGLY ADMITTED — gate broken'}")
