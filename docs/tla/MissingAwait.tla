--------------------------- MODULE MissingAwait ---------------------------
(*
 * Formal specification of pact's missing_await checker decision procedure.
 *
 * Models the _CORO_CONSUMERS frozenset classification and proves:
 *
 *   Safety       — no call wrapped in a known consumer is ever flagged
 *   Completeness — every un-consumed awaitable call is eventually flagged
 *
 * Corresponds to:
 *   tools/pact/failure_mode.py  (_CORO_CONSUMERS, MissingAwaitMode.check)
 *
 * Design: we model consumer membership and awaitable-ness as set predicates
 * (ConsumedSites, AwaitableSites) rather than function constants, so TLC
 * can enumerate them directly without needing inline record definitions
 * in the config file.
 *
 * To check with TLC:
 *   cd docs/tla
 *   java -XX:+UseParallelGC -jar ~/.local/share/tla2tools.jar \
 *     -config MissingAwait.cfg -deadlock MissingAwait
 *)

EXTENDS Integers, FiniteSets, TLC

(* ---------------------------------------------------------------------------
   Constants
   --------------------------------------------------------------------------- *)

CONSTANTS
    Sites,          \* Set of all call-site identifiers in the codebase
    ConsumedSites,  \* Sites whose wrapping fn is in _CORO_CONSUMERS — must NOT be flagged
    AwaitableSites  \* Sites that produce a coroutine/awaitable — candidates to flag

ASSUME IsFiniteSet(Sites)
ASSUME ConsumedSites  \subseteq Sites
ASSUME AwaitableSites \subseteq Sites

(* ---------------------------------------------------------------------------
   Decision predicate (mirrors failure_mode.py logic exactly)

   ShouldFlag(s) iff:
     1. the call produces a coroutine  (s ∈ AwaitableSites), AND
     2. it is NOT wrapped in a consumer (s ∉ ConsumedSites)

   Equivalent Python:
     if call produces coroutine and call.consumer not in _CORO_CONSUMERS:
         yield FailureEvidence(...)
   --------------------------------------------------------------------------- *)

ShouldFlag(s) ==
    /\ s \in AwaitableSites
    /\ s \notin ConsumedSites

(* ---------------------------------------------------------------------------
   State variables
   --------------------------------------------------------------------------- *)

VARIABLES
    pending,    \* Sites not yet processed by the checker
    violations  \* Sites determined to be violations

vars == <<pending, violations>>

(* ---------------------------------------------------------------------------
   Initial state
   --------------------------------------------------------------------------- *)

Init ==
    /\ pending    = Sites
    /\ violations = {}

(* ---------------------------------------------------------------------------
   CheckSite — process one site: apply ShouldFlag, remove from pending.
   --------------------------------------------------------------------------- *)

CheckSite(s) ==
    /\ s \in pending
    /\ pending'    = pending \ {s}
    /\ violations' = IF ShouldFlag(s)
                     THEN violations \union {s}
                     ELSE violations

Next == \E s \in pending : CheckSite(s)

(* ---------------------------------------------------------------------------
   Fairness — every pending site is eventually checked.
   --------------------------------------------------------------------------- *)

Fairness == \A s \in Sites : WF_vars(CheckSite(s))

Spec == Init /\ [][Next]_vars /\ Fairness

(* ===========================================================================
   TYPE INVARIANT
   =========================================================================== *)

TypeInvariant ==
    /\ pending    \subseteq Sites
    /\ violations \subseteq Sites

(* ===========================================================================
   SAFETY: No consumed site is ever flagged
   ===========================================================================
 *
 * Core guarantee: adding a name to _CORO_CONSUMERS (ConsumedSites)
 * is sufficient to permanently exclude that site from violations.
 *
 * Proof trace: ShouldFlag(s) requires s ∉ ConsumedSites.
 * If s ∈ ConsumedSites, ShouldFlag(s) = FALSE → ELSE branch → violations unchanged.
 *)
NoFalsePositive ==
    violations \intersect ConsumedSites = {}

(*
 * Temporal form: consumed sites are permanently absent from violations.
 * Stronger than NoFalsePositive: holds in every reachable state, not just now.
 *)
ConsumedSitesPermanentlyClean ==
    [](violations \intersect ConsumedSites = {})

(* ===========================================================================
   SAFETY: Violations only grow
   =========================================================================== *)

MonotonicViolations ==
    [][violations \subseteq violations']_violations

(* ===========================================================================
   LIVENESS: Every real violation is eventually found
   ===========================================================================
 *
 * A "real violation" is a site that is awaitable and not consumed.
 * Under fairness, every such site is eventually processed and flagged.
 *)
EventuallyFlagged ==
    \A s \in Sites :
        ShouldFlag(s) => <>(s \in violations)

(* ===========================================================================
   COMPLETENESS: When done, violation set exactly matches real violations
   =========================================================================== *)

Done == pending = {}

CompletionCorrectness ==
    <>(Done /\
       violations = {s \in Sites : ShouldFlag(s)})

========================================================================
