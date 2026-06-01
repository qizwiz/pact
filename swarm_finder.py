"""
swarm_finder — soft-BFT adversarial finding layer (experiment).

Pipeline:
  1. PROPOSER (precision-tuned): proposes candidate vulns, each backed by an exploit.
  2. CHALLENGERS: for each candidate, N *isolated* adversarial skeptics — separate
     API calls that each see ONLY {code, this one candidate}. No shared state, no
     view of each other or of the proposer's reasoning → they cannot collude.
  3. ORACLE (soft): a candidate SURVIVES only if a majority of skeptics fail to
     refute it (>= ceil(N/2) "REAL").

HONESTY: this is *soft* BFT — the challengers are the same model, so they share
training-induced bias (it reduces variance, not bias). The SOUND oracle is z3 /
a runnable PoC, which is the deferred hard layer. Survivors here "withstood the
soft filter," NOT "proven."

    .venv/bin/python swarm_finder.py <contract.sol> [<contract2.sol> ...]
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
from llm import make_client, resolve_model  # noqa: E402

_CLIENT = make_client()
_MODEL = resolve_model()
N_CHALLENGERS = 3


def _call(prompt: str, max_tokens: int = 1200) -> str:
    r = _CLIENT.messages.create(
        model=_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text if r.content else ""


def _parse(txt: str):
    txt = txt.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"```\s*$", "", txt).strip()
    m = re.search(r"[\[{].*[\]}]", txt, re.S)
    try:
        return json.loads(m.group(0) if m else txt)
    except Exception:
        return None


def propose(src: str, n: int = 3) -> list[dict]:
    p = (
        f"You are auditing this Solidity contract. Propose up to {n} candidate vulnerabilities — "
        "ONLY ones you can back with a concrete, reachable exploit. No speculation, no style nits. "
        'Return ONLY a JSON array of {"id","title","where","claim","exploit"}.\n\nCONTRACT:\n'
        + src
    )
    out = _parse(_call(p)) or []
    return out if isinstance(out, list) else []


def challenge(src: str, cand: dict) -> dict:
    # ISOLATED: this skeptic sees only the code + ONE candidate. Fresh call → no collusion.
    p = (
        "You are an adversarial security skeptic. A colleague claims the contract below has this bug. "
        "Your job is to REFUTE it. Decide: is it ACTUALLY real and reachable against THIS code, or is it "
        "wrong, vacuous, non-exploitable, or a misread? Default to REFUTED unless the exploit clearly holds. "
        'Return ONLY JSON {"verdict":"REAL"|"REFUTED","reason":"..."}.\n\n'
        "CLAIMED BUG:\n" + json.dumps(cand) + "\n\nCONTRACT:\n" + src
    )
    v = _parse(_call(p, 500)) or {}
    return v if isinstance(v, dict) else {}


def run(path: str) -> None:
    src = open(path).read()
    name = os.path.basename(path)
    cands = propose(src, 3)
    print(f"\n=== {name}: proposer raised {len(cands)} candidate(s) ===")
    survivors = 0
    for c in cands:
        verdicts = [challenge(src, c) for _ in range(N_CHALLENGERS)]
        reals = sum(1 for v in verdicts if str(v.get("verdict", "")).upper() == "REAL")
        survived = reals >= (N_CHALLENGERS // 2 + 1)
        survivors += survived
        flag = "🟢 SURVIVES" if survived else "🔴 KILLED  "
        print(
            f"  {flag}  [{reals}/{N_CHALLENGERS} skeptics say REAL]  {c.get('title','?')}"
        )
    print(
        f"  --> {survivors}/{len(cands)} candidate(s) survived the adversarial filter"
    )


if __name__ == "__main__":
    for path in sys.argv[1:]:
        run(path)
