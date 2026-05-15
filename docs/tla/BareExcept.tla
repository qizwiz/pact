----------------------------- MODULE BareExcept -----------------------------
(*
 * Formal specification of pact's bare_except checker.
 *
 * The bug class: bare `except:` or `except Exception: pass` swallows *all*
 * exceptions, including BaseException subclasses (KeyboardInterrupt, SystemExit,
 * GeneratorExit) that callers and runtimes rely on for clean shutdown.
 *
 * Models exception propagation through a handler chain and proves:
 *
 *   Safety   — critical system exceptions (BaseException-only) always propagate
 *              unless the handler explicitly re-raises or re-wraps them
 *   Liveness — every bare handler is eventually detected and flagged
 *
 * Corresponds to:
 *   tools/pact/failure_mode.py  (_scan_file_bare_except)
 *
 * To check with TLC:
 *   cd docs/tla
 *   java -XX:+UseParallelGC -jar ~/.local/share/tla2tools.jar \
 *     -config BareExcept.cfg -deadlock BareExcept
 *)

EXTENDS Integers, FiniteSets, TLC

(* ---------------------------------------------------------------------------
   Constants
   --------------------------------------------------------------------------- *)

CONSTANTS
    ExceptionTypes,     \* All exception types that can be raised in this scope
    BaseOnlyExceptions, \* Exceptions only caught by bare `except:` (KeyboardInterrupt etc.)
    HandlerKind         \* "bare" | "exception_pass" | "specific" | "reraise"

ASSUME IsFiniteSet(ExceptionTypes)
ASSUME BaseOnlyExceptions \subseteq ExceptionTypes
ASSUME HandlerKind \in {"bare", "exception_pass", "specific", "reraise"}

(* A handler is "swallowing" if it catches everything and doesn't re-raise.
   bare except: and except Exception: pass are the two flagged forms. *)
IsSwallowingHandler ==
    HandlerKind \in {"bare", "exception_pass"}

(* ---------------------------------------------------------------------------
   State variables
   --------------------------------------------------------------------------- *)

VARIABLES
    raised,         \* Set of exception types that have been raised
    propagated,     \* Set of exception types that propagated past the handler
    swallowed,      \* Set of exception types silently swallowed
    handler_flagged \* Whether pact flagged the handler

vars == <<raised, propagated, swallowed, handler_flagged>>

(* ---------------------------------------------------------------------------
   Type invariant
   --------------------------------------------------------------------------- *)

TypeInvariant ==
    /\ raised          \subseteq ExceptionTypes
    /\ propagated      \subseteq ExceptionTypes
    /\ swallowed       \subseteq ExceptionTypes
    /\ handler_flagged \in {TRUE, FALSE}
    \* Disjointness: an exception is either propagated, swallowed, or still pending
    /\ propagated \intersect swallowed = {}

(* ---------------------------------------------------------------------------
   Initial state
   --------------------------------------------------------------------------- *)

Init ==
    /\ raised          = {}
    /\ propagated      = {}
    /\ swallowed       = {}
    /\ handler_flagged = FALSE

(* ---------------------------------------------------------------------------
   Raise — an exception occurs in the try-body
   --------------------------------------------------------------------------- *)

RaiseException(e) ==
    /\ e \in ExceptionTypes
    /\ e \notin raised
    /\ raised'          = raised \union {e}
    /\ UNCHANGED <<propagated, swallowed, handler_flagged>>

(* ---------------------------------------------------------------------------
   Handle — the except clause processes raised exceptions.

   "bare" and "exception_pass" swallow everything.
   "specific" only catches non-BaseOnly exceptions.
   "reraise" catches and re-raises (propagates all).
   --------------------------------------------------------------------------- *)

HandleExceptions ==
    /\ raised /= {}
    /\ LET pending == raised \ (propagated \union swallowed)
       IN
       /\ pending /= {}
       /\ IF IsSwallowingHandler
          THEN
            /\ swallowed'      = swallowed \union pending
            /\ propagated'     = propagated
            /\ handler_flagged'= TRUE   \* Checker fires: swallowing handler detected
          ELSE IF HandlerKind = "specific"
          THEN
            \* Specific handler: BaseOnlyExceptions are not caught, they propagate
            /\ propagated'     = propagated \union (pending \intersect BaseOnlyExceptions)
            /\ swallowed'      = swallowed \union (pending \ BaseOnlyExceptions)
            /\ handler_flagged'= FALSE
          ELSE \* "reraise"
            /\ propagated'     = propagated \union pending
            /\ swallowed'      = swallowed
            /\ handler_flagged'= FALSE
    /\ UNCHANGED raised

(* ---------------------------------------------------------------------------
   State machine
   --------------------------------------------------------------------------- *)

Next ==
    \/ \E e \in ExceptionTypes : RaiseException(e)
    \/ HandleExceptions

Fairness ==
    /\ \A e \in ExceptionTypes : WF_vars(RaiseException(e))
    /\ WF_vars(HandleExceptions)

Spec == Init /\ [][Next]_vars /\ Fairness

(* ===========================================================================
   SAFETY: Critical system exceptions must not be silently swallowed
   ===========================================================================
 *
 * If a BaseException-only exception (KeyboardInterrupt, SystemExit) is raised
 * AND the handler is a swallowing kind, the checker MUST flag it.
 *
 * In other words: no state where a critical exception is in `swallowed` without
 * the handler being flagged.
 *)
NoCriticalExceptionSilenced ==
    [](
        (swallowed \intersect BaseOnlyExceptions /= {})
        => handler_flagged
    )

(* ===========================================================================
   SAFETY: Swallowing handler is always flagged when it acts
   =========================================================================== *)

SwallowingHandlerAlwaysFlagged ==
    [](
        (IsSwallowingHandler /\ swallowed /= {})
        => handler_flagged
    )

(* ===========================================================================
   SAFETY: Specific handlers with re-raise don't get flagged
   =========================================================================== *)

PreciseHandlerNotFlagged ==
    [](
        (HandlerKind \in {"specific", "reraise"})
        => ~handler_flagged
    )

(* ===========================================================================
   LIVENESS: If there's a swallowing handler and exceptions are raised,
             flagging eventually happens
   =========================================================================== *)

SwallowingEventuallyDetected ==
    (IsSwallowingHandler /\ raised /= {})
    => <>(handler_flagged)

(* ===========================================================================
   COMPLETENESS: When handling is done, flag state reflects handler kind
   =========================================================================== *)

Done == raised /= {} /\ raised \subseteq (propagated \union swallowed)

CompletionCorrectness ==
    Done => (handler_flagged <=> IsSwallowingHandler)

=============================================================================
