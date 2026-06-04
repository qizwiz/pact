/-
  The convertToShares / mulDiv summary's SOUNDNESS OBLIGATION, discharged in Lean over ℕ — the
  true integers the abstraction is sound in (solmate mulDivDown computes the exact floor via a
  512-bit intermediate, so the math value is floor(a*s/T) with no wraparound).

  This is the ROOT OF TRUST for the FV-summarization gate (pact-standalone/summarize.py). z3 only
  REFUTES bad axioms cheaply; it cannot PROVE these (exponential bit-blast wall, measured: 0.1s@8bit,
  2.7s@12bit, timeout@16bit). Lean proves them width-INDEPENDENTLY, which is what soundness needs.

  Each theorem certifies one axiom the metaprompt (sol_summary_meta.md) proposed for convertToShares.
  Zero `sorry`, zero axioms beyond Mathlib ⇒ the obligation is discharged ⇒ the summary is ADMITTED.
-/
-- Only Lean/Std CORE lemmas are used below, so no Mathlib AGGREGATE oleans are needed (those
-- aren't built in this project — only leaf modules are). One confirmed-built import supplies `omega`.
import Mathlib.Tactic.IntervalCases

namespace PactSummaryObligation

/-- (1)/(2) Euclidean floor bracket: with `q = n / T`, we have `q*T ≤ n < (q+1)*T`.
    (`n = a*s` is one such `n`; the bracket holds for every `n`.) -/
theorem floor_bracket (n T : ℕ) (hT : 0 < T) :
    (n / T) * T ≤ n ∧ n < (n / T + 1) * T := by
  refine ⟨Nat.div_mul_le_self n T, ?_⟩
  have hdm : (n / T) * T + n % T = n := by rw [Nat.mul_comm]; exact Nat.div_add_mod n T
  have hml : n % T < T := Nat.mod_lt n hT
  have hsucc : (n / T + 1) * T = (n / T) * T + T := by rw [Nat.add_mul, Nat.one_mul]
  rw [hsucc]; omega

/-- (3) zero case: `a = 0 ⇒ a*s = 0 ⇒ shares = 0`. -/
theorem zero_case (T : ℕ) : (0 : ℕ) / T = 0 := Nat.zero_div T

/-- (4a) floor is monotone in the dividend: `x₁ ≤ x₂ ⇒ x₁/T ≤ x₂/T`. -/
theorem floor_monotone {x₁ x₂ : ℕ} (T : ℕ) (h : x₁ ≤ x₂) : x₁ / T ≤ x₂ / T :=
  Nat.div_le_div_right h

/-- (4b) product monotone (sound over ℕ — the a*s < 2^256 no-overflow regime):
    `a₁ ≤ a₂ ⇒ a₁*s ≤ a₂*s`. -/
theorem product_monotone {a₁ a₂ : ℕ} (s : ℕ) (h : a₁ ≤ a₂) : a₁ * s ≤ a₂ * s :=
  Nat.mul_le_mul h (Nat.le_refl s)

end PactSummaryObligation
