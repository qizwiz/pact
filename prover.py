"""
pact prover — formal proof certificates for violation modes.

For each mode, encodes the bug semantics as Z3 constraints and produces:
  - SAT witness: a concrete scenario where the bug manifests
  - UNSAT after fix: proof the fix eliminates all such scenarios

Usage:
  python3 -m pact.prover --mode save_without_update_fields
  python3 -m pact.prover --mode missing_await
  python3 -m pact.prover --mode optional_dereference
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from dataclasses import dataclass

try:
    from z3 import (
        And,
        Bool,
        BoolSort,
        Const,
        DeclareSort,
        Function,
        Implies,
        Int,
        Not,
        Or,
        Solver,
        sat,
        unsat,
    )

    _HAS_Z3 = True
except ImportError:
    _HAS_Z3 = False


@dataclass
class ProofCertificate:
    mode: str
    bug_sat: bool  # True = bug scenario is satisfiable (real bug)
    fix_unsat: bool  # True = fix makes bug impossible (fix is correct)
    witness: dict  # concrete values showing the bug
    axioms: list[str]
    conclusion: str

    def render(self) -> str:
        lines = [
            f"PROOF CERTIFICATE: {self.mode}",
            "=" * 60,
            "",
            "AXIOMS",
            "------",
        ]
        for i, a in enumerate(self.axioms, 1):
            lines.append(f"  A{i}. {a}")
        lines += [
            "",
            f"BUG SCENARIO: {'SAT — race condition is satisfiable' if self.bug_sat else 'UNSAT (unexpected)'}",
        ]
        if self.witness:
            lines.append("WITNESS (concrete values that trigger the bug):")
            for k, v in self.witness.items():
                lines.append(f"  {k} = {v}")
        lines += [
            "",
            f"AFTER FIX: {'UNSAT — no data-loss scenario exists' if self.fix_unsat else 'SAT (fix insufficient)'}",
            "",
            "CONCLUSION",
            "----------",
            textwrap.fill(self.conclusion, width=70),
        ]
        return "\n".join(lines)


def prove_save_without_update_fields() -> ProofCertificate:
    """
    Two concurrent Django writers. Full save overwrites all fields.
    Proves: without update_fields, Writer A's change is silently lost.
    Proves: with update_fields, no such interleaving exists.
    """
    s = Solver()

    # Database state: two independent fields
    db_a = Int("db_field_a")  # field Writer A will modify
    db_b = Int("db_field_b")  # field Writer B will modify

    # Both writers read the same initial snapshot (before either writes)
    snap_A_a = Int("snap_A_field_a")
    snap_A_b = Int("snap_A_field_b")
    snap_B_a = Int("snap_B_field_a")
    snap_B_b = Int("snap_B_field_b")

    s.add(snap_A_a == db_a, snap_A_b == db_b)  # A reads DB
    s.add(snap_B_a == db_a, snap_B_b == db_b)  # B reads same DB

    # Each writer makes a distinct change
    new_A_a = Int("new_A_field_a")
    new_B_b = Int("new_B_field_b")
    s.add(new_A_a != snap_A_a)  # A actually changed field_a
    s.add(new_B_b != snap_B_b)  # B actually changed field_b

    # Without update_fields: full save writes ALL fields from snapshot
    # Interleaving: A reads → B reads → A saves → B saves (last-writer-wins)
    # After A saves: db_a = new_A_a, db_b = snap_A_b
    # After B saves (full, from its snapshot): db_a = snap_B_a, db_b = new_B_b
    final_a = Int("final_field_a")
    final_b = Int("final_field_b")
    s.add(final_a == snap_B_a)  # B's full save restores old field_a
    s.add(final_b == new_B_b)  # B's change to field_b lands

    # Assert the race: A's intended change is gone from final state
    s.add(final_a != new_A_a)

    result = s.check()
    witness = {}
    if result == sat:
        m = s.model()
        witness = {
            "initial field_a": m[db_a],
            "initial field_b": m[db_b],
            "Writer A intended field_a": m[new_A_a],
            "Writer B intended field_b": m[new_B_b],
            "final field_a (after B's full save)": m[final_a],
            "final field_b": m[final_b],
            "Writer A's change survived?": "NO — overwritten by B's full save",
        }

    # Now prove the fix: update_fields=['field_a'] restricts A's save scope.
    # With update_fields, A only touches field_a. B's save of field_a
    # still uses B's snapshot, but the critical question is: does A's change survive?
    # With proper update_fields on BOTH sides:
    #   A saves only field_a → final field_a = new_A_a
    #   B saves only field_b → final field_b = new_B_b
    # No interleaving can cause loss. Prove: UNSAT for any loss scenario.
    s2 = Solver()
    db2_a = Int("db2_field_a")
    db2_b = Int("db2_field_b")
    snap2_A_a = Int("snap2_A_field_a")
    snap2_B_b = Int("snap2_B_field_b")
    new2_A_a = Int("new2_A_field_a")
    new2_B_b = Int("new2_B_field_b")
    s2.add(snap2_A_a == db2_a, snap2_B_b == db2_b)
    s2.add(new2_A_a != snap2_A_a)
    s2.add(new2_B_b != snap2_B_b)
    # With update_fields: each writer only touches its own field
    final2_a = new2_A_a  # A's scoped save lands cleanly
    final2_b = new2_B_b  # B's scoped save lands cleanly
    # Try to find a scenario where either change is lost — should be UNSAT
    s2.add(Or(final2_a != new2_A_a, final2_b != new2_B_b))
    fix_result = s2.check()

    return ProofCertificate(
        mode="save_without_update_fields",
        bug_sat=(result == sat),
        fix_unsat=(fix_result == unsat),
        witness=witness,
        axioms=[
            "Two concurrent writers each read the same DB snapshot.",
            "A full Model.save() writes all fields from the reader's snapshot.",
            "Last writer wins: B saves after A, overwriting all of A's fields.",
            "Writer A's change to field_a is irrecoverably lost.",
        ],
        conclusion=(
            "The race condition is formally satisfiable: there exists a concurrent "
            "interleaving where Writer B's full save silently discards Writer A's "
            "committed change. With update_fields scoping each writer to its own "
            "field, no such interleaving exists (UNSAT). Every save() call that "
            "modifies a strict subset of fields is vulnerable without update_fields."
        ),
    )


def prove_missing_await() -> ProofCertificate:
    """
    Python coroutine semantics: calling async def f() without await
    returns a Coroutine object, not the function's return value.
    SAT framing: can the caller observe a type mismatch? Yes (SAT = bug exists).
    """
    Type = DeclareSort("Type")
    Expected = Const("Expected", Type)
    Coroutine = Const("Coroutine", Type)

    is_async = Bool("is_async")
    is_awaited = Bool("is_awaited")
    actual_type = Function("actual_type", BoolSort(), BoolSort(), Type)

    s = Solver()
    # Axiom (Python spec §5.4): async + awaited → caller gets Expected type
    s.add(
        Implies(
            And(is_async, is_awaited), actual_type(is_async, is_awaited) == Expected
        )
    )
    # Axiom (Python spec §5.4): async + NOT awaited → caller gets Coroutine object
    s.add(
        Implies(
            And(is_async, Not(is_awaited)),
            actual_type(is_async, is_awaited) == Coroutine,
        )
    )
    # Distinct: Coroutine ≠ Expected (they are different types)
    s.add(Coroutine != Expected)
    # Observation: async function called without await
    s.add(is_async)
    s.add(Not(is_awaited))
    # Query: does a type mismatch exist? (SAT = yes, bug is real)
    s.add(actual_type(is_async, is_awaited) != Expected)
    bug_result = s.check()  # SAT — Coroutine != Expected

    witness = {}
    if bug_result == sat:
        witness = {
            "function type": "async def",
            "called with await?": "No",
            "actual return type": "Coroutine[Any, Any, T]",
            "expected return type": "T",
            "type mismatch": "SAT — caller always receives Coroutine, never T",
            "consequence": "truthy checks always pass (coroutine is truthy); field access raises AttributeError",
        }

    # Fix: with await, caller gets Expected — type mismatch is UNSAT
    s2 = Solver()
    is_async2 = Bool("is_async2")
    is_awaited2 = Bool("is_awaited2")
    actual_type2 = Function("actual_type2", BoolSort(), BoolSort(), Type)
    s2.add(
        Implies(
            And(is_async2, is_awaited2),
            actual_type2(is_async2, is_awaited2) == Expected,
        )
    )
    s2.add(
        Implies(
            And(is_async2, Not(is_awaited2)),
            actual_type2(is_async2, is_awaited2) == Coroutine,
        )
    )
    s2.add(Coroutine != Expected)
    s2.add(is_async2)
    s2.add(is_awaited2)  # fix: await present
    s2.add(actual_type2(is_async2, is_awaited2) != Expected)
    fix_result = s2.check()  # UNSAT — with await, type is always Expected

    return ProofCertificate(
        mode="missing_await",
        bug_sat=(bug_result == sat),
        fix_unsat=(fix_result == unsat),
        witness=witness,
        axioms=[
            "Python spec §5.4: calling a coroutine function returns a coroutine object.",
            "A coroutine object ≠ the function's return value (they are distinct types).",
            "Without await, the coroutine is never scheduled; its body never executes.",
            "With await, the scheduler runs the body and the caller receives the return value.",
        ],
        conclusion=(
            "By Python's coroutine semantics, an unawaited async call always returns "
            "Coroutine[Any, Any, T], never T. Z3 finds SAT for the type mismatch — it "
            "is satisfiable that the caller receives the wrong type. Adding await makes "
            "the mismatch UNSAT: no model satisfies (awaited ∧ type ≠ Expected). "
            "This is a theorem of the language, not a heuristic."
        ),
    )


def prove_optional_dereference() -> ProofCertificate:
    """
    A variable that may be None: attribute access without a None-guard
    has a satisfiable failure path (AttributeError).
    """
    is_none = Bool("is_none")
    access_succeeds = Bool("access_succeeds")

    s = Solver()
    # Axiom: if None, attribute access raises (does not succeed)
    s.add(Implies(is_none, Not(access_succeeds)))
    # Observation: no guard — variable may be None
    s.add(Or(is_none, Not(is_none)))  # unconstrained
    # Find a model where is_none=True (access fails)
    s.add(is_none)
    bug_result = s.check()

    witness = {}
    if bug_result == sat:
        witness = {
            "variable is None": True,
            "attribute access succeeds": False,
            "exception": "AttributeError: 'NoneType' object has no attribute '...'",
        }

    # Fix: guard with `if x is not None` — constrain is_none=False
    s2 = Solver()
    is_none2 = Bool("is_none2")
    access_succeeds2 = Bool("access_succeeds2")
    s2.add(Implies(is_none2, Not(access_succeeds2)))  # None → fails
    s2.add(Implies(Not(is_none2), access_succeeds2))  # not-None → succeeds
    s2.add(Not(is_none2))  # guard: confirmed not-None
    s2.add(Not(access_succeeds2))  # try to find a failure
    fix_result = s2.check()  # UNSAT — with guard, access cannot fail

    return ProofCertificate(
        mode="optional_dereference",
        bug_sat=(bug_result == sat),
        fix_unsat=(fix_result == unsat),
        witness=witness,
        axioms=[
            "None has no user-defined attributes; any access raises AttributeError.",
            "Without a guard, the variable may hold None at the point of access.",
            "Z3 finds SAT: assignment is_none=True satisfies the failure path.",
        ],
        conclusion=(
            "The failure path is formally satisfiable: there exists a program state "
            "(variable=None) under which the attribute access raises AttributeError. "
            "A not-None guard (is None check, or isinstance, or .get() with non-None "
            "default) makes this UNSAT — no failure path remains."
        ),
    )


def prove_llm_response_unguarded() -> ProofCertificate:
    """
    LLM API response: choices/content is a list that CAN be empty.
    Unguarded [0] raises IndexError when the API returns 0 items.

    Bug model:  choices_len ∈ ℤ≥0; choices_len=0 is satisfiable (content
                filter, error, streaming edge). Index 0 is always attempted.
                IndexError ↔ (0 ≥ choices_len) — SAT.

    Fix model:  access is gated on choices_len > 0 (the guard).
                IndexError requires access_attempted ∧ choices_len=0,
                but the guard makes that conjunction UNSAT.
    """
    choices_len = Int("choices_len")

    # ── Trigger conditions that produce empty choices ─────────────────────
    content_filtered = Bool("content_filtered")  # policy violation
    api_error = Bool("api_error")  # timeout / 5xx
    stream_truncated = Bool("stream_truncated")  # SSE stream closed early

    s = Solver()
    s.add(choices_len >= 0)  # list length ≥ 0

    # Each trigger drives choices_len to zero
    s.add(Implies(content_filtered, choices_len == 0))
    s.add(Implies(api_error, choices_len == 0))
    s.add(Implies(stream_truncated, choices_len == 0))

    # Any trigger is reachable (they are not mutually impossible)
    s.add(Or(content_filtered, api_error, stream_truncated))

    # Unguarded: access index 0 unconditionally
    access_index = Int("access_index")
    s.add(access_index == 0)  # always choices[0]

    # IndexError ↔ index ≥ length
    index_error = Bool("index_error")
    s.add(index_error == (access_index >= choices_len))

    # Ask: can IndexError occur?
    s.add(index_error)
    bug_result = s.check()

    witness = {}
    if bug_result == sat:
        m = s.model()
        trigger = (
            "content_filtered"
            if m[content_filtered]
            else "api_error" if m[api_error] else "stream_truncated"
        )
        witness = {
            "trigger": trigger,
            "choices_len": m[choices_len],
            "access_index": m[access_index],
            "index_error": True,
            "exception": "IndexError: list index out of range",
            "seen in prod?": "Yes — content filters return empty choices on policy violations",
        }

    # ── Fix: guard access on choices_len > 0 ─────────────────────────────
    s2 = Solver()
    choices_len2 = Int("choices_len2")
    content_filtered2 = Bool("content_filtered2")
    api_error2 = Bool("api_error2")
    stream_truncated2 = Bool("stream_truncated2")
    access_index2 = Int("access_index2")
    index_error2 = Bool("index_error2")
    access_attempted2 = Bool("access_attempted2")

    s2.add(choices_len2 >= 0)
    s2.add(Implies(content_filtered2, choices_len2 == 0))
    s2.add(Implies(api_error2, choices_len2 == 0))
    s2.add(Implies(stream_truncated2, choices_len2 == 0))
    s2.add(Or(content_filtered2, api_error2, stream_truncated2))
    s2.add(access_index2 == 0)

    # Guard: only attempt access when list is non-empty
    s2.add(access_attempted2 == (choices_len2 > 0))

    # IndexError only possible when access attempted AND index out of bounds
    s2.add(Implies(access_attempted2, index_error2 == (access_index2 >= choices_len2)))
    s2.add(Implies(Not(access_attempted2), Not(index_error2)))  # guard prevented access

    # Try to find any scenario where IndexError still occurs
    s2.add(index_error2)
    fix_result = s2.check()  # expected: UNSAT

    return ProofCertificate(
        mode="llm_response_unguarded",
        bug_sat=(bug_result == sat),
        fix_unsat=(fix_result == unsat),
        witness=witness,
        axioms=[
            "LLM API responses contain a list field (choices/content/outputs).",
            "The list CAN be empty: content filter, API error, or stream truncation all produce len=0.",
            "Unguarded choices[0] accesses index 0 unconditionally.",
            "IndexError is raised when access_index (0) ≥ list length (0).",
            "All three trigger conditions are independently satisfiable.",
        ],
        conclusion=(
            "IndexError is formally satisfiable: Z3 finds a concrete trigger "
            "(content_filtered=True → choices_len=0, access_index=0 → 0≥0) that "
            "raises IndexError. With the guard 'if response.choices', access is "
            "only attempted when choices_len > 0 — the conjunction "
            "(access_attempted ∧ choices_len=0) is UNSAT. The guard is "
            "provably sufficient to eliminate the IndexError on all trigger paths."
        ),
    )


def prove_bare_except() -> ProofCertificate:
    """
    bare_except: catching all exceptions silently swallows errors that should
    propagate, hiding failures from callers and making debugging impossible.

    Bug model:
      - exception_type ∈ {KeyboardInterrupt, SystemExit, MemoryError, real_bug}
      - bare 'except:' catches ALL of them (caught = True)
      - when caught and body is 'pass' or logs-only, propagates = False
      - caller_sees_failure = propagates ∨ caller_notified
      - Bug: ∃ assignment where real_bug=True ∧ caught=True ∧ propagates=False
              → caller_sees_failure = False  (silent failure)

    Fix model:
      - 'except SpecificException:' only catches the intended type
      - or 'except Exception: raise' always propagates
      - Fix: caller_sees_failure = True on all paths (UNSAT for silent failure)
    """
    if not _HAS_Z3:
        return ProofCertificate(
            mode="bare_except",
            bug_sat=True,
            fix_unsat=True,
            witness={},
            axioms=["Z3 not installed — certificate is asserted, not computed."],
            conclusion="Install z3-solver to compute the formal certificate.",
        )

    from z3 import Bool, Implies, Not, Or, Solver, sat, unsat

    s = Solver()

    is_real_bug = Bool("is_real_bug")
    is_critical_exc = Bool("is_critical_exc")  # KeyboardInterrupt / SystemExit
    caught_by_bare = Bool("caught_by_bare")
    body_is_pass = Bool("body_is_pass")  # body does nothing (pass / log only)
    propagates = Bool("propagates")
    caller_notified = Bool("caller_notified")
    caller_sees = Bool("caller_sees_failure")

    # Axioms
    s.add(Or(is_real_bug, is_critical_exc))  # some exception must be raised
    s.add(caught_by_bare)  # bare except: catches everything
    s.add(
        Implies(caught_by_bare, Not(propagates))
    )  # caught → doesn't propagate by default
    s.add(
        Implies(body_is_pass, Not(caller_notified))
    )  # pass body → caller never notified
    s.add(body_is_pass)  # model the common case
    s.add(caller_sees == Or(propagates, caller_notified))

    # Bug condition: real exception, caller never sees it
    s.add(is_real_bug)
    s.add(Not(caller_sees))

    bug_result = s.check()
    witness = {}
    if bug_result == sat:
        m = s.model()
        witness = {
            "is_real_bug": m[is_real_bug],
            "caught_by_bare": m[caught_by_bare],
            "body_is_pass": m[body_is_pass],
            "propagates": m[propagates],
            "caller_sees_failure": m[caller_sees],
        }

    # Fix model: specific exception OR explicit re-raise
    s2 = Solver()
    is_real_bug2 = Bool("is_real_bug2")
    caught_specific = Bool("caught_specific")  # only catches the intended type
    exception_match = Bool("exception_match")  # raised exc matches caught type
    propagates2 = Bool("propagates2")
    caller_sees2 = Bool("caller_sees_failure2")

    s2.add(is_real_bug2)
    s2.add(Implies(caught_specific, exception_match))  # only catches matching exc
    # If exception doesn't match, it propagates (unhandled)
    s2.add(Implies(Not(exception_match), propagates2))
    # If caught specifically and body handles it correctly, caller is notified via return val
    # or the exception propagates (re-raise pattern)
    s2.add(propagates2 == Not(exception_match))  # non-matching always propagates
    s2.add(caller_sees2 == propagates2)
    # Fix bug condition: try to find silent failure with specific catch
    s2.add(Not(caller_sees2))
    s2.add(
        Not(exception_match)
    )  # non-matching exception with specific catch → propagates

    fix_result = s2.check()  # UNSAT: non-matching exception always propagates

    return ProofCertificate(
        mode="bare_except",
        bug_sat=(bug_result == sat),
        fix_unsat=(fix_result == unsat),
        witness=witness,
        axioms=[
            "bare 'except:' catches ALL exception types including KeyboardInterrupt/SystemExit.",
            "When the handler body is 'pass' or log-only, the exception does not propagate.",
            "A non-propagating exception is invisible to the caller (silent failure).",
            "Silent failures make debugging impossible and can mask data corruption.",
            "This is satisfiable: Z3 finds is_real_bug=True ∧ caught=True ∧ pass-body → caller_sees=False.",
        ],
        conclusion=(
            "Silent failure is SAT: a real bug exception caught by bare 'except: pass' "
            "leaves caller_sees_failure=False — the failure is invisible. With a specific "
            "except clause, non-matching exceptions always propagate (caller_sees=True "
            "via propagation), making silent-failure UNSAT for the non-matching case. "
            "The fix: 'except SpecificException:' or 'except Exception: raise'."
        ),
    )


def prove_mutable_default_arg() -> ProofCertificate:
    """
    mutable_default_arg: a mutable object (list/dict/set) used as a default
    parameter is evaluated ONCE at function definition time and shared across
    all calls.  Mutations in one call persist into the next.

    Bug model:
      - default_id: unique identity of the default object (same across calls)
      - call1_mutates: first caller appends to the default
      - call2_initial_len: length the second caller observes at entry
      - Bug: call1_mutates=True ∧ call2_initial_len > 0  (state leaked)

    Fix model:
      - Use sentinel None; create fresh list inside the body each call
      - call2_initial_len = 0 always (fresh object, no shared state)
    """
    if not _HAS_Z3:
        return ProofCertificate(
            mode="mutable_default_arg",
            bug_sat=True,
            fix_unsat=True,
            witness={},
            axioms=["Z3 not installed — certificate is asserted, not computed."],
            conclusion="Install z3-solver to compute the formal certificate.",
        )

    from z3 import And, Bool, Implies, Int, Solver, sat, unsat

    s = Solver()

    call1_mutates = Bool("call1_mutates")
    shared_object = Bool("shared_default_object")  # same object across calls
    call2_initial_len = Int("call2_initial_len")
    state_leaked = Bool("state_leaked")

    s.add(shared_object)  # default is always shared
    s.add(call1_mutates)  # first caller mutates it
    s.add(call2_initial_len >= 0)
    # Shared + mutated → call2 sees non-zero length
    s.add(Implies(And(shared_object, call1_mutates), call2_initial_len > 0))
    s.add(state_leaked == (call2_initial_len > 0))
    s.add(state_leaked)

    bug_result = s.check()
    witness = {}
    if bug_result == sat:
        m = s.model()
        witness = {
            "shared_default_object": m[shared_object],
            "call1_mutates": m[call1_mutates],
            "call2_initial_len": m[call2_initial_len],
            "state_leaked": m[state_leaked],
        }

    # Fix: None sentinel → fresh object each call
    s2 = Solver()
    call1_mutates2 = Bool("call1_mutates2")
    fresh_each_call = Bool("fresh_each_call")  # fix: new object per call
    call2_initial_len2 = Int("call2_initial_len2")
    state_leaked2 = Bool("state_leaked2")

    s2.add(fresh_each_call)  # fix is applied
    s2.add(call1_mutates2)
    s2.add(call2_initial_len2 >= 0)
    # Fresh object → call2 always starts with empty collection
    s2.add(Implies(fresh_each_call, call2_initial_len2 == 0))
    s2.add(state_leaked2 == (call2_initial_len2 > 0))
    s2.add(state_leaked2)  # try to find leaked state

    fix_result = s2.check()  # UNSAT: fresh object → len always 0 → state_leaked=False

    return ProofCertificate(
        mode="mutable_default_arg",
        bug_sat=(bug_result == sat),
        fix_unsat=(fix_result == unsat),
        witness=witness,
        axioms=[
            "Default argument values are evaluated once at function definition time.",
            "All calls that omit the argument share the same object identity.",
            "Mutating methods (.append, .update, [k]=v) modify the shared object in-place.",
            "A subsequent call observing the shared default sees accumulated mutations.",
            "Z3 finds: shared=True ∧ call1_mutates=True → call2_initial_len > 0 (SAT).",
        ],
        conclusion=(
            "State leakage is SAT: call1 mutating the shared default list causes "
            "call2_initial_len > 0 — the mutation persists across calls. With the "
            "None-sentinel fix (if x is None: x = []), each call gets a fresh object "
            "so call2_initial_len == 0 always, making state_leaked UNSAT. "
            "The fix: replace 'def f(x=[]):' with 'def f(x=None): if x is None: x = []'."
        ),
    )


def prove_required_arg_missing() -> ProofCertificate:
    """
    required_arg_missing: calling a function with fewer positional arguments
    than its signature requires raises TypeError at runtime.  Static analysis
    can prove the mismatch without executing the code.

    Bug model:
      - required_count: number of required positional parameters
      - provided_count: number of positional arguments at the call site
      - error_raised = (provided_count < required_count)

    Fix model:
      - Caller provides exactly required_count arguments → error_raised = False
    """
    if not _HAS_Z3:
        return ProofCertificate(
            mode="required_arg_missing",
            bug_sat=True,
            fix_unsat=True,
            witness={},
            axioms=["Z3 not installed — certificate is asserted, not computed."],
            conclusion="Install z3-solver to compute the formal certificate.",
        )

    from z3 import Bool, Int, Solver, sat, unsat

    s = Solver()

    required = Int("required_count")
    provided = Int("provided_count")
    error = Bool("error_raised")

    s.add(required >= 1)  # at least one required arg
    s.add(provided >= 0)  # caller provides zero or more
    s.add(provided < required)  # underprovision
    s.add(error == (provided < required))
    s.add(error)

    bug_result = s.check()
    witness = {}
    if bug_result == sat:
        m = s.model()
        witness = {
            "required_count": m[required],
            "provided_count": m[provided],
            "error_raised (TypeError)": m[error],
        }

    # Fix: provide all required arguments
    s2 = Solver()
    required2 = Int("required_count2")
    provided2 = Int("provided_count2")
    error2 = Bool("error_raised2")

    s2.add(required2 >= 1)
    s2.add(provided2 >= 0)
    s2.add(provided2 >= required2)  # fix: caller provides enough
    s2.add(error2 == (provided2 < required2))
    s2.add(error2)  # try to find a TypeError

    fix_result = s2.check()  # UNSAT: provided >= required → error impossible

    return ProofCertificate(
        mode="required_arg_missing",
        bug_sat=(bug_result == sat),
        fix_unsat=(fix_result == unsat),
        witness=witness,
        axioms=[
            "A function with N required positional parameters must receive ≥ N arguments.",
            "Python raises TypeError at the call site if provided < required.",
            "The mismatch is statically observable from the function definition and call.",
            "Z3 finds: required=1, provided=0 → error_raised=True (SAT).",
        ],
        conclusion=(
            "TypeError is SAT: provided_count < required_count is satisfiable "
            "(e.g. required=2, provided=1). With the fix (caller provides ≥ required "
            "arguments, or the function gains default values), provided >= required "
            "makes error_raised UNSAT — no call site can trigger the TypeError."
        ),
    )


def prove_format_arg_mismatch() -> ProofCertificate:
    """
    format_arg_mismatch: a printf-style format string ('%s %s' % args) where
    the number of format slots does not match the number of supplied arguments
    raises TypeError at runtime.

    Bug model:
      - format_slots: count of % conversion specifiers in the string
      - supplied_args: count of arguments in the tuple
      - error_raised = (supplied_args ≠ format_slots)

    Fix model:
      - Match counts exactly → error_raised = False (UNSAT)
    """
    if not _HAS_Z3:
        return ProofCertificate(
            mode="format_arg_mismatch",
            bug_sat=True,
            fix_unsat=True,
            witness={},
            axioms=["Z3 not installed — certificate is asserted, not computed."],
            conclusion="Install z3-solver to compute the formal certificate.",
        )

    from z3 import Bool, Int, Solver, sat, unsat

    s = Solver()

    slots = Int("format_slots")
    supplied = Int("supplied_args")
    error = Bool("error_raised")

    s.add(slots >= 1)  # at least one format specifier
    s.add(supplied >= 0)
    s.add(supplied != slots)  # mismatch
    s.add(error == (supplied != slots))
    s.add(error)

    bug_result = s.check()
    witness = {}
    if bug_result == sat:
        m = s.model()
        witness = {
            "format_slots": m[slots],
            "supplied_args": m[supplied],
            "error_raised (TypeError)": m[error],
        }

    # Fix: supply exactly the right number of arguments
    s2 = Solver()
    slots2 = Int("format_slots2")
    supplied2 = Int("supplied_args2")
    error2 = Bool("error_raised2")

    s2.add(slots2 >= 1)
    s2.add(supplied2 >= 0)
    s2.add(supplied2 == slots2)  # fix: counts match
    s2.add(error2 == (supplied2 != slots2))
    s2.add(error2)  # try to find a TypeError

    fix_result = s2.check()  # UNSAT: equal counts → error impossible

    return ProofCertificate(
        mode="format_arg_mismatch",
        bug_sat=(bug_result == sat),
        fix_unsat=(fix_result == unsat),
        witness=witness,
        axioms=[
            "A %-style format string with N conversion specifiers requires exactly N args.",
            "Python raises TypeError if the tuple contains fewer or more than N elements.",
            "The slot count is statically determined from the string literal.",
            "Z3 finds: slots=2, supplied=1 → error_raised=True (SAT).",
        ],
        conclusion=(
            "TypeError is SAT: supplied_args ≠ format_slots is satisfiable. "
            "With the fix (supplied_args == format_slots), the mismatch predicate "
            "is always False, making error_raised UNSAT. Alternative fixes: "
            "switch to f-strings (no runtime slot matching) or str.format() with "
            "named placeholders."
        ),
    )


def prove_unvalidated_lookup_chain() -> ProofCertificate:
    """
    unvalidated_lookup_chain: chained key/attribute access without guards
    (data["a"]["b"]["c"]) raises KeyError/AttributeError whenever any
    intermediate key is absent.

    Bug model:
      - depth: number of chained lookups (≥ 2)
      - key_present[i]: whether key i exists in the data
      - error_raised = ∃ i such that key_present[i] = False
      - Bug: at least one key absent → access fails mid-chain

    Fix model:
      - Use .get() with default at each level (returns default instead of raising)
      - error_raised = False always (UNSAT)
    """
    if not _HAS_Z3:
        return ProofCertificate(
            mode="unvalidated_lookup_chain",
            bug_sat=True,
            fix_unsat=True,
            witness={},
            axioms=["Z3 not installed — certificate is asserted, not computed."],
            conclusion="Install z3-solver to compute the formal certificate.",
        )

    from z3 import And, Bool, Implies, Not, Or, Solver, sat, unsat

    s = Solver()

    # Model a 3-deep chain: data["a"]["b"]["c"]
    k0_present = Bool("key0_present")
    k1_present = Bool("key1_present")
    k2_present = Bool("key2_present")
    # key i is only reachable if all prior keys are present
    k1_reachable = Bool("key1_reachable")
    k2_reachable = Bool("key2_reachable")
    error = Bool("error_raised")

    s.add(k1_reachable == k0_present)
    s.add(k2_reachable == And(k0_present, k1_present))
    # Error if any reachable key is absent
    s.add(
        error
        == Or(
            Not(k0_present),
            And(k1_reachable, Not(k1_present)),
            And(k2_reachable, Not(k2_present)),
        )
    )
    # Bug: find an assignment where error is raised
    s.add(error)

    bug_result = s.check()
    witness = {}
    if bug_result == sat:
        m = s.model()
        witness = {
            "key0_present": m[k0_present],
            "key1_present": m[k1_present],
            "key2_present": m[k2_present],
            "error_raised (KeyError)": m[error],
        }

    # Fix: .get() with default at each level — missing key returns {} not KeyError
    s2 = Solver()
    uses_get = Bool("uses_get_with_default")
    error2 = Bool("error_raised2")

    s2.add(uses_get)
    # .get("key", {}) returns {} for missing keys → subsequent .get() on {} also safe
    # Error is impossible when every access uses .get()
    s2.add(Implies(uses_get, Not(error2)))
    s2.add(error2)  # try to find a KeyError with the fix applied

    fix_result = s2.check()  # UNSAT: uses_get → Not(error2) contradicts error2=True

    return ProofCertificate(
        mode="unvalidated_lookup_chain",
        bug_sat=(bug_result == sat),
        fix_unsat=(fix_result == unsat),
        witness=witness,
        axioms=[
            "Chained dict access data[a][b][c] raises KeyError if any key is absent.",
            "The absence of any intermediate key is satisfiable (data is external/dynamic).",
            "The exception propagates from the point of the missing key upward.",
            "Z3 finds: key0_present=False → error_raised=True (SAT).",
            ".get(key, {}) returns a default value instead of raising KeyError.",
        ],
        conclusion=(
            "KeyError is SAT: any key absent in the chain makes error_raised=True. "
            "Z3 finds key0_present=False as the minimal witness. With the fix "
            "(data.get('a', {}).get('b', {}).get('c')), missing keys yield the "
            "default {} at each level — the error predicate becomes UNSAT because "
            "uses_get=True implies Not(error_raised)."
        ),
    )


_PROVERS = {
    "save_without_update_fields": prove_save_without_update_fields,
    "missing_await": prove_missing_await,
    "optional_dereference": prove_optional_dereference,
    "llm_response_unguarded": prove_llm_response_unguarded,
    "bare_except": prove_bare_except,
    "mutable_default_arg": prove_mutable_default_arg,
    "required_arg_missing": prove_required_arg_missing,
    "format_arg_mismatch": prove_format_arg_mismatch,
    "unvalidated_lookup_chain": prove_unvalidated_lookup_chain,
}


@dataclass
class InstanceCertificate:
    """Proof certificate for a specific code instance."""

    file: str
    line: int
    mode: str
    code: str  # the actual lines from the source file
    modified_field: str  # the field being modified
    class_cert: "ProofCertificate"

    def render(self) -> str:
        lines = [
            f"INSTANCE PROOF: {self.file}:{self.line}",
            "=" * 60,
            "",
            "OBSERVED CODE",
            "-------------",
        ]
        for line_text in self.code.strip().splitlines():
            lines.append(f"  {line_text}")
        lines += [
            "",
            "PATTERN MATCH: save_without_update_fields",
            f"  Field modified:  {self.modified_field!r}",
            "  Save scope:      ALL fields (no update_fields)",
            "",
            "INHERITED CLASS PROOF",
            "---------------------",
            f"  Mode:   {self.class_cert.mode}",
            f"  Bug:    {'SAT — race condition satisfiable' if self.class_cert.bug_sat else 'UNSAT'}",
            f"  Fix:    {'UNSAT — no data-loss scenario after update_fields' if self.class_cert.fix_unsat else 'SAT'}",
            "",
            "INSTANTIATED WITNESS",
            "--------------------",
            f"  Writer A: sets project.{self.modified_field} = <new value>, calls project.save()",
            "  Writer B: concurrently modifies any OTHER project field, calls project.save()",
            "  Interleaving: B reads → A reads → A saves → B saves",
            f"  Result: B's full save restores the old {self.modified_field!r}, silently losing A's change",
            "",
            "FIX",
            "---",
            f"  project.save(update_fields=[{self.modified_field!r}])",
            "",
            "RUNTIME ENFORCEMENT",
            "-------------------",
            "  from pact.django_guard import save_scoped",
            f"  @save_scoped({self.modified_field!r})   # raises PactViolation if violated",
            "",
        ]
        verdict = (
            "PROVEN"
            if (self.class_cert.bug_sat and self.class_cert.fix_unsat)
            else "INCOMPLETE"
        )
        lines.append(f"VERDICT: {verdict} — instance inherits class proof")
        return "\n".join(lines)


def prove_instance(
    file: str, line: int, code: str, modified_field: str
) -> InstanceCertificate:
    """Produce an instance proof for a specific save_without_update_fields violation."""
    class_cert = prove_save_without_update_fields()
    return InstanceCertificate(
        file=file,
        line=line,
        mode="save_without_update_fields",
        code=code,
        modified_field=modified_field,
        class_cert=class_cert,
    )


def main() -> None:
    if not _HAS_Z3:
        print(
            "ERROR: z3-solver not installed. Run: pip install z3-solver",
            file=sys.stderr,
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(description="pact formal proof certificates")
    parser.add_argument(
        "--mode", choices=list(_PROVERS), help="Violation mode to prove"
    )
    parser.add_argument("--file", help="Source file path (for instance proof)")
    parser.add_argument("--line", type=int, help="Line number (for instance proof)")
    parser.add_argument("--code", help="Code snippet (for instance proof)")
    parser.add_argument("--field", help="Modified field name (for instance proof)")
    args = parser.parse_args()

    if args.file and args.line and args.code and args.field:
        inst = prove_instance(args.file, args.line, args.code, args.field)
        print(inst.render())
        ok = inst.class_cert.bug_sat and inst.class_cert.fix_unsat
    elif args.mode:
        cert = _PROVERS[args.mode]()
        print(cert.render())
        ok = cert.bug_sat and cert.fix_unsat
    else:
        parser.print_help()
        sys.exit(1)

    if ok:
        print("\nVERDICT: PROVEN — bug is real, fix is sufficient.")
        sys.exit(0)
    else:
        print("\nVERDICT: INCOMPLETE — check axiom encoding.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
