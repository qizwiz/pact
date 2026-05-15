--------------------------- MODULE MutableDefaultArg ---------------------------
(*
 * Formal specification of pact's mutable_default_arg checker.
 *
 * The bug class: a function defined with a mutable default argument (list,
 * dict, set, or their constructor equivalents list()/dict()/set()) shares
 * the SAME object across all calls. Mutations persist between invocations:
 *
 *   def accumulate(items=[]):   # ONE list, created once at def time
 *       items.append("new")
 *       return items
 *
 *   accumulate()  => ["new"]
 *   accumulate()  => ["new", "new"]   # surprise!
 *
 * Models the lifecycle of a function definition and call history, proving:
 *
 *   Safety   — flagging only occurs when the default IS mutable AND IS mutated
 *   Liveness — every violation is eventually detected
 *
 * Corresponds to:
 *   tools/pact/failure_mode.py  (_scan_file_mutable_defaults)
 *
 * Fix: _is_mutable_default() covers ast.List/Dict/Set literals AND
 *      list()/dict()/set() Call nodes (false-negative fixed 2026-05-15
 *      after Hypothesis property-based test discovered the gap).
 *
 * To check with TLC:
 *   cd docs/tla
 *   java -XX:+UseParallelGC -jar ~/.local/share/tla2tools.jar \
 *     -config MutableDefaultArg.cfg -deadlock MutableDefaultArg
 *)

EXTENDS Integers, FiniteSets, TLC

(* ---------------------------------------------------------------------------
   Constants
   --------------------------------------------------------------------------- *)

CONSTANTS
    Params,         \* Set of parameter names in the function signature
    MutableParams,  \* Params whose default value is mutable (list/dict/set/constructor)
    MutatedParams   \* Params that are mutated inside the function body

ASSUME IsFiniteSet(Params)
ASSUME MutableParams \subseteq Params
ASSUME MutatedParams \subseteq Params

(* A parameter is a violation iff: default is mutable AND body mutates it *)
IsViolation(p) ==
    /\ p \in MutableParams
    /\ p \in MutatedParams

(* ---------------------------------------------------------------------------
   State variables
   --------------------------------------------------------------------------- *)

VARIABLES
    checked,    \* Params whose default has been analyzed
    flagged     \* Params where checker found the violation

vars == <<checked, flagged>>

(* ---------------------------------------------------------------------------
   Type invariant
   --------------------------------------------------------------------------- *)

TypeInvariant ==
    /\ checked \subseteq Params
    /\ flagged \subseteq Params

(* ---------------------------------------------------------------------------
   Initial state
   --------------------------------------------------------------------------- *)

Init ==
    /\ checked = {}
    /\ flagged = {}

(* ---------------------------------------------------------------------------
   CheckParam — analyze one parameter's default + body usage
   --------------------------------------------------------------------------- *)

CheckParam(p) ==
    /\ p \in Params
    /\ p \notin checked
    /\ checked' = checked \union {p}
    /\ flagged'  = IF IsViolation(p)
                   THEN flagged \union {p}
                   ELSE flagged

(* ---------------------------------------------------------------------------
   State machine + fairness
   --------------------------------------------------------------------------- *)

Next == \E p \in Params : CheckParam(p)

Fairness == \A p \in Params : WF_vars(CheckParam(p))

Spec == Init /\ [][Next]_vars /\ Fairness

(* ===========================================================================
   STATE INVARIANTS (checked in every reachable state by TLC)
   =========================================================================== *)

(* Only real violations are flagged — conjunctive requirement *)
FlaggedOnlyIfViolation ==
    flagged \subseteq {p \in Params : IsViolation(p)}

(* Immutable defaults are never flagged *)
ImmutableNeverFlagged ==
    \A p \in (Params \ MutableParams) : p \notin flagged

(* Mutable but read-only defaults are never flagged.
   Design: require BOTH mutable default AND in-body mutation. *)
ReadOnlyNeverFlagged ==
    \A p \in (MutableParams \ MutatedParams) : p \notin flagged

(* ===========================================================================
   TEMPORAL PROPERTIES (verified over complete state space)
   =========================================================================== *)

(* Violations grow monotonically — no un-flagging *)
MonotonicFlagged ==
    [][flagged \subseteq flagged']_flagged

(* Every violation is eventually detected *)
ViolationsEventuallyFlagged ==
    \A p \in Params :
        IsViolation(p) => <>(p \in flagged)

(* ===========================================================================
   COMPLETENESS: When all params are checked, flagged = exactly violations
   =========================================================================== *)

Done == checked = Params

CompletionCorrectness ==
    Done => (flagged = {p \in Params : IsViolation(p)})

=============================================================================
