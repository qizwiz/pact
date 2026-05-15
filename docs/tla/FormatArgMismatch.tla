--------------------------- MODULE FormatArgMismatch ---------------------------
(*
 * Formal specification of pact's format_arg_mismatch checker.
 *
 * The bug class: a str.format() call provides a different number of positional
 * arguments than the format string expects.  The common failure patterns:
 *
 *   "{} {}".format(a)           -- too few args  → IndexError at runtime
 *   "{name}".format(a, b)       -- extra positional args (usually a logic error)
 *   "%(k)s" % {"wrong_key": v}  -- wrong key in % formatting → KeyError
 *
 * A format call is "matched" when:
 *   - every positional placeholder {N} or {} is covered by a positional arg, AND
 *   - every named placeholder {name} is covered by a keyword arg of the same name
 *
 * Unmatched calls are guaranteed to raise IndexError or KeyError at runtime.
 *
 * Models the checker lifecycle over all format call sites and proves:
 *
 *   Safety    — only mismatched format calls are flagged
 *   Precision — matched format calls are never flagged
 *   Liveness  — every format call site is eventually analyzed
 *
 * Corresponds to:
 *   tools/pact/failure_mode.py  (_check_format_arg_mismatch / FORMAT_ARG_MISMATCH)
 *
 * To check with TLC:
 *   cd docs/tla
 *   java -XX:+UseParallelGC -jar ~/.local/share/tla2tools.jar \
 *     -config FormatArgMismatch.cfg -deadlock FormatArgMismatch
 *)

EXTENDS Integers, FiniteSets, TLC

(* ---------------------------------------------------------------------------
   Constants
   --------------------------------------------------------------------------- *)

CONSTANTS
    FormatCalls,    \* All str.format() / % call sites in the codebase
    MatchedCalls    \* Calls where args exactly satisfy the format string

ASSUME IsFiniteSet(FormatCalls)
ASSUME MatchedCalls \subseteq FormatCalls

IsMismatch(s) == s \notin MatchedCalls

(* ---------------------------------------------------------------------------
   State variables
   --------------------------------------------------------------------------- *)

VARIABLES
    analyzed,   \* Format calls processed by the checker
    flagged     \* Calls flagged as arg-count / arg-name mismatches

vars == <<analyzed, flagged>>

(* ---------------------------------------------------------------------------
   Type invariant
   --------------------------------------------------------------------------- *)

TypeInvariant ==
    /\ analyzed \subseteq FormatCalls
    /\ flagged  \subseteq FormatCalls

(* ---------------------------------------------------------------------------
   Initial state
   --------------------------------------------------------------------------- *)

Init ==
    /\ analyzed = {}
    /\ flagged  = {}

(* ---------------------------------------------------------------------------
   AnalyzeCall — process one format call site
   --------------------------------------------------------------------------- *)

AnalyzeCall(s) ==
    /\ s \in FormatCalls
    /\ s \notin analyzed
    /\ analyzed' = analyzed \union {s}
    /\ flagged'  = IF IsMismatch(s)
                   THEN flagged \union {s}
                   ELSE flagged

(* ---------------------------------------------------------------------------
   State machine + fairness
   --------------------------------------------------------------------------- *)

Next == \E s \in FormatCalls : AnalyzeCall(s)

Fairness == \A s \in FormatCalls : WF_vars(AnalyzeCall(s))

Spec == Init /\ [][Next]_vars /\ Fairness

(* ===========================================================================
   STATE INVARIANTS
   =========================================================================== *)

(* Only mismatched format calls are flagged *)
FlaggedOnlyIfMismatch ==
    flagged \subseteq {s \in FormatCalls : IsMismatch(s)}

(* Matched format calls are never flagged *)
MatchedNeverFlagged ==
    flagged \intersect MatchedCalls = {}

(* ===========================================================================
   TEMPORAL PROPERTIES
   =========================================================================== *)

(* Every mismatched call is eventually flagged *)
MismatchEventuallyFlagged ==
    \A s \in FormatCalls :
        IsMismatch(s) => <>(s \in flagged)

(* All format call sites are eventually analyzed *)
AllCallsEventuallyAnalyzed ==
    \A s \in FormatCalls : <>(s \in analyzed)

(* ===========================================================================
   COMPLETENESS
   =========================================================================== *)

Done == analyzed = FormatCalls

CompletionCorrectness ==
    Done => (flagged = {s \in FormatCalls : IsMismatch(s)})

=============================================================================
