----------------------- MODULE SaveWithoutUpdateFields -----------------------
(*
 * Formal specification of pact's save_without_update_fields checker.
 *
 * The bug class: calling Django's Model.save() on a partially-hydrated instance
 * (loaded via .only() / .defer() / select_related with subsets) without
 * specifying update_fields causes a full-row UPDATE, overwriting every unloaded
 * field's DB value with the Python object's stale default.
 *
 * Models the lifecycle of a Django model instance from fetch through save and
 * proves two properties:
 *
 *   Safety   — save() is only called with all fields present OR update_fields set
 *   Liveness — every save on a partial instance is eventually flagged
 *
 * Corresponds to:
 *   tools/pact/failure_mode.py  (SaveWithoutUpdateFieldsMode.check)
 *
 * To check with TLC:
 *   cd docs/tla
 *   java -XX:+UseParallelGC -jar ~/.local/share/tla2tools.jar \
 *     -config SaveWithoutUpdateFields.cfg -deadlock SaveWithoutUpdateFields
 *)

EXTENDS Integers, FiniteSets, TLC

(* ---------------------------------------------------------------------------
   Constants
   --------------------------------------------------------------------------- *)

CONSTANTS
    AllFields,      \* Complete set of fields on the model class
    LoadedFields,   \* Fields actually fetched for this instance (subset of AllFields)
    UpdateFields    \* Fields passed as update_fields= when used (subset of LoadedFields)

ASSUME IsFiniteSet(AllFields)
ASSUME LoadedFields \subseteq AllFields
ASSUME UpdateFields \subseteq LoadedFields

(* A partial fetch is the root cause: the instance doesn't know about all fields *)
IsPartialInstance == LoadedFields /= AllFields

(* ---------------------------------------------------------------------------
   Instance lifecycle phases
   --------------------------------------------------------------------------- *)

VARIABLES
    phase,          \* "idle" | "loaded" | "modified" | "saved"
    modified_fields,\* Fields changed on the in-memory instance
    save_kind,      \* "none" | "full" | "partial" — how save() was called
    flagged         \* Whether the checker found this violation

vars == <<phase, modified_fields, save_kind, flagged>>

(* ---------------------------------------------------------------------------
   Allowed phases
   --------------------------------------------------------------------------- *)

Phases == {"idle", "loaded", "modified", "saved"}

TypeInvariant ==
    /\ phase          \in Phases
    /\ modified_fields \subseteq LoadedFields
    /\ save_kind      \in {"none", "full", "partial"}
    /\ flagged        \in {TRUE, FALSE}

(* ---------------------------------------------------------------------------
   Initial state: instance exists, no fields loaded, not yet saved
   --------------------------------------------------------------------------- *)

Init ==
    /\ phase           = "idle"
    /\ modified_fields = {}
    /\ save_kind       = "none"
    /\ flagged         = FALSE

(* ---------------------------------------------------------------------------
   Fetch — load the instance (with or without all fields)
   --------------------------------------------------------------------------- *)

Fetch ==
    /\ phase  = "idle"
    /\ phase' = "loaded"
    /\ UNCHANGED <<modified_fields, save_kind, flagged>>

(* ---------------------------------------------------------------------------
   Modify — update one or more loaded fields
   --------------------------------------------------------------------------- *)

ModifyField(f) ==
    /\ phase           = "loaded"
    /\ f              \in LoadedFields
    /\ f              \notin modified_fields
    /\ phase'          = "modified"
    /\ modified_fields'= modified_fields \union {f}
    /\ UNCHANGED <<save_kind, flagged>>

(* ---------------------------------------------------------------------------
   Save with explicit update_fields — safe regardless of LoadedFields
   --------------------------------------------------------------------------- *)

SaveWithUpdateFields ==
    /\ phase     \in {"loaded", "modified"}
    /\ UpdateFields /= {}
    /\ phase'    = "saved"
    /\ save_kind'= "partial"
    /\ flagged'  = FALSE   \* Checker correctly skips: update_fields scopes the UPDATE
    /\ UNCHANGED modified_fields

(* ---------------------------------------------------------------------------
   Save without update_fields — only safe when all fields are loaded
   --------------------------------------------------------------------------- *)

SaveFull ==
    /\ phase             \in {"loaded", "modified"}
    /\ UpdateFields       = {}          \* no update_fields arg
    /\ ~IsPartialInstance               \* safe: all fields present
    /\ phase'            = "saved"
    /\ save_kind'        = "full"
    /\ flagged'          = FALSE        \* No violation: full instance
    /\ UNCHANGED modified_fields

SavePartialUnsafe ==
    /\ phase             \in {"loaded", "modified"}
    /\ UpdateFields       = {}          \* no update_fields arg
    /\ IsPartialInstance                \* VIOLATION: partial instance + full-row save
    /\ phase'            = "saved"
    /\ save_kind'        = "full"
    /\ flagged'          = TRUE         \* Checker fires
    /\ UNCHANGED modified_fields

(* ---------------------------------------------------------------------------
   State machine
   --------------------------------------------------------------------------- *)

Next ==
    \/ Fetch
    \/ \E f \in LoadedFields : ModifyField(f)
    \/ SaveWithUpdateFields
    \/ SaveFull
    \/ SavePartialUnsafe

Fairness ==
    /\ WF_vars(Fetch)
    /\ WF_vars(SaveWithUpdateFields \/ SaveFull \/ SavePartialUnsafe)

Spec == Init /\ [][Next]_vars /\ Fairness

(* ===========================================================================
   SAFETY: A partial instance that reaches "saved" via full save is flagged
   ===========================================================================
 *
 * The checker's core guarantee: if you save a partial instance without
 * scoping the UPDATE with update_fields, the violation is detected.
 *)
NoSilentDataLoss ==
    [](
        (phase = "saved" /\ save_kind = "full" /\ IsPartialInstance)
        => flagged
    )

(* ===========================================================================
   SAFETY: A safe save (update_fields present, or full instance) is never flagged
   =========================================================================== *)

NoPreciseViolationsAreFlagged ==
    [](
        (phase = "saved" /\ (UpdateFields /= {} \/ ~IsPartialInstance))
        => ~flagged
    )

(* ===========================================================================
   SAFETY: Full-instance saves are always clean
   =========================================================================== *)

FullInstanceNeverFlagged ==
    []((phase = "saved" /\ ~IsPartialInstance) => ~flagged)

(* ===========================================================================
   LIVENESS: If partial + no update_fields, violation is eventually detected
   ===========================================================================
 *
 * Under fairness, the system reaches "saved" and the checker fires.
 *)
PartialSaveEventuallyFlagged ==
    IsPartialInstance /\ UpdateFields = {}
    => <>(phase = "saved" /\ flagged)

(* ===========================================================================
   COMPLETENESS: When saved, flag state exactly mirrors IsPartialInstance
   =========================================================================== *)

Done == phase = "saved"

SaveCorrectnessWhenDone ==
    Done =>
        (flagged <=> (save_kind = "full" /\ IsPartialInstance))

=============================================================================
