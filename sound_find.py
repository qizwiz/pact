"""
sound_find — close pact_find's soundness gap by EXECUTION, not self-report.

pact_find (LLM + Hypothesis) proposes a property and a counterexample, then reports
`hypothesis_confirmed: true`. But that confirmation is not trustworthy on its own —
observed live: for an `apply_discount` bug it reported counterexample (0.01, 0.0),
which does NOT distinguish buggy from correct (at percent=0 both return the input).
The explanation was right; the witness was bogus. That is the LLM half-grading itself.

This layer makes confirmation SOUND, with no LLM in the verdict:

  for a finding (function, reported counterexample):
    1. run the reported counterexample through the REAL code (buggy vs fixed twin).
       If their outputs/exceptions differ → SOUND, witness is real.
    2. if it does not distinguish, FUZZ around the counterexample's type-shape
       (random inputs of the same arity/types) for an input that DOES distinguish.
       If found → SOUND (recovered a real witness pact_find failed to report).
    3. otherwise → UNCONFIRMED (no executable evidence the bug is real).

The oracle is execution on two versions of the code — the same differential discipline
proven in the Solidity loop, now applied to pact_find's own output. A finding survives
only with a concrete input on which the code demonstrably misbehaves.

(Benchmark form uses the fixed twin. The product form, with no twin, swaps the twin for
an executable rendering of the proposed property — same execute-don't-trust principle.)
"""

from __future__ import annotations

import ast
import importlib.util
import random
import sys


def _load(src: str, name: str):
    """Import a module from source string under a unique name."""
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(src, f"<{name}>", "exec"), mod.__dict__)
    sys.modules[name] = mod
    return mod


def _func_at_line(src: str, line: int) -> str:
    """Name of the function whose body span contains `line`."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ""
    best = ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lo, hi = node.lineno, getattr(node, "end_lineno", node.lineno)
            if lo <= line <= hi:
                best = node.name  # innermost containing function
    return best


def _parse_args(ce: str):
    """pact_find counterexamples look like '(0, 1)' or '0' — normalise to a tuple."""
    try:
        val = ast.literal_eval(ce.strip())
    except Exception:
        return None
    return val if isinstance(val, tuple) else (val,)


def _sample(template: tuple):
    """Random input matching the type-shape of the template tuple."""
    out = []
    for t in template:
        if isinstance(t, bool):
            out.append(random.choice([True, False]))
        elif isinstance(t, int):
            out.append(random.randint(-1000, 1000))
        elif isinstance(t, float):
            out.append(round(random.uniform(-1000, 1000), 3))
        elif isinstance(t, str):
            out.append(random.choice(["", "a", "x" * random.randint(0, 8)]))
        else:
            out.append(t)  # unknown type → reuse template value
    return tuple(out)


def _call(mod, func: str, args: tuple):
    """Return ('ok', value) or ('raise', ExceptionTypeName)."""
    try:
        return ("ok", getattr(mod, func)(*args))
    except Exception as e:  # noqa: BLE001 — we are comparing failure behaviour
        return ("raise", type(e).__name__)


def _distinguishes(bmod, fmod, func: str, args: tuple) -> bool:
    return _call(bmod, func, args) != _call(fmod, func, args)


def sound_confirm(
    buggy_src: str, fixed_src: str, line: int, counterexample: str, fuzz_n: int = 300
):
    """Return (verdict, witness). verdict ∈ {sound-reported, sound-fuzzed, unconfirmed}."""
    func = _func_at_line(buggy_src, line)
    if not func:
        return ("unconfirmed", None)
    bmod = _load(buggy_src, "sf_buggy")
    fmod = _load(fixed_src, "sf_fixed")

    ce = _parse_args(counterexample) if counterexample else None
    if ce is not None:
        try:
            if _distinguishes(bmod, fmod, func, ce):
                return ("sound-reported", ce)
        except TypeError:
            ce = None  # arity mismatch → fall through to fuzz

    template = ce if ce is not None else None
    if template is None:
        return ("unconfirmed", None)
    for _ in range(fuzz_n):
        cand = _sample(template)
        try:
            if _distinguishes(bmod, fmod, func, cand):
                return ("sound-fuzzed", cand)
        except TypeError:
            break
    return ("unconfirmed", None)


# --------------------------------------------------------------------------- #
# self-test on the case that exposed the gap
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    buggy = open("/tmp/fixtest/target.py").read()
    fixed = open("/tmp/fixtest/fixed.py").read()
    # pact_find's two findings (line, reported counterexample) from the live run
    findings = [
        ("permission bypass", 1, "(0, 1)"),
        ("discount /1000", 9, "(0.01, 0.0)"),  # reported witness is BOGUS
    ]
    for label, line, ce in findings:
        verdict, witness = sound_confirm(buggy, fixed, line, ce)
        print(f"{label:20s} reported={ce:12s} -> {verdict:15s} witness={witness}")
