--------------------------- MODULE RequiredArgMissing --------------------------
(*
 * Formal specification of pact's required_arg_missing checker.
 *
 * The bug class: a function is called without providing all of its required
 * positional arguments.  The common failure pattern:
 *
 *   def process(path, encoding, timeout):   -- 3 required args
 *       ...
 *
 *   process(path, encoding)                 -- missing timeout → TypeError
 *
 * A call is "covered" when positional_count + kwarg_keys fully satisfies
 * every required param of the callee.  Special cases that suppress the check
 * (pytest fixtures, Click commands, *args spreads, __main__ blocks) are modelled
 * as pre-classified SafeSites that are never flagged.
 *
 * Models the checker lifecycle over all call sites and proves:
 *
 *   Safety    — only genuinely uncovered calls are flagged
 *   Precision — covered calls and safe sites are never flagged
 *   Liveness  — every call site is eventually analyzed
 *
 * Corresponds to:
 *   tools/pact/failure_mode.py  (_check_required_arg / REQUIRED_ARG_MISSING)
 *
 * To check with TLC:
 *   cd docs/tla
 *   java -XX:+UseParallelGC -jar ~/.local/share/tla2tools.jar \
 *     -config RequiredArgMissing.cfg -deadlock RequiredArgMissing
 *)

EXTENDS Integers, FiniteSets, TLC

(* ---------------------------------------------------------------------------
   Constants
   --------------------------------------------------------------------------- *)

CONSTANTS
    CallSites,      \* All call sites where a function is invoked
    CoveredSites,   \* Sites where all required args are provided
    SafeSites       \* Sites suppressed: fixtures, Click commands, *args, __main__

ASSUME IsFiniteSet(CallSites)
ASSUME CoveredSites \subseteq CallSites
ASSUME SafeSites    \subseteq CallSites

\* A site is a violation iff args are not covered AND it is not in a safe category
IsViolation(s) == s \notin CoveredSites /\ s \notin SafeSites

(* ---------------------------------------------------------------------------
   State variables
   --------------------------------------------------------------------------- *)

VARIABLES
    analyzed,   \* Call sites processed by the checker
    flagged     \* Call sites identified as missing required args

vars == <<analyzed, flagged>>

(* ---------------------------------------------------------------------------
   Type invariant
   --------------------------------------------------------------------------- *)

TypeInvariant ==
    /\ analyzed \subseteq CallSites
    /\ flagged  \subseteq CallSites

(* ---------------------------------------------------------------------------
   Initial state
   --------------------------------------------------------------------------- *)

Init ==
    /\ analyzed = {}
    /\ flagged  = {}

(* ---------------------------------------------------------------------------
   AnalyzeCall — process one call site
   --------------------------------------------------------------------------- *)

AnalyzeCall(s) ==
    /\ s \in CallSites
    /\ s \notin analyzed
    /\ analyzed' = analyzed \union {s}
    /\ flagged'  = IF IsViolation(s)
                   THEN flagged \union {s}
                   ELSE flagged

(* ---------------------------------------------------------------------------
   State machine + fairness
   --------------------------------------------------------------------------- *)

Next == \E s \in CallSites : AnalyzeCall(s)

Fairness == \A s \in CallSites : WF_vars(AnalyzeCall(s))

Spec == Init /\ [][Next]_vars /\ Fairness

(* ===========================================================================
   STATE INVARIANTS
   =========================================================================== *)

(* Only genuine violations are flagged *)
FlaggedOnlyIfViolation ==
    flagged \subseteq {s \in CallSites : IsViolation(s)}

(* Covered calls are never flagged *)
CoveredNeverFlagged ==
    flagged \intersect CoveredSites = {}

(* Explicitly safe sites are never flagged *)
SafeSitesNeverFlagged ==
    flagged \intersect SafeSites = {}

(* ===========================================================================
   TEMPORAL PROPERTIES
   =========================================================================== *)

(* Every genuine violation is eventually flagged *)
ViolationsEventuallyFlagged ==
    \A s \in CallSites :
        IsViolation(s) => <>(s \in flagged)

(* All call sites are eventually analyzed — checker is complete *)
AllSitesEventuallyAnalyzed ==
    \A s \in CallSites : <>(s \in analyzed)

(* ===========================================================================
   COMPLETENESS
   =========================================================================== *)

Done == analyzed = CallSites

CompletionCorrectness ==
    Done => (flagged = {s \in CallSites : IsViolation(s)})

=============================================================================
