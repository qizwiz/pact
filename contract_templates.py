"""
pact contract_templates — pre-built Z3 script templates keyed by contract_kind.

Each template is a plain string with ALL_CAPS placeholders.  The rendered
script is always syntactically valid Python, always imports z3 and json, and
always prints exactly one JSON line:

    {"status": "sat"|"unsat", "counterexample": {...}|null, "explanation": "..."}

SAT   → the bug exists (contract is violated in the current code)
UNSAT → the contract is enforced (no counterexample exists)

Usage::

    from pact.contract_templates import render_z3_template, SUPPORTED_KINDS
    script = render_z3_template("flag_invariant", {"flag_name": "enabled", ...})
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# flag_invariant
# ---------------------------------------------------------------------------
# A boolean flag silently suppresses constraint checks when False.
# SAT when silent_when_false=True  → flag=False lets violations through (bug)
# UNSAT when silent_when_false=False → guard always runs (fixed)
# ---------------------------------------------------------------------------

_FLAG_TEMPLATE = """\
import z3
import json

s = z3.Solver()

flag = z3.Bool('FLAG_NAME')
violation_exists = z3.Bool('violation_exists')

if SILENT_WHEN_FALSE:
    # Bug: flag=False suppresses CHECK_NAME — violation can slip through
    # SAT witness: flag=False AND violation_exists=True
    s.add(z3.Not(flag))
    s.add(violation_exists)
else:
    # Fixed: guard always runs regardless of flag — no silent skip
    # UNSAT: can't have violation_exists=True when guard always fires
    s.add(z3.Implies(violation_exists, flag))
    s.add(z3.Not(flag))
    s.add(violation_exists)

result = s.check()
if str(result) == 'sat':
    m = s.model()
    ce = {
        'FLAG_NAME': str(m[flag]),
        'violation_exists': str(m[violation_exists]),
        'explanation': 'FLAG_NAME=False silently skips CHECK_NAME — violation not caught'
    }
    print(json.dumps({'status': 'sat', 'counterexample': ce,
                      'explanation': 'FLAG_NAME=False suppresses CHECK_NAME check'}))
else:
    print(json.dumps({'status': 'unsat', 'counterexample': None,
                      'explanation': 'FLAG_NAME guard always fires — no silent skip possible'}))
"""


def _flag_invariant(params: dict) -> str:
    flag_name = params.get("flag_name", "flag")
    check_name = params.get("check_name", "check")
    silent_when_false = params.get("silent_when_false", True)
    script = _FLAG_TEMPLATE
    script = script.replace("FLAG_NAME", flag_name)
    script = script.replace("CHECK_NAME", check_name)
    script = script.replace(
        "SILENT_WHEN_FALSE", "True" if silent_when_false else "False"
    )
    return script


# ---------------------------------------------------------------------------
# nullable_contract
# ---------------------------------------------------------------------------
# A None value silently skips all value-level checks.
# SAT when skips_on_none=True  → field=None skips FIELD_NAME checks (bug)
# UNSAT when skips_on_none=False → None is handled explicitly (fixed)
# ---------------------------------------------------------------------------

_NULLABLE_TEMPLATE = """\
import z3
import json

s = z3.Solver()

is_none = z3.Bool('FIELD_NAME_is_none')
check_skipped = z3.Bool('check_skipped')

if SKIPS_ON_NONE:
    # Bug: None silently skips CHECK_NAME — violation can slip through
    # SAT witness: is_none=True AND check_skipped=True
    s.add(is_none)
    s.add(check_skipped)
else:
    # Fixed: None is handled explicitly — check always runs or raises
    # UNSAT: can't have check_skipped=True when None is explicitly handled
    s.add(z3.Implies(is_none, z3.Not(check_skipped)))
    s.add(is_none)
    s.add(check_skipped)

result = s.check()
if str(result) == 'sat':
    m = s.model()
    ce = {
        'FIELD_NAME': 'None',
        'check_skipped': str(m[check_skipped]),
        'explanation': 'FIELD_NAME=None silently skips CHECK_NAME check'
    }
    print(json.dumps({'status': 'sat', 'counterexample': ce,
                      'explanation': 'FIELD_NAME=None silently skips CHECK_NAME'}))
else:
    print(json.dumps({'status': 'unsat', 'counterexample': None,
                      'explanation': 'None case handled explicitly — no silent skip possible'}))
"""


def _nullable_contract(params: dict) -> str:
    field_name = params.get("field_name", "field")
    check_name = params.get("check_name", "check")
    skips_on_none = params.get("skips_on_none", True)
    script = _NULLABLE_TEMPLATE
    script = script.replace("FIELD_NAME", field_name)
    script = script.replace("CHECK_NAME", check_name)
    script = script.replace("SKIPS_ON_NONE", "True" if skips_on_none else "False")
    return script


# ---------------------------------------------------------------------------
# subset_relation
# ---------------------------------------------------------------------------
# SET_A must be a subset of SET_B (required ⊆ provided).
# SAT when there exists an element in SET_A that is NOT in SET_B.
# Uses BitVec(8) to model sets of up to 8 elements.
# ---------------------------------------------------------------------------

_SUBSET_TEMPLATE = """\
import z3
import json

s = z3.Solver()

# Model sets as BitVec(8): bit i set means element i is in the set
set_a = z3.BitVec('SET_A_bits', 8)   # required set
set_b = z3.BitVec('SET_B_bits', 8)   # provided set

# set_a must be non-empty (there is something required)
s.add(set_a != 0)

# Find element in set_a that is NOT in set_b: set_a & ~set_b != 0
missing = set_a & (~set_b)
s.add(missing != 0)

result = s.check()
if str(result) == 'sat':
    m = s.model()
    a_val = m[set_a].as_long()
    b_val = m[set_b].as_long()
    missing_bits = a_val & (~b_val & 0xFF)
    ce = {
        'SET_A': bin(a_val),
        'SET_B': bin(b_val),
        'missing_from_SET_B': bin(missing_bits),
        'explanation': 'SET_A has element(s) not present in SET_B — subset violated'
    }
    print(json.dumps({'status': 'sat', 'counterexample': ce,
                      'explanation': 'SET_A is not a subset of SET_B'}))
else:
    print(json.dumps({'status': 'unsat', 'counterexample': None,
                      'explanation': 'SET_A is always a subset of SET_B — contract holds'}))
"""


def _subset_relation(params: dict) -> str:
    set_a = params.get("set_a", "required")
    set_b = params.get("set_b", "provided")
    script = _SUBSET_TEMPLATE
    script = script.replace("SET_A", set_a)
    script = script.replace("SET_B", set_b)
    return script


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------
# SECOND_OP requires FIRST_OP to have run first.
# SAT when guard_exists=False  → SECOND_OP can be called without FIRST_OP (bug)
# UNSAT when guard_exists=True → guard enforces ordering (fixed)
# ---------------------------------------------------------------------------

_ORDERING_TEMPLATE = """\
import z3
import json

s = z3.Solver()

first_op_ran = z3.Bool('FIRST_OP_ran')
second_op_called = z3.Bool('SECOND_OP_called')

if GUARD_EXISTS:
    # Fixed: guard ensures FIRST_OP ran before SECOND_OP
    # UNSAT: SECOND_OP cannot be called unless FIRST_OP ran
    s.add(z3.Implies(second_op_called, first_op_ran))
    s.add(second_op_called)
    s.add(z3.Not(first_op_ran))
else:
    # Bug: no guard — SECOND_OP can be called without FIRST_OP
    # SAT witness: second_op_called=True AND first_op_ran=False
    s.add(second_op_called)
    s.add(z3.Not(first_op_ran))

result = s.check()
if str(result) == 'sat':
    m = s.model()
    ce = {
        'FIRST_OP_ran': str(m[first_op_ran]),
        'SECOND_OP_called': str(m[second_op_called]),
        'explanation': 'SECOND_OP called without FIRST_OP having run first'
    }
    print(json.dumps({'status': 'sat', 'counterexample': ce,
                      'explanation': 'SECOND_OP can be called without FIRST_OP — ordering violated'}))
else:
    print(json.dumps({'status': 'unsat', 'counterexample': None,
                      'explanation': 'Guard enforces FIRST_OP before SECOND_OP — ordering holds'}))
"""


def _ordering(params: dict) -> str:
    first_op = params.get("first_op", "setup")
    second_op = params.get("second_op", "run")
    guard_exists = params.get("guard_exists", False)
    script = _ORDERING_TEMPLATE
    script = script.replace("FIRST_OP", first_op)
    script = script.replace("SECOND_OP", second_op)
    script = script.replace("GUARD_EXISTS", "True" if guard_exists else "False")
    return script


# ---------------------------------------------------------------------------
# resource_lifecycle
# ---------------------------------------------------------------------------
# RESOURCE must be released after acquisition.
# SAT when release_guaranteed=False → acquire path exists without release (bug)
# UNSAT when release_guaranteed=True → every acquire is paired with release (fixed)
# ---------------------------------------------------------------------------

_RESOURCE_LIFECYCLE_TEMPLATE = """\
import z3
import json

s = z3.Solver()

acquired = z3.Bool('RESOURCE_acquired')
released = z3.Bool('RESOURCE_released')
exception_path = z3.Bool('exception_path')

if RELEASE_GUARANTEED:
    # Fixed: release always happens — either normally or via finally/context manager
    # UNSAT: cannot have acquired=True AND released=False
    s.add(z3.Implies(acquired, released))
    s.add(acquired)
    s.add(z3.Not(released))
else:
    # Bug: exception path skips release — acquire without release possible
    # SAT witness: acquired=True AND exception_path=True AND released=False
    s.add(acquired)
    s.add(exception_path)
    s.add(z3.Implies(exception_path, z3.Not(released)))

result = s.check()
if str(result) == 'sat':
    m = s.model()
    ce = {
        'RESOURCE_acquired': str(m[acquired]),
        'RESOURCE_released': str(m[released]),
        'exception_path': str(m[exception_path]),
        'explanation': 'RESOURCE acquired but not released on exception path'
    }
    print(json.dumps({'status': 'sat', 'counterexample': ce,
                      'explanation': 'RESOURCE lifecycle violated — acquire without release'}))
else:
    print(json.dumps({'status': 'unsat', 'counterexample': None,
                      'explanation': 'RESOURCE always released after acquire — lifecycle holds'}))
"""


def _resource_lifecycle(params: dict) -> str:
    resource = params.get("resource", "resource")
    release_guaranteed = params.get("release_guaranteed", False)
    script = _RESOURCE_LIFECYCLE_TEMPLATE
    script = script.replace("RESOURCE", resource)
    script = script.replace(
        "RELEASE_GUARANTEED", "True" if release_guaranteed else "False"
    )
    return script


# ---------------------------------------------------------------------------
# error_contract
# ---------------------------------------------------------------------------
# An exception is caught and silently swallowed — caller receives an identical
# sentinel (None, [], False) regardless of whether an error occurred.
# SAT when silent_on_exception=True  → exception raised but caller not notified (bug)
# UNSAT when silent_on_exception=False → exception produces distinguishable signal (fixed)
# ---------------------------------------------------------------------------

_ERROR_CONTRACT_TEMPLATE = """\
import z3
import json

s = z3.Solver()

exception_raised = z3.Bool('exception_raised')
caller_notified = z3.Bool('caller_notified')

if SILENT_ON_EXCEPTION:
    # Bug: EXCEPTION_NAME caught and swallowed — caller receives same sentinel
    # SAT witness: exception_raised=True AND caller_notified=False
    s.add(exception_raised)
    s.add(z3.Not(caller_notified))
else:
    # Fixed: exception produces distinct signal (log, re-raise, or typed return)
    # UNSAT: cannot have exception_raised=True AND caller_notified=False
    s.add(z3.Implies(exception_raised, caller_notified))
    s.add(exception_raised)
    s.add(z3.Not(caller_notified))

result = s.check()
if str(result) == 'sat':
    m = s.model()
    ce = {
        'exception_raised': str(m[exception_raised]),
        'caller_notified': str(m[caller_notified]),
        'explanation': 'EXCEPTION_NAME silently swallowed in FUNCTION_NAME — caller cannot detect error'
    }
    print(json.dumps({'status': 'sat', 'counterexample': ce,
                      'explanation': 'EXCEPTION_NAME silently swallowed in FUNCTION_NAME'}))
else:
    print(json.dumps({'status': 'unsat', 'counterexample': None,
                      'explanation': 'FUNCTION_NAME signals EXCEPTION_NAME to caller — no silent swallow'}))
"""


def _error_contract(params: dict) -> str:
    exception_name = params.get("exception_name", "Exception")
    function_name = params.get("function_name", "function")
    silent_on_exception = params.get("silent_on_exception", True)
    script = _ERROR_CONTRACT_TEMPLATE
    script = script.replace("EXCEPTION_NAME", exception_name)
    script = script.replace("FUNCTION_NAME", function_name)
    script = script.replace(
        "SILENT_ON_EXCEPTION", "True" if silent_on_exception else "False"
    )
    return script


# ---------------------------------------------------------------------------
# conservation_invariant   (smart-contract: sum(balances) == total_supply)
# ---------------------------------------------------------------------------
# A token transfer must preserve the conservation law: the sum of all balances
# always equals total_supply (no value created or destroyed outside mint/burn).
# We model one transfer of `amount` from `bal_from` to `bal_to`, with the
# invariant holding BEFORE, and check whether it can be broken AFTER.
# SAT  when preserves_sum=False → transfer credits receiver without debiting
#                                 sender → tokens minted from nothing (bug)
# UNSAT when preserves_sum=True → debit + credit cancel → conservation holds
# ---------------------------------------------------------------------------

_CONSERVATION_TEMPLATE = """\
import z3
import json

s = z3.Solver()

# Conservation invariant: sum(balances) == total_supply.
# Model ONE transfer of `amount` from bal_from to bal_to.
bal_from = z3.Int('bal_from')
bal_to   = z3.Int('bal_to')
amount   = z3.Int('amount')
total    = z3.Int('total_supply')

# Preconditions: non-negative balances, a real transfer the sender can afford,
# and the invariant holds BEFORE the transfer.
s.add(bal_from >= 0, bal_to >= 0, total >= 0)
s.add(amount > 0, amount <= bal_from)
s.add(bal_from + bal_to == total)          # invariant holds before

if PRESERVES_SUM:
    # Correct: debit sender, credit receiver — sum is conserved
    bal_from_after = bal_from - amount
    bal_to_after   = bal_to + amount
else:
    # Bug: credit receiver but FORGET to debit sender → value minted from nothing
    bal_from_after = bal_from
    bal_to_after   = bal_to + amount

# Violation = invariant broken AFTER the transfer (total_supply unchanged)
s.add(bal_from_after + bal_to_after != total)

result = s.check()
if str(result) == 'sat':
    m = s.model()
    sum_after = m.eval(bal_from_after + bal_to_after, model_completion=True)
    ce = {
        'bal_from_before': str(m.eval(bal_from, model_completion=True)),
        'bal_to_before': str(m.eval(bal_to, model_completion=True)),
        'amount': str(m.eval(amount, model_completion=True)),
        'total_supply': str(m.eval(total, model_completion=True)),
        'sum_balances_after': str(sum_after),
        'explanation': 'TOKEN.transfer breaks sum(balances)==total_supply — value created/destroyed'
    }
    print(json.dumps({'status': 'sat', 'counterexample': ce,
                      'explanation': 'TOKEN.transfer can violate conservation: sum(balances) != total_supply'}))
else:
    print(json.dumps({'status': 'unsat', 'counterexample': None,
                      'explanation': 'TOKEN.transfer preserves sum(balances)==total_supply — conservation holds'}))
"""


def _conservation_invariant(params: dict) -> str:
    token = params.get("token", "Token")
    preserves_sum = params.get("preserves_sum", True)
    script = _CONSERVATION_TEMPLATE
    script = script.replace("TOKEN", token)
    script = script.replace("PRESERVES_SUM", "True" if preserves_sum else "False")
    return script


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

SUPPORTED_KINDS = {
    "flag_invariant",
    "nullable_contract",
    "subset_relation",
    "ordering",
    "resource_lifecycle",
    "error_contract",
    "conservation_invariant",
}

_RENDERERS = {
    "flag_invariant": _flag_invariant,
    "nullable_contract": _nullable_contract,
    "subset_relation": _subset_relation,
    "ordering": _ordering,
    "resource_lifecycle": _resource_lifecycle,
    "error_contract": _error_contract,
    "conservation_invariant": _conservation_invariant,
}


def render_z3_template(contract_kind: str, params: dict) -> str:
    """Return a runnable Z3 script for the given contract_kind and params.

    Parameters
    ----------
    contract_kind:
        One of SUPPORTED_KINDS.
    params:
        Kind-specific parameters (see module docstring for each kind's params).

    Returns
    -------
    str
        A complete, syntactically valid Python script that imports z3 and json
        and prints a single JSON result line.

    Raises
    ------
    KeyError
        If contract_kind is not in SUPPORTED_KINDS.
    """
    renderer = _RENDERERS[contract_kind]
    return renderer(params)
