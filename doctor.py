"""
pact doctor — environment preflight.

Exercises each critical capability (imports AND a trivial use — "installed" != "works"),
resolves the LLM key WITHOUT printing it, and reports green/red. Run this BEFORE relying
on the env, so an env blocker is caught up front instead of mid-task.

    .venv/bin/python doctor.py      # exit 0 = all green, 1 = a check is red
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn

    return deco


@check("python version sane")
def _py():
    v = sys.version_info
    if v < (3, 9):
        return False, f"{v.major}.{v.minor} too old"
    if v >= (3, 14):
        return True, f"{v.major}.{v.minor} bleeding-edge (wheels may be spotty)"
    return True, f"{v.major}.{v.minor}.{v.micro}"


@check("z3 imports + solves")
def _z3():
    import z3

    s = z3.Solver()
    x = z3.Int("x")
    s.add(x > 0)
    return str(s.check()) == "sat", z3.get_version_string()


@check("anthropic imports (pydantic stack intact)")
def _anthropic():
    import anthropic

    return True, getattr(anthropic, "__version__", "ok")


@check("tree-sitter + solidity grammar parse")
def _treesitter():
    import tree_sitter_solidity as tssol
    from tree_sitter import Language, Parser

    parser = Parser(Language(tssol.language()))
    root = parser.parse(b"contract C { uint256 x; }").root_node
    return (not root.has_error and root.child_count > 0), f"root={root.type}"


@check("LLM key resolvable (value never printed)")
def _key():
    for k in (
        "PACT_LLM_API_KEY",
        "PACT_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    ):
        v = os.environ.get(k)
        if v:
            return True, f"{k} present (len {len(v)})"
    return False, "no key in env or .env"


@check("pact llm client constructs")
def _client():
    sys.path.insert(0, HERE)
    from llm import make_client

    make_client()
    return True, "constructed"


def main():
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(HERE, ".env"))
    except Exception:
        pass

    ok_all = True
    for name, fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {str(e)[:90]}"
        ok_all = ok_all and ok
        print(f"  {'🟢' if ok else '🔴'} {name}: {detail}")
    print(
        "\nDOCTOR:",
        "ALL GREEN ✅" if ok_all else "RED ❌ — fix above before running pact",
    )
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
