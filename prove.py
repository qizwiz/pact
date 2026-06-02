"""
prove — twin-free execution oracle for intent gaps (the A upgrade).

pact intent's _verify_intent_gaps z3-checks invariants, but where z3 "couldn't encode"
the claim it falls back to LLM confidence (soft). This is the missing positive oracle for
that case: it PROVES a violation by execution, with no fixed twin — the invariant itself
is the reference.

  1. render the intent invariant (the code's own stated property) as an executable
     predicate  prop(args, result) -> bool  (True iff the property HOLDS).
  2. fuzz the real function; for each input, run it and evaluate the predicate.
  3. a violation is CONFIRMED only if some real execution makes the property FALSE —
     that input is the concrete witness. No witness -> unconfirmed (no soft pass).

This is sound_find lifted from twin-differential to property-differential: the oracle is
execution against the code's own intent, not a corrected copy. Residual soft spot, stated
honestly: the LLM renders the predicate — but the predicate is EXECUTED and the witness is
a real failing run, so a mis-render yields no spurious confirmation unless it both
type-checks AND flips on a real input (rare, and the witness is inspectable).

    .venv/bin/python prove.py     # prove on the logic bugs that exposed the soft gap
"""

from __future__ import annotations

import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(HERE, ".env"))
try:  # dual-mode: works standalone AND as pact.prove (package context)
    from .llm import make_client, resolve_model
    from .sound_find import _load, _sample
except ImportError:
    from llm import make_client, resolve_model
    from sound_find import _load, _sample


def _run(fn, args):
    try:
        return ("ok", fn(*args))
    except Exception as e:  # noqa: BLE001
        return ("exc", type(e).__name__)


_CLIENT = None  # lazy: no client built at import (avoids cost/key errors on import)


def _client(api_key=None):
    global _CLIENT
    if _CLIENT is None or api_key is not None:
        _CLIENT = make_client(api_key) if api_key else make_client()
    return _CLIENT


def _sig(source: str, func: str) -> str:
    try:
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == func
            ):
                return f"{func}(" + ", ".join(a.arg for a in node.args.args) + ")"
    except SyntaxError:
        pass
    return func + "(...)"


_BOUNDARIES = [0, 1, -1, True, False, "", None, [], 2, 0.0]


def _boundary_inputs(arity: int):
    """Inputs that expose sparse witnesses: all-same, and each position swept over
    boundary values while the rest are held at 1 (a benign non-falsy default)."""
    seeds = [tuple(v for _ in range(arity)) for v in (0, 1, -1)]
    for p in range(arity):
        for b in _BOUNDARIES:
            base = [1] * arity
            base[p] = b
            seeds.append(tuple(base))
    return seeds


def _arity(source: str, func: str) -> int:
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == func
        ):
            return len(node.args.args)
    return 1


def render_predicate(
    invariant: str, source: str, func: str, api_key=None, model=None
) -> str:
    """LLM renders the intent invariant as an executable predicate."""
    p = (
        "Write a pure Python predicate that checks whether a function's INTENDED property holds "
        "for one call. Signature EXACTLY:\n\n"
        "def prop(args, result):\n    # args is the tuple of positional arguments; result is the return value\n"
        "    # return True iff the intended property HOLDS for this (args, result)\n\n"
        f"Function under test: {_sig(source, func)}\n"
        f"Intended property (invariant): {invariant}\n\n"
        "Use only `args` and `result`. Be tolerant of float rounding (allow tiny epsilon). "
        "Return ONLY the `def prop` source — no prose, no fences."
    )
    r = _client(api_key).messages.create(
        model=model or resolve_model(),
        max_tokens=600,
        messages=[{"role": "user", "content": p}],
    )
    txt = (r.content[0].text if r.content else "").strip()
    txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
    return re.sub(r"```\s*$", "", txt).strip()


def execution_confirm(
    invariant: str, source: str, func: str, fuzz_n: int = 400, api_key=None, model=None
):
    """Return (verdict, witness). verdict in {confirmed, unconfirmed, error}.
    confirmed => a real input on which the actual function violates the intended property.
    """
    pred_src = render_predicate(invariant, source, func, api_key, model)
    try:
        prop = _load(pred_src, "prove_pred").prop
        fn = getattr(_load(source, "prove_tgt"), func)
    except Exception as e:  # noqa: BLE001
        return ("error", f"load failed: {type(e).__name__}: {e}")

    arity = _arity(source, func)
    template = tuple(0 for _ in range(arity))
    # boundary-seeded inputs first (sparse witnesses like a falsy 0 in one arg are
    # invisible to pure random sampling), then random fuzz.
    for args in _boundary_inputs(arity) + [_sample(template) for _ in range(fuzz_n)]:
        kind, res = _run(fn, args)
        if kind != "ok":
            continue  # the function raising is a different signal; intent gap is a wrong VALUE
        try:
            holds = prop(args, res)
        except Exception:  # noqa: BLE001
            continue  # predicate undefined on this input → skip
        if holds is False:
            return ("confirmed", {"args": args, "result": res})
    return ("unconfirmed", None)


# --------------------------------------------------------------------------- #
# prove it on the exact logic bugs that exposed the soft gap (twin-free)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    buggy = open("/tmp/fixtest/target.py").read()
    cases = [
        ("can_edit_content", "A user may edit content only if user_id == owner_id"),
        (
            "apply_discount",
            "apply_discount(price, percent) must equal price * (1 - percent/100)",
        ),
    ]
    for func, invariant in cases:
        verdict, witness = execution_confirm(invariant, buggy, func)
        print(f"{func:18s} -> {verdict:12s} witness={witness}")
