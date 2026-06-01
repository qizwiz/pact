"""Behavioral tests for the Solidity conservation layer.

These cover the *semantics* (not just "it renders") of:
  - the ``conservation_invariant`` z3 template — SAT iff a transfer can break
    ``sum(balances) == total_supply`` (value created/destroyed);
  - ``SolidityExtractor`` — parse a .sol file into a manifest + conservation
    obligation, then discharge it with z3.

Dependencies: only z3-solver + stdlib (no API key, no network, no forge), so
they are safe to run in CI on every push.
"""

import json
import subprocess
import sys
from pathlib import Path

from .contract_templates import SUPPORTED_KINDS, render_z3_template
from .solidity_extractor import extract, verify

EX = Path(__file__).resolve().parent / "examples" / "solidity"


def _run_template(preserves_sum: bool) -> dict:
    """Render the conservation template and actually run it through z3."""
    src = render_z3_template(
        "conservation_invariant", {"preserves_sum": preserves_sum, "token": "Tok"}
    )
    proc = subprocess.run(
        [sys.executable, "-c", src], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"z3 script crashed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


# ---------------------------------------------------------------------------
# conservation_invariant template
# ---------------------------------------------------------------------------


class TestConservationTemplate:
    def test_kind_is_registered(self):
        assert "conservation_invariant" in SUPPORTED_KINDS

    def test_preserving_transfer_is_unsat(self):
        # debit + credit cancel → conservation cannot be broken → UNSAT.
        out = _run_template(preserves_sum=True)
        assert out["status"] == "unsat"
        assert out["counterexample"] is None

    def test_minting_transfer_is_sat_with_witness(self):
        # credit-without-debit → z3 finds a state where the sum diverges.
        out = _run_template(preserves_sum=False)
        assert out["status"] == "sat"
        ce = out["counterexample"]
        assert ce is not None
        # the witness must actually show value created: sum != total_supply.
        assert int(ce["sum_balances_after"]) != int(ce["total_supply"])


# ---------------------------------------------------------------------------
# SolidityExtractor  (.sol -> manifest -> z3 verdict)
# ---------------------------------------------------------------------------


class TestSolidityExtractor:
    def test_badtoken_obligation_fails(self):
        m = extract(EX / "BadToken.sol")
        assert m.conservation is not None
        assert m.conservation.preserved is False
        v = verify(m)
        assert v["status"] == "sat"
        assert v["counterexample"] is not None

    def test_goodtoken_obligation_holds(self):
        m = extract(EX / "GoodToken.sol")
        assert m.conservation is not None
        assert m.conservation.preserved is True
        v = verify(m)
        assert v["status"] == "unsat"

    def test_extracts_balances_and_supply_fields(self):
        m = extract(EX / "BadToken.sol")
        names = {f.name for f in m.model.fields}
        assert {"balances", "totalSupply"} <= names

    def test_no_obligation_when_no_supply_scalar(self):
        # YieldBank tracks shares but has no balances+supply pair → nothing to prove.
        m = extract(EX / "YieldBank.sol")
        assert m.conservation is None
        assert verify(m)["status"] == "skipped"
