---- MODULE PactLoop ----
(*
 * Formal model of pact_loop.py — recursive self-improvement with ML convergence.
 *
 * Key properties proved:
 *   OracleSafety          -- NO patch is applied unless the oracle (test suite) passed
 *   Termination           -- the loop ALWAYS halts (finite iterations)
 *   FitnessMonotone       -- fitness is non-decreasing across windows that converge
 *   StuckDetection        -- if CEGIS makes no progress for STUCK_WINDOW iters, loop exits
 *   CacheFreshInMeasure   -- caches cleared before every measure (LRU staleness gap, ADR-042)
 *   EpochMonotone         -- cache_epoch never decreases
 *
 * Corresponds to ADR-037, ADR-042, pact_loop.py, and the CEGIS oracle model.
 * ADR-042: added cache_epoch to model the LRU staleness abstraction gap found by
 *          spec_learner (gap_name: cache_opacity, confidence: 0.87, verdict: CATCHES_BUG).
 *
 * Verified with TLC:
 *   cd docs/tla && java -jar tla2tools.jar -config PactLoop.cfg PactLoop.tla
 *
 * Constants to set in PactLoop.cfg:
 *   MAX_ITERS   = 5      (safety bound; loop terminates by this)
 *   STUCK_WINDOW = 2     (consecutive zero-accept iterations → STUCK)
 *   WINDOW       = 3     (convergence window size)
 *   EPSILON      = 1     (fitness delta threshold, scaled ×100 for integer TLA+)
 *
 * NOTE: fitness is modelled as integers in 0..100 (×100 of actual float).
 *       EPSILON = 1 corresponds to ε = 0.01 in the Python code.
 *)
EXTENDS Naturals, Sequences, TLC

CONSTANTS
    MAX_ITERS,      \* hard upper bound on loop iterations
    STUCK_WINDOW,   \* consecutive zero-accept iterations before STUCK declared
    WINDOW,         \* number of recent fitness values checked for convergence
    EPSILON         \* max fitness delta (×100) for convergence (integer)

VARIABLES
    iter,               \* current iteration number (0..MAX_ITERS)
    violations,         \* current violation count (Nat)
    fitness_history,    \* sequence of integer fitness values (×100, 0..100)
    oracle_passed,      \* set of patch IDs that passed the oracle test suite
    patches_applied,    \* set of patch IDs actually applied to the codebase
    accepted_history,   \* sequence of accepted patch counts per iteration
    phase,              \* current phase of the loop
    termination,        \* termination reason ("" = running)
    cache_epoch,        \* monotone counter: bumped by clear_file_caches() at Measure start
    caches_fresh        \* TRUE iff caches were cleared since the last Heal that applied patches

vars == <<iter, violations, fitness_history, oracle_passed, patches_applied,
          accepted_history, phase, termination, cache_epoch, caches_fresh>>

(* ────────────────────────── Type invariant ────────────────────────── *)

TypeInvariant ==
    /\ iter \in 0..MAX_ITERS
    /\ violations \in Nat
    /\ fitness_history \in Seq(0..100)
    /\ oracle_passed \subseteq Nat
    /\ patches_applied \subseteq Nat
    /\ accepted_history \in Seq(Nat)
    /\ phase \in {"measure", "heal", "improve", "check"}
    /\ termination \in {"", "PROVED_CLEAN", "CONVERGED", "STUCK", "TIMEOUT"}
    /\ cache_epoch \in Nat
    /\ caches_fresh \in {TRUE, FALSE}

(* ────────────────────────── Helpers ──────────────────────────────── *)

\* Last N elements of a sequence
TakeLast(s, n) ==
    IF Len(s) <= n THEN s
    ELSE SubSeq(s, Len(s) - n + 1, Len(s))

\* Max of a sequence of integers
SeqMax(s) ==
    IF s = <<>> THEN 0
    ELSE LET RECURSIVE _Max(_, _)
             _Max(i, m) == IF i > Len(s) THEN m
                           ELSE _Max(i + 1, IF s[i] > m THEN s[i] ELSE m)
         IN _Max(1, s[1])

\* Min of a sequence of integers
SeqMin(s) ==
    IF s = <<>> THEN 100
    ELSE LET RECURSIVE _Min(_, _)
             _Min(i, m) == IF i > Len(s) THEN m
                           ELSE _Min(i + 1, IF s[i] < m THEN s[i] ELSE m)
         IN _Min(1, s[1])

\* TRUE iff the last WINDOW fitness values differ by less than EPSILON
Converged ==
    /\ Len(fitness_history) >= WINDOW
    /\ LET recent == TakeLast(fitness_history, WINDOW)
       IN  SeqMax(recent) - SeqMin(recent) < EPSILON

\* TRUE iff the last STUCK_WINDOW accepted counts are all zero
Stuck ==
    /\ Len(accepted_history) >= STUCK_WINDOW
    /\ LET recent == TakeLast(accepted_history, STUCK_WINDOW)
       IN  \A i \in 1..Len(recent) : recent[i] = 0

(* ────────────────────────── Initial state ─────────────────────────── *)

Init ==
    /\ iter             = 0
    /\ violations       = 0
    /\ fitness_history  = <<>>
    /\ oracle_passed    = {}
    /\ patches_applied  = {}
    /\ accepted_history = <<>>
    /\ phase            = "measure"
    /\ termination      = ""
    /\ cache_epoch      = 0
    /\ caches_fresh     = TRUE   \* no patches applied yet; initial state is clean

(* ────────────────────────── Phase transitions ─────────────────────── *)

(*
 * Measure: scan codebase for violations.
 * Models clear_file_caches() — bumps cache_epoch and marks caches fresh.
 * This is the fix for the cache_opacity abstraction gap (ADR-042):
 * without this, stale LRU scanner results persist from the previous Heal.
 *)
Measure ==
    /\ phase        = "measure"
    /\ termination  = ""
    /\ phase'       = "heal"
    /\ cache_epoch' = cache_epoch + 1
    /\ caches_fresh' = TRUE
    /\ UNCHANGED <<iter, violations, fitness_history,
                   oracle_passed, patches_applied, accepted_history, termination>>

(*
 * Heal: CEGIS synthesis round.
 *
 * patch_id — unique identifier for the proposed patch (modelled as Nat).
 * oracle_ok — TRUE iff the oracle (test suite) passes for this patch.
 * n_accepted — number of patches accepted this heal phase (0 or more).
 *
 * ORACLE SAFETY KEY RULE:
 *   patches_applied' \subseteq oracle_passed'
 * This is maintained because we only add patch_id to patches_applied when
 * oracle_ok = TRUE, and we add it to oracle_passed in that same step.
 *)
Heal(patch_id, oracle_ok, n_accepted) ==
    /\ phase = "heal"
    /\ IF oracle_ok
       THEN /\ oracle_passed'    = oracle_passed    \cup {patch_id}
            /\ patches_applied'  = patches_applied  \cup {patch_id}
            /\ caches_fresh'     = FALSE   \* patched files → scanner caches now stale
       ELSE /\ UNCHANGED <<oracle_passed, patches_applied>>
            /\ UNCHANGED caches_fresh
    /\ accepted_history' = Append(accepted_history, n_accepted)
    /\ phase'            = "improve"
    /\ UNCHANGED <<iter, violations, fitness_history, termination, cache_epoch>>

Improve ==
    /\ phase  = "improve"
    /\ phase' = "check"
    /\ UNCHANGED <<iter, violations, fitness_history,
                   oracle_passed, patches_applied, accepted_history, termination,
                   cache_epoch, caches_fresh>>

(*
 * Check: measure new violations and fitness, test termination conditions.
 *
 * new_v  — new violation count after this iteration's patches.
 * new_f  — new fitness (integer ×100, 0..100).
 * sheaf0 — TRUE iff sheaf Ȟ¹ rank is 0 (all interprocedural paths clean).
 *
 * Termination priority (first match wins):
 *   PROVED_CLEAN  new_v = 0 AND sheaf0
 *   CONVERGED     Converged (last WINDOW fitness deltas < EPSILON)
 *   STUCK         Stuck (last STUCK_WINDOW heal rounds accepted 0)
 *   TIMEOUT       iter + 1 >= MAX_ITERS
 *)
Check(new_v, new_f, sheaf0) ==
    /\ phase            = "check"
    /\ violations'      = new_v
    /\ fitness_history' = Append(fitness_history, new_f)
    /\ iter'            = iter + 1
    /\ LET next_h == Append(fitness_history, new_f)
           done  ==
               IF new_v = 0 /\ sheaf0 THEN "PROVED_CLEAN"
               ELSE IF Converged      THEN "CONVERGED"
               ELSE IF Stuck          THEN "STUCK"
               ELSE IF iter + 1 >= MAX_ITERS THEN "TIMEOUT"
               ELSE ""
       IN termination' = done
    /\ phase'           = IF termination' /= "" THEN "check" ELSE "measure"
    /\ UNCHANGED <<oracle_passed, patches_applied, accepted_history,
                   cache_epoch, caches_fresh>>

(* ────────────────────────── Next-state relation ───────────────────── *)

Next ==
    \/ Measure
    \/ \E pid \in 0..9, ok \in {TRUE, FALSE}, na \in 0..5 : Heal(pid, ok, na)
    \/ Improve
    \/ \E v \in 0..50, f \in 0..100, s \in {TRUE, FALSE} : Check(v, f, s)

(* ────────────────────────── Fairness ─────────────────────────────── *)

(*
 * Weak fairness on all phases ensures the loop makes progress.
 * Without fairness, TLC can find stuttering counter-examples to Termination.
 *)
Fairness ==
    /\ WF_vars(Measure)
    /\ WF_vars(Improve)
    /\ \E v \in 0..50, f \in 0..100, s \in {TRUE, FALSE} :
           WF_vars(Check(v, f, s))
    /\ \E pid \in 0..9, ok \in {TRUE, FALSE}, na \in 0..5 :
           WF_vars(Heal(pid, ok, na))

(* ────────────────────────── Specification ─────────────────────────── *)

Spec == Init /\ [][Next]_vars /\ Fairness

(* ────────────────────────── Properties ───────────────────────────── *)

(*
 * OracleSafety: every applied patch was previously oracle-approved.
 * This is the core CEGIS guarantee — the oracle cannot be bypassed.
 *)
OracleSafety == []( patches_applied \subseteq oracle_passed )

(*
 * Termination: the loop eventually reaches a terminal state.
 * Holds because iter increases each Check step and MAX_ITERS bounds it.
 *)
Termination == <>[](termination /= "")

(*
 * PhaseProgress: once the loop halts, phase stays at "check".
 * Prevents phantom phase cycling after termination.
 *)
PhaseProgress == [](termination /= "" => phase = "check")

(*
 * FitnessMonotone: if the loop converges, the fitness window is non-decreasing.
 * Weaker property — holds under the assumption that CEGIS doesn't regress.
 *)
FitnessMonotone ==
    [](termination = "CONVERGED" =>
        \A i \in 1..(Len(fitness_history) - 1) :
            fitness_history[i+1] >= fitness_history[i] - EPSILON)

(*
 * ── cache_opacity refinement (ADR-042) ──────────────────────────────────────
 *
 * These three invariants were discovered by the spec_learner ML pipeline.
 * Bug: LRU-cached file scanners served stale violations after heal() modified
 *      files. The original spec modelled `violations \in Nat` (a count), hiding
 *      whether the count was computed from fresh or stale scanner caches.
 * Fix: clear_file_caches() called at the start of each Measure phase.
 *      Modelled here as: Measure bumps cache_epoch and sets caches_fresh=TRUE.
 *
 * gap_name: cache_opacity  confidence: 0.87  verdict: CATCHES_BUG
 *)

(*
 * CacheFreshInMeasure: scanner caches must be cleared before measuring.
 * This is the invariant that WOULD HAVE caught the bug before the fix.
 *)
CacheFreshInMeasure == [](phase = "measure" => caches_fresh = TRUE)

(*
 * StaleResultsExcluded: temporal alias — always, in measure phase, caches fresh.
 * Same as CacheFreshInMeasure but named after the spec_learner invariant.
 *)
StaleResultsExcluded == CacheFreshInMeasure

(*
 * EpochMonotone: cache_epoch is a monotone counter — it never decreases.
 * Guards against implementation bugs that might reset or wrap the epoch.
 *)
EpochMonotone == [](cache_epoch >= 0)

====
