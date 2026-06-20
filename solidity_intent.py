"""
solidity_intent — pact intent, UNLOCKED for Solidity, discharged by Halmos.

invariant_agent was a naive proposer that skipped pact's real engine. This routes pact
intent's actual machinery onto Solidity:

  1. PROPOSE  — LLM reads the contract, proposes STRUCTURED invariants (id, statement,
                applies_to, rationale) — intent's invariant shape.
  2. SKEPTIC  — pact.intent._invariant_skeptic (REUSED, unchanged): an isolated adversarial
                LLM that tries to FALSIFY each claim against the source, no shared context.
                Falsified claims are dropped BEFORE they ever reach Halmos. This is pact's
                real vetting layer — it kills WRONG-CLAIM false positives.
  3. RENDER   — surviving invariants -> Halmos check_* tests, with hard discipline to kill
                WRONG-TEST false positives: every check MUST establish its precondition with
                require() on the starting state, and use DISTINCT CONCRETE accounts (no
                symbolic addresses that can alias). (The measured FP — check_conservation_borrow
                violating on correct code — was a wrong-test, not a wrong-claim.)
  4. DISCHARGE— Halmos (symbolic EVM, BitVec256): proof for all inputs or real counterexample.

intent is Python-locked (rglob *.py, ast.parse), so this is the Solidity FRONTEND reusing
intent's skeptic + Halmos as the backend — "pact intent + Halmos", not a parallel script.

    .venv/bin/python solidity_intent.py [contract.sol] [Name]
"""

from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(HERE, ".env"))

# import pact's REAL skeptic (package context via the symlink halmos_check sets up)
_LINK_PARENT = "/tmp/pact_pkg_link"
os.makedirs(_LINK_PARENT, exist_ok=True)
_LINK = os.path.join(_LINK_PARENT, "pact")
if not os.path.exists(_LINK):
    os.symlink(HERE, _LINK)
sys.path.insert(0, _LINK_PARENT)
from pact.intent import _invariant_skeptic  # noqa: E402  pact's real adversarial oracle
from pact.llm import resolve_key, resolve_model  # noqa: E402

import invariant_agent as agent  # noqa: E402  reuse _ask, _setup_project, _build, PROJECT
from plumbline import prompt_improve as pi  # noqa: E402  the improve decorator (file-backed prompts)
from halmos_check import run_halmos  # noqa: E402


def _salvage_objects(txt: str) -> list[dict]:
    """Recover complete top-level JSON objects from a possibly-truncated array.
    Real contracts + verbose invariants can overflow the token budget, cutting the
    closing ]; we keep every object that did close rather than dropping all of them."""
    out, depth, start, instr, esc = [], 0, None, False, False
    for i, ch in enumerate(txt):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    out.append(json.loads(txt[start : i + 1]))
                except Exception:
                    pass
                start = None
    return out


def propose_invariants(src: str) -> list[dict]:
    # file-backed, self-improving prompt (prompts/sol_invariant_propose.md)
    # budget raised 1600 -> 4000: improved prompt emits setup_sequence + richer rationale,
    # which overflowed 1600 on real (flattened) contracts and truncated the JSON.
    txt = agent._ask(pi.render(pi.load_prompt("sol_invariant_propose"), src=src), 4000)
    m = re.search(r"\[.*\]", txt, re.S)
    try:
        out = json.loads(m.group(0) if m else txt)
        if isinstance(out, list):
            return out
    except Exception:
        pass
    # truncation-tolerant fallback: salvage whatever objects closed
    return _salvage_objects(txt)


def render_tests(name: str, src: str, invs: list[dict]) -> str:
    inv_text = "\n".join(
        f"- {i['id']}: {i['statement']} (applies_to: {i.get('applies_to')})"
        for i in invs
    )
    # file-backed, self-improving prompt (prompts/sol_invariant_render.md)
    return agent._ask(
        pi.render(
            pi.load_prompt("sol_invariant_render"),
            name=name,
            invariants=inv_text,
            src=src,
        ),
        2600,
    )


def run(contract_path: str, name: str) -> dict:
    # NOTE: this LIVE path no longer self-improves the prompts. The former hooks scored
    # propose on skeptic-survival and render on build-success — both rate a VACUOUS test
    # 1.0 (it survives the skeptic and builds) while it catches nothing, so they churned
    # the prompt on a signal blind to recall. Prompt improvement now happens OFFLINE in
    # recall_loop.py, grounded on mutation RECALL (did the test catch a known bug), which
    # needs ground-truth mutants that only exist in the benchmark, not on a live contract.
    src = open(contract_path).read()
    model, key = resolve_model(), resolve_key()

    invs = propose_invariants(src)
    print(f"  proposed {len(invs)} invariant(s): {[i.get('id') for i in invs]}")
    if not invs:
        return {"built": False, "verdicts": []}

    # pact's REAL skeptic prunes wrong-claim false positives before Halmos
    summaries = [{"path": f"{name}.sol", "purpose": f"{name} contract", "source": src}]
    surviving_ids, falsified = _invariant_skeptic(
        invs, summaries, model, key, verbose=True
    )
    surviving = [i for i in invs if i.get("id") in surviving_ids] or invs
    print(
        f"  skeptic kept {len(surviving)}/{len(invs)} (falsified: {falsified or 'none'})"
    )

    agent._setup_project(name, src)
    test_src = render_tests(name, src, surviving)
    built = False
    out = ""
    for attempt in range(4):
        built, out = agent._build(test_src)
        if built:
            break
        print(f"  build attempt {attempt+1} failed; repairing…")
        test_src = agent._ask(
            agent._repair_prompt(test_src, "\n".join(out.splitlines()[-25:]))
        )
    verdicts = run_halmos(agent.PROJECT) if built else []
    return {"built": built, "verdicts": verdicts}


if __name__ == "__main__":
    if len(sys.argv) > 2:
        path, name = sys.argv[1], sys.argv[2]
    else:
        os.makedirs("/tmp/si_demo", exist_ok=True)
        path = "/tmp/recall_panel/LendingPool.sol"
        name = "LendingPool"
    print(f"solidity_intent (pact skeptic + Halmos): {name}\n")
    res = run(path, name)
    if not res["built"]:
        print("could not build a valid Halmos test")
        sys.exit(1)
    print()
    for v in res["verdicts"]:
        mark = "✅ PROVED  " if v["proved"] else "🔴 VIOLATED"
        print(
            f"  {mark} {v['function']}"
            + ("" if v["proved"] else f"  {v['counterexample']}")
        )
    bugs = [v for v in res["verdicts"] if not v["proved"]]
    print(
        f"\n{len(bugs)}/{len(res['verdicts'])} surviving invariants violated (post-skeptic, EVM-real)."
    )
