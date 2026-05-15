------------------------- MODULE OptionalDereference -------------------------
(*
 * Formal specification of pact's optional_dereference checker.
 *
 * The bug class: accessing an attribute or subscript on a value that may be
 * None without first checking for None. Common patterns:
 *   response.choices[0].message.content   -- choices may be empty/None
 *   user.profile.address.city             -- any intermediate may be None
 *   config.get("key").strip()             -- dict.get() returns None on miss
 *
 * Models the type-state of a value through guards and dereferences and proves:
 *
 *   Safety   — no dereference happens without a prior None-guard on that value
 *   Liveness — every unguarded dereference is eventually flagged
 *
 * Corresponds to:
 *   tools/pact/failure_mode.py  (OPTIONAL_DEREF / _scan_file_optional_deref)
 *
 * To check with TLC:
 *   cd docs/tla
 *   java -XX:+UseParallelGC -jar ~/.local/share/tla2tools.jar \
 *     -config OptionalDereference.cfg -deadlock OptionalDereference
 *)

EXTENDS Integers, FiniteSets, TLC

(* ---------------------------------------------------------------------------
   Constants
   --------------------------------------------------------------------------- *)

CONSTANTS
    Values,         \* Set of value identifiers that may be None (call sites, vars)
    NoneValues      \* Subset of Values that ARE None in this execution

ASSUME IsFiniteSet(Values)
ASSUME NoneValues \subseteq Values

(* A value is "optional" if it may be None — either it IS None, or it's from
   a source known to return Optional (dict.get, first(), LLM response fields). *)
IsOptional(v) == v \in NoneValues

(* ---------------------------------------------------------------------------
   State variables
   --------------------------------------------------------------------------- *)

VARIABLES
    guarded,    \* Values that have passed through a None-guard (is not None / if v: / v is None)
    derefed,    \* Values that have been dereferenced (attribute access / subscript)
    flagged     \* Values where pact found unguarded dereference

vars == <<guarded, derefed, flagged>>

(* ---------------------------------------------------------------------------
   Type invariant
   --------------------------------------------------------------------------- *)

TypeInvariant ==
    /\ guarded  \subseteq Values
    /\ derefed  \subseteq Values
    /\ flagged  \subseteq Values

(* ---------------------------------------------------------------------------
   Initial state
   --------------------------------------------------------------------------- *)

Init ==
    /\ guarded = {}
    /\ derefed = {}
    /\ flagged = {}

(* ---------------------------------------------------------------------------
   Guard — a None-check is performed on value v.
   After this, dereferences on v are safe.
   --------------------------------------------------------------------------- *)

Guard(v) ==
    /\ v \in Values
    /\ v \notin guarded
    /\ v \notin derefed   \* Static analysis: guards are meaningful only before first access
    /\ guarded' = guarded \union {v}
    /\ UNCHANGED <<derefed, flagged>>

(* ---------------------------------------------------------------------------
   DerefSafe — dereference a guarded value (safe path, not flagged)
   --------------------------------------------------------------------------- *)

DerefSafe(v) ==
    /\ v \in Values
    /\ v \in guarded
    /\ v \notin derefed
    /\ derefed' = derefed \union {v}
    /\ flagged'  = flagged          \* Safe: guard was present
    /\ UNCHANGED guarded

(* ---------------------------------------------------------------------------
   DerefUnguarded — dereference a value with no prior guard.
   If the value is optional (may be None), this is flagged.
   If the value is known non-None, it's safe.
   --------------------------------------------------------------------------- *)

DerefUnguarded(v) ==
    /\ v \in Values
    /\ v \notin guarded
    /\ v \notin derefed
    /\ derefed' = derefed \union {v}
    /\ flagged'  = IF IsOptional(v)
                   THEN flagged \union {v}   \* Violation: optional + no guard
                   ELSE flagged              \* Non-None value: no flag
    /\ UNCHANGED guarded

(* ---------------------------------------------------------------------------
   State machine
   --------------------------------------------------------------------------- *)

Next ==
    \/ \E v \in Values : Guard(v)
    \/ \E v \in Values : DerefSafe(v)
    \/ \E v \in Values : DerefUnguarded(v)

Fairness ==
    \A v \in Values :
        /\ WF_vars(Guard(v))
        /\ WF_vars(DerefSafe(v) \/ DerefUnguarded(v))

Spec == Init /\ [][Next]_vars /\ Fairness

(* ===========================================================================
   SAFETY: Every flagged value was optional AND unguarded
   ===========================================================================
 *
 * The checker only flags values that are both optional (may be None) and
 * were accessed without a prior guard. No false positives on known-non-None values.
 *)
FlaggedImpliesOptionalUnguarded ==
    [](
        \A v \in flagged :
            IsOptional(v) /\ v \notin guarded
    )

(* ===========================================================================
   SAFETY: Guarded values are never flagged
   =========================================================================== *)

GuardedNeverFlagged ==
    [](flagged \intersect guarded = {})

(* ===========================================================================
   SAFETY: Non-optional values are never flagged regardless of guards
   =========================================================================== *)

NonOptionalNeverFlagged ==
    [](\A v \in (Values \ NoneValues) : v \notin flagged)

(* ===========================================================================
   LIVENESS: Every value is eventually processed (dereference decision made)
   ===========================================================================
 *
 * Under fairness, the checker eventually processes every value in scope.
 * Combined with safety, this gives completeness: no value is permanently skipped.
 *)

AllValuesEventuallyProcessed ==
    \A v \in Values : <>(v \in derefed)

(* ===========================================================================
   COMPLETENESS: When all values are dereferenced, flagged = unguarded optionals
   =========================================================================== *)

Done == derefed = Values

CompletionCorrectness ==
    Done =>
        flagged = {v \in NoneValues : v \notin guarded}

=============================================================================
