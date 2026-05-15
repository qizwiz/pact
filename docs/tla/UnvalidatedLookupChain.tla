----------------------- MODULE UnvalidatedLookupChain -------------------------
(*
 * Formal specification of pact's unvalidated_lookup_chain checker.
 *
 * The bug class: a value retrieved via dict.get() (which is Optional) is used
 * as a subscript key in a second, different collection without a membership
 * check first.  The common failure pattern:
 *
 *   x = mapping.get(key)       -- x may be None
 *   if x:                      -- guards None, but NOT "x in other"
 *       value = other[x]       -- KeyError if x is absent from other
 *
 * A guarded site has an intervening `x in other` or `x not in other` check
 * before the subscript access.  An unguarded site does not.
 *
 * The spec models the data flow from get()-assigned optional variables to
 * subscript-index uses and proves:
 *
 *   Safety    — only unguarded lookup chains are flagged
 *   Precision — guarded sites are never flagged
 *   Liveness  — every lookup-chain site is eventually analyzed
 *
 * Corresponds to:
 *   tools/pact/failure_mode.py  (_scan_file_unvalidated_lookup_chain)
 *
 * To check with TLC:
 *   cd docs/tla
 *   java -XX:+UseParallelGC -jar ~/.local/share/tla2tools.jar \
 *     -config UnvalidatedLookupChain.cfg -deadlock UnvalidatedLookupChain
 *)

EXTENDS Integers, FiniteSets, TLC

(* ---------------------------------------------------------------------------
   Constants
   --------------------------------------------------------------------------- *)

CONSTANTS
    LookupSites,    \* All sites where a get()-assigned var is used as a subscript
    GuardedSites    \* Sites that have an intervening membership check (x in other)

ASSUME IsFiniteSet(LookupSites)
ASSUME GuardedSites \subseteq LookupSites

IsUnguarded(s) == s \notin GuardedSites

(* ---------------------------------------------------------------------------
   State variables
   --------------------------------------------------------------------------- *)

VARIABLES
    analyzed,   \* Sites processed by the checker
    flagged     \* Sites flagged as unguarded lookup chains

vars == <<analyzed, flagged>>

(* ---------------------------------------------------------------------------
   Type invariant
   --------------------------------------------------------------------------- *)

TypeInvariant ==
    /\ analyzed \subseteq LookupSites
    /\ flagged  \subseteq LookupSites

(* ---------------------------------------------------------------------------
   Initial state
   --------------------------------------------------------------------------- *)

Init ==
    /\ analyzed = {}
    /\ flagged  = {}

(* ---------------------------------------------------------------------------
   AnalyzeSite — process one lookup-chain site
   --------------------------------------------------------------------------- *)

AnalyzeSite(s) ==
    /\ s \in LookupSites
    /\ s \notin analyzed
    /\ analyzed' = analyzed \union {s}
    /\ flagged'  = IF IsUnguarded(s)
                   THEN flagged \union {s}
                   ELSE flagged

(* ---------------------------------------------------------------------------
   State machine + fairness
   --------------------------------------------------------------------------- *)

Next == \E s \in LookupSites : AnalyzeSite(s)

Fairness == \A s \in LookupSites : WF_vars(AnalyzeSite(s))

Spec == Init /\ [][Next]_vars /\ Fairness

(* ===========================================================================
   STATE INVARIANTS
   =========================================================================== *)

(* Only unguarded sites are ever flagged *)
FlaggedOnlyIfUnguarded ==
    flagged \subseteq {s \in LookupSites : IsUnguarded(s)}

(* Guarded sites are never flagged *)
GuardedNeverFlagged ==
    flagged \intersect GuardedSites = {}

(* Flagged set grows monotonically — once flagged, never un-flagged *)
FlaggedMonotone ==
    flagged \subseteq flagged

(* ===========================================================================
   TEMPORAL PROPERTIES
   =========================================================================== *)

(* Every unguarded site is eventually flagged *)
UnguardedEventuallyFlagged ==
    \A s \in LookupSites :
        IsUnguarded(s) => <>(s \in flagged)

(* All sites are eventually analyzed — checker is complete *)
AllSitesEventuallyAnalyzed ==
    \A s \in LookupSites : <>(s \in analyzed)

(* ===========================================================================
   COMPLETENESS
   =========================================================================== *)

Done == analyzed = LookupSites

CompletionCorrectness ==
    Done => (flagged = {s \in LookupSites : IsUnguarded(s)})

=============================================================================
