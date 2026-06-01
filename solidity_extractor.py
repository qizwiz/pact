"""
SolidityExtractor — smallest-viable stub (ADR-070).

Reads a .sol contract, decides whether `transfer` preserves the conservation
invariant  sum(balances) == totalSupply,  and drives the z3
`conservation_invariant` template.  UNSAT = invariant holds; SAT = counterexample.

This is the SMALLEST viable magic: a real .sol in → a z3 green/red out. The parse
here is a deliberate string-level HEURISTIC — the stub the real
tree-sitter-solidity Extractor (ADR-070) will replace. The z3 verification is
fully real; only the front-door parse is a stub.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from contract_templates import render_z3_template

_MAP = r"(?:_?[bB]alances|_?balanceOf)"
_SENDER = r"(?:msg\.sender|_?from|sender)"
_RECEIVER = r"(?:_?to|recipient|dst)"

_DEBIT = re.compile(rf"{_MAP}\s*\[\s*{_SENDER}\s*\]\s*(?:-=|=\s*[^;]*-)")
_CREDIT = re.compile(rf"{_MAP}\s*\[\s*{_RECEIVER}\s*\]\s*(?:\+=|=\s*[^;]*\+)")
_TRANSFER = re.compile(r"function\s+transfer\s*\([^)]*\)[^{]*\{")

_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _strip_comments(s: str) -> str:
    """Remove // and /* */ comments so commentary can't trip the scanners."""
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", s))


def _transfer_body(src: str) -> str | None:
    """Return the body of the first `transfer(...)` function, brace-matched."""
    m = _TRANSFER.search(src)
    if not m:
        return None
    i = m.end() - 1  # index of the opening brace
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1 : j]
    return None


def transfer_preserves_sum(src: str) -> bool:
    """True iff `transfer` BOTH debits the sender AND credits the receiver."""
    body = _transfer_body(_strip_comments(src))
    if body is None:
        return True  # no transfer → nothing to break the invariant
    return bool(_DEBIT.search(body)) and bool(_CREDIT.search(body))


def verify_sol(sol_path: str | Path) -> dict:
    """Full pipeline: parse .sol → derive param → render z3 → run → result dict."""
    src = Path(sol_path).read_text(encoding="utf-8")
    token = Path(sol_path).stem
    preserves = transfer_preserves_sum(src)
    script = render_z3_template(
        "conservation_invariant", {"token": token, "preserves_sum": preserves}
    )
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(script)
        p = f.name
    try:
        out = subprocess.run(
            [sys.executable, p], capture_output=True, text=True, timeout=30
        )
    finally:
        os.unlink(p)
    return json.loads(out.stdout.strip())


if __name__ == "__main__":
    for path in sys.argv[1:]:
        res = verify_sol(path)
        print(f"{Path(path).name}: {res['status'].upper()} — {res['explanation']}")
        if res.get("counterexample"):
            print(f"   counterexample: {res['counterexample']}")
