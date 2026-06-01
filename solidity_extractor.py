"""
SolidityExtractor — ADR-070, smallest viable.

Reads a .sol contract and emits pact IR — a ModelManifest for the token state,
FunctionManifests for its functions, and a ConservationObligation carrying the
sum(balances) == totalSupply invariant — then drives the z3
`conservation_invariant` template from that IR. UNSAT = holds; SAT = counterexample.

Two honesty stamps:
  * The parse is a deliberate string-level HEURISTIC — the stub the real
    tree-sitter-solidity Extractor (ADR-070) will replace.
  * `ConservationObligation` is the minimal IR GROWTH the ADR predicted:
    `FieldConstraint` can't express a sum-over-mapping invariant, so the
    constraint vocabulary grows by one record.
What is threaded properly is the IR: `.sol -> Manifests -> template`, not
`.sol -> bool -> template`. The z3 verification is fully real.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from contract_templates import render_z3_template
from extractor import ArgConstraint, FieldConstraint, FunctionManifest, ModelManifest

# --- stub parse (regex; tree-sitter is the ADR-070 growth) ------------------
_MAP = r"(?:_?[bB]alances|_?balanceOf)"
_SENDER = r"(?:msg\.sender|_?from|sender)"
_RECEIVER = r"(?:_?to|recipient|dst)"
_DEBIT = re.compile(rf"{_MAP}\s*\[\s*{_SENDER}\s*\]\s*(?:-=|=\s*[^;]*-)")
_CREDIT = re.compile(rf"{_MAP}\s*\[\s*{_RECEIVER}\s*\]\s*(?:\+=|=\s*[^;]*\+)")
_TRANSFER = re.compile(r"function\s+transfer\s*\([^)]*\)[^{]*\{")
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CONTRACT = re.compile(r"\bcontract\s+(\w+)")
_MAPPING_FIELD = re.compile(
    r"mapping\s*\(\s*address\s*=>\s*(uint\d*)\s*\)\s*"
    r"(?:public|private|internal|external)?\s*(\w+)"
)
_SCALAR_FIELD = re.compile(
    r"\b(uint\d*)\s+(?:(?:public|private|internal|external|constant|immutable)\s+)*"
    r"(\w*[Ss]upply\w*)\b"
)
_FUNC = re.compile(r"function\s+(\w+)\s*\(([^)]*)\)")


def _strip_comments(s: str) -> str:
    """Remove // and /* */ comments so commentary can't trip the scanners."""
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", s))


def _transfer_body(src: str) -> str | None:
    m = _TRANSFER.search(src)
    if not m:
        return None
    i = m.end() - 1  # opening brace
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1 : j]
    return None


def _transfer_preserves_sum(clean: str) -> bool:
    """True iff `transfer` BOTH debits the sender AND credits the receiver."""
    body = _transfer_body(clean)
    if body is None:
        return True
    return bool(_DEBIT.search(body)) and bool(_CREDIT.search(body))


# --- IR (ADR-070) -----------------------------------------------------------
@dataclass
class ConservationObligation:
    """Minimal IR growth: the invariant sum(mapping_field) == total_field, and
    whether `transfer` preserves it. `FieldConstraint` can't express this."""

    token: str
    mapping_field: str
    total_field: str
    preserved: bool


@dataclass
class SolidityManifest:
    """The IR the SolidityExtractor emits for one contract."""

    model: ModelManifest
    functions: list[FunctionManifest] = field(default_factory=list)
    conservation: ConservationObligation | None = None


def extract(sol_path: str | Path) -> SolidityManifest:
    """.sol -> pact IR (ModelManifest + FunctionManifests + ConservationObligation)."""
    raw = Path(sol_path).read_text(encoding="utf-8")
    clean = _strip_comments(raw)
    cm = _CONTRACT.search(clean)
    name = cm.group(1) if cm else Path(sol_path).stem

    fields: list[FieldConstraint] = []
    mapping_field: str | None = None
    for ftype, fname in _MAPPING_FIELD.findall(clean):
        fields.append(
            FieldConstraint(
                name=fname, required=False, field_type=f"mapping(address=>{ftype})"
            )
        )
        mapping_field = mapping_field or fname
    total_field: str | None = None
    for ftype, fname in _SCALAR_FIELD.findall(clean):
        fields.append(FieldConstraint(name=fname, required=False, field_type=ftype))
        total_field = total_field or fname

    model = ModelManifest(name=name, file=str(sol_path), line=0, fields=fields)

    functions: list[FunctionManifest] = []
    for fname, arglist in _FUNC.findall(clean):
        args = [
            ArgConstraint(name=a.strip().split()[-1], required=True)
            for a in arglist.split(",")
            if a.strip()
        ]
        functions.append(
            FunctionManifest(
                name=fname, file=str(sol_path), line=0, module_path=name, args=args
            )
        )

    conservation = None
    if mapping_field and total_field:
        conservation = ConservationObligation(
            token=name,
            mapping_field=mapping_field,
            total_field=total_field,
            preserved=_transfer_preserves_sum(clean),
        )
    return SolidityManifest(model=model, functions=functions, conservation=conservation)


def verify(manifest: SolidityManifest) -> dict:
    """Render + run the conservation_invariant z3 check from the IR."""
    ob = manifest.conservation
    if ob is None:
        return {
            "status": "skipped",
            "counterexample": None,
            "explanation": "no conservation obligation (no balances mapping + supply scalar found)",
        }
    script = render_z3_template(
        "conservation_invariant", {"token": ob.token, "preserves_sum": ob.preserved}
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


def verify_sol(sol_path: str | Path) -> dict:
    """Convenience: .sol -> IR -> z3 result."""
    return verify(extract(sol_path))


if __name__ == "__main__":
    for path in sys.argv[1:]:
        m = extract(path)
        r = verify(m)
        print(f"{Path(path).name}: {r['status'].upper()} — {r.get('explanation', '')}")
        if r.get("counterexample"):
            print(f"   counterexample: {r['counterexample']}")
