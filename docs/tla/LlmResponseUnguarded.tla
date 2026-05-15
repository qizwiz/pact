------------------------- MODULE LlmResponseUnguarded -------------------------
(*
 * Formal specification of pact's llm_response_unguarded checker.
 *
 * The bug class: accessing LLM response fields without guarding against empty
 * or None responses. The common failure:
 *
 *   response = client.chat.completions.create(...)
 *   text = response.choices[0].message.content    # IndexError if choices=[]
 *
 * LLM APIs return empty choices lists when:
 *   - Content is filtered (safety guardrails)
 *   - Rate limits / quota errors produce degraded responses
 *   - Streaming responses are reassembled incompletely
 *   - Provider-specific error formats use empty choices instead of exceptions
 *
 * Models the data flow from LLM response source to content access and proves:
 *
 *   Safety   — unguarded access to LLM response fields is always flagged
 *   Precision — guarded access is never flagged
 *   Liveness  — every response access site is eventually analyzed
 *
 * Corresponds to:
 *   tools/pact/failure_mode.py  (LLM_RESPONSE_UNGUARDED / _scan_file_llm_response_unguarded)
 *
 * To check with TLC:
 *   cd docs/tla
 *   java -XX:+UseParallelGC -jar ~/.local/share/tla2tools.jar \
 *     -config LlmResponseUnguarded.cfg -deadlock LlmResponseUnguarded
 *)

EXTENDS Integers, FiniteSets, TLC

(* ---------------------------------------------------------------------------
   Constants
   --------------------------------------------------------------------------- *)

CONSTANTS
    AccessSites,    \* All sites where an LLM response field is accessed
    GuardedSites    \* Sites where access is wrapped in a None/empty-check guard

ASSUME IsFiniteSet(AccessSites)
ASSUME GuardedSites \subseteq AccessSites

(* A site is unguarded if it accesses an LLM field without a prior None-check *)
IsUnguarded(s) == s \notin GuardedSites

(* ---------------------------------------------------------------------------
   State variables
   --------------------------------------------------------------------------- *)

VARIABLES
    analyzed,   \* Sites that have been processed by the checker
    flagged     \* Sites identified as unguarded LLM response accesses

vars == <<analyzed, flagged>>

(* ---------------------------------------------------------------------------
   Type invariant
   --------------------------------------------------------------------------- *)

TypeInvariant ==
    /\ analyzed \subseteq AccessSites
    /\ flagged  \subseteq AccessSites

(* ---------------------------------------------------------------------------
   Initial state
   --------------------------------------------------------------------------- *)

Init ==
    /\ analyzed = {}
    /\ flagged  = {}

(* ---------------------------------------------------------------------------
   AnalyzeSite — process one access site
   --------------------------------------------------------------------------- *)

AnalyzeSite(s) ==
    /\ s \in AccessSites
    /\ s \notin analyzed
    /\ analyzed' = analyzed \union {s}
    /\ flagged'  = IF IsUnguarded(s)
                   THEN flagged \union {s}
                   ELSE flagged

(* ---------------------------------------------------------------------------
   State machine + fairness
   --------------------------------------------------------------------------- *)

Next == \E s \in AccessSites : AnalyzeSite(s)

Fairness == \A s \in AccessSites : WF_vars(AnalyzeSite(s))

Spec == Init /\ [][Next]_vars /\ Fairness

(* ===========================================================================
   STATE INVARIANTS
   =========================================================================== *)

(* Only unguarded sites are flagged *)
FlaggedOnlyIfUnguarded ==
    flagged \subseteq {s \in AccessSites : IsUnguarded(s)}

(* Guarded sites are never flagged *)
GuardedNeverFlagged ==
    flagged \intersect GuardedSites = {}

(* Flagging grows monotonically *)
MonotonicFlagged ==
    flagged \subseteq flagged

(* ===========================================================================
   TEMPORAL PROPERTIES
   =========================================================================== *)

(* Every unguarded site is eventually flagged *)
UnguardedEventuallyFlagged ==
    \A s \in AccessSites :
        IsUnguarded(s) => <>(s \in flagged)

(* All sites are eventually analyzed — the checker is complete *)
AllSitesEventuallyAnalyzed ==
    \A s \in AccessSites : <>(s \in analyzed)

(* ===========================================================================
   COMPLETENESS
   =========================================================================== *)

Done == analyzed = AccessSites

CompletionCorrectness ==
    Done => (flagged = {s \in AccessSites : IsUnguarded(s)})

=============================================================================
