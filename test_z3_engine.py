"""
Tests for the Z3 Fixedpoint engine.

We use synthetic Python source fixtures so tests don't depend on the
futureagi/ app structure and run in milliseconds.
"""

import importlib.util
import textwrap
import tempfile
from pathlib import Path

import pytest


from pact.z3_engine import LLMResponseEngine, PactEngine, verify_file


def _make_fixture(source: str) -> Path:
    """Write a single Python source string to a temp file and return its directory."""
    d = Path(tempfile.mkdtemp())
    (d / "models.py").write_text(textwrap.dedent(source))
    return d


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _run(source: str) -> list:
    root = _make_fixture(source)
    engine = PactEngine()
    engine.load(root)
    return engine.violations()


# ──────────────────────────────────────────────────────────────────────────────
# Happy-path: no violations
# ──────────────────────────────────────────────────────────────────────────────


def test_no_violation_when_all_required_fields_provided():
    viols = _run("""
        import django.db.models as m

        class Widget(m.Model):
            name = m.CharField(max_length=100)
            count = m.IntegerField()

        Widget.objects.create(name='foo', count=3)
    """)
    assert viols == []


def test_optional_field_not_required():
    viols = _run("""
        import django.db.models as m

        class Widget(m.Model):
            name = m.CharField(max_length=100)
            notes = m.TextField(blank=True, null=True)

        Widget.objects.create(name='foo')
    """)
    assert viols == []


def test_field_with_default_not_required():
    viols = _run("""
        import django.db.models as m

        class Widget(m.Model):
            name = m.CharField(max_length=100)
            active = m.BooleanField(default=True)

        Widget.objects.create(name='bar')
    """)
    assert viols == []


# ──────────────────────────────────────────────────────────────────────────────
# Single violation
# ──────────────────────────────────────────────────────────────────────────────


def test_missing_required_field_detected():
    viols = _run("""
        import django.db.models as m

        class Widget(m.Model):
            name = m.CharField(max_length=100)

        Widget.objects.create()
    """)
    assert len(viols) == 1
    assert "name" in viols[0].missing


def test_violation_reports_correct_file_and_line():
    source = textwrap.dedent("""
        import django.db.models as m

        class Widget(m.Model):
            name = m.CharField(max_length=100)

        Widget.objects.create()
    """).lstrip()

    root = Path(tempfile.mkdtemp())
    fixture = root / "models.py"
    fixture.write_text(source)

    engine = PactEngine()
    engine.load(root)
    viols = engine.violations()

    assert len(viols) == 1
    assert viols[0].file == str(fixture)
    assert viols[0].line == 6  # Widget.objects.create() is line 6


def test_multiple_missing_fields_all_reported():
    viols = _run("""
        import django.db.models as m

        class Order(m.Model):
            user = m.CharField(max_length=50)
            total = m.IntegerField()
            status = m.CharField(max_length=20)

        Order.objects.create(user='alice')
    """)
    assert len(viols) == 1
    assert set(viols[0].missing) == {"total", "status"}


# ──────────────────────────────────────────────────────────────────────────────
# Multiple sites
# ──────────────────────────────────────────────────────────────────────────────


def test_only_bad_site_flagged_not_good_site():
    viols = _run("""
        import django.db.models as m

        class Widget(m.Model):
            name = m.CharField(max_length=100)

        Widget.objects.create(name='ok')     # line 7 — clean
        Widget.objects.create()              # line 8 — violation
    """)
    assert len(viols) == 1
    assert viols[0].line == 8


def test_two_models_only_bad_create_flagged():
    viols = _run("""
        import django.db.models as m

        class Widget(m.Model):
            name = m.CharField(max_length=100)

        class Gadget(m.Model):
            title = m.CharField(max_length=200)

        Widget.objects.create(name='w')      # clean
        Gadget.objects.create()              # missing title
    """)
    assert len(viols) == 1
    assert "title" in viols[0].missing
    assert "Widget" not in viols[0].call


# ──────────────────────────────────────────────────────────────────────────────
# Cross-file: model defined in one file, create() in another
# ──────────────────────────────────────────────────────────────────────────────


def test_cross_file_violation_detected():
    d = Path(tempfile.mkdtemp())
    (d / "mymodels.py").write_text(textwrap.dedent("""
        import django.db.models as m

        class Widget(m.Model):
            name = m.CharField(max_length=100)
    """))
    (d / "views.py").write_text(textwrap.dedent("""
        from mymodels import Widget

        def create_widget():
            Widget.objects.create()   # missing name
    """))
    engine = PactEngine()
    engine.load(d)
    viols = engine.violations()
    assert len(viols) == 1
    assert "name" in viols[0].missing
    assert "views.py" in viols[0].file


# ──────────────────────────────────────────────────────────────────────────────
# Unknown model — no violation (open-world assumption)
# ──────────────────────────────────────────────────────────────────────────────


def test_unknown_model_not_flagged():
    viols = _run("""
        ExternalModel.objects.create()
    """)
    assert viols == []


# ──────────────────────────────────────────────────────────────────────────────
# prover.py — proof certificates
# ──────────────────────────────────────────────────────────────────────────────

_HAS_Z3 = importlib.util.find_spec("z3") is not None

pytestmark_z3 = pytest.mark.skipif(not _HAS_Z3, reason="z3-solver not installed")


@pytestmark_z3
def test_llm_response_unguarded_proof():
    """Z3 must confirm: bug SAT (IndexError reachable), fix UNSAT (guard seals it)."""
    from pact.prover import prove_llm_response_unguarded

    cert = prove_llm_response_unguarded()
    assert cert.bug_sat, "Bug scenario must be SAT — IndexError must be reachable"
    assert cert.fix_unsat, "Fix scenario must be UNSAT — guard must seal all paths"
    assert cert.witness, "Must have a concrete witness (trigger + choices_len=0)"


@pytestmark_z3
def test_save_without_update_fields_proof():
    from pact.prover import prove_save_without_update_fields

    cert = prove_save_without_update_fields()
    assert cert.bug_sat and cert.fix_unsat


@pytestmark_z3
def test_missing_await_proof():
    from pact.prover import prove_missing_await

    cert = prove_missing_await()
    assert cert.bug_sat and cert.fix_unsat


@pytestmark_z3
def test_optional_dereference_proof():
    from pact.prover import prove_optional_dereference

    cert = prove_optional_dereference()
    assert cert.bug_sat and cert.fix_unsat


@pytestmark_z3
def test_bare_except_proof():
    """Silent failure is SAT; specific catch makes it UNSAT."""
    from pact.prover import prove_bare_except

    cert = prove_bare_except()
    assert cert.bug_sat, "Bug: bare except swallowing real exception must be SAT"
    assert cert.fix_unsat, "Fix: specific catch must make silent failure UNSAT"
    assert cert.witness, "Must have concrete witness (caught=True, pass body)"


@pytestmark_z3
def test_mutable_default_arg_proof():
    """State leakage across calls is SAT; None sentinel makes it UNSAT."""
    from pact.prover import prove_mutable_default_arg

    cert = prove_mutable_default_arg()
    assert cert.bug_sat, "Bug: shared mutable default leaking state must be SAT"
    assert cert.fix_unsat, "Fix: fresh object per call must make leakage UNSAT"
    assert cert.witness, "Must have concrete witness (call2_initial_len > 0)"


@pytestmark_z3
def test_required_arg_missing_proof():
    """TypeError from underprovision is SAT; providing all args makes it UNSAT."""
    from pact.prover import prove_required_arg_missing

    cert = prove_required_arg_missing()
    assert cert.bug_sat, "Bug: provided < required must be SAT"
    assert cert.fix_unsat, "Fix: provided >= required must make error UNSAT"
    assert cert.witness, "Must have concrete witness (required and provided counts)"


@pytestmark_z3
def test_format_arg_mismatch_proof():
    """Format slot/arg count mismatch is SAT; matching counts makes it UNSAT."""
    from pact.prover import prove_format_arg_mismatch

    cert = prove_format_arg_mismatch()
    assert cert.bug_sat, "Bug: slots != supplied must be SAT"
    assert cert.fix_unsat, "Fix: slots == supplied must make error UNSAT"
    assert cert.witness, "Must have concrete witness (slot and arg counts)"


@pytestmark_z3
def test_unvalidated_lookup_chain_proof():
    """KeyError from absent chain key is SAT; .get() with default makes it UNSAT."""
    from pact.prover import prove_unvalidated_lookup_chain

    cert = prove_unvalidated_lookup_chain()
    assert cert.bug_sat, "Bug: absent key in chain must be SAT"
    assert cert.fix_unsat, "Fix: .get() with default must make KeyError UNSAT"
    assert cert.witness, "Must have concrete witness (which key is absent)"


# ──────────────────────────────────────────────────────────────────────────────
# LLMResponseEngine — Datalog verifier for llm_response_unguarded
# ──────────────────────────────────────────────────────────────────────────────


def _llm_run(source: str):
    d = Path(tempfile.mkdtemp())
    (d / "llm_code.py").write_text(textwrap.dedent(source))
    engine = LLMResponseEngine()
    engine.load(d)
    return engine.result()


def test_llm_engine_proved_safe_on_guarded_code():
    result = _llm_run("""
        def call_llm(client):
            response = client.chat.completions.create(model="gpt-4o", messages=[])
            if not response.choices:
                return None
            return response.choices[0].message.content
    """)
    assert result.proved_safe
    assert result.violations == []
    assert result.scopes_analyzed >= 1


def test_llm_engine_unsafe_on_unguarded_code():
    result = _llm_run("""
        def bad(client):
            response = client.completions.create(model="gpt-4o", messages=[])
            return response.choices[0].message.content
    """)
    assert not result.proved_safe
    assert len(result.violations) == 1
    assert "choices[0]" in result.violations[0].call


def test_llm_engine_safe_on_no_llm_calls():
    result = _llm_run("""
        def pure_math(x, y):
            return x + y
    """)
    assert result.proved_safe
    assert result.scopes_analyzed == 0


def test_llm_engine_str_safe():
    result = _llm_run("""
        def ok(client):
            response = client.messages.create(model="claude-3", messages=[])
            if not response.choices or response.choices[0].message is None:
                raise ValueError("empty response")
            return response.choices[0].message.content
    """)
    assert result.proved_safe
    assert "SAFE" in str(result)


def test_llm_engine_str_unsafe():
    result = _llm_run("""
        def bad(client):
            response = client.generate(prompt="hello")
            return response.candidates[0].text
    """)
    assert not result.proved_safe
    assert "UNSAFE" in str(result)


# ──────────────────────────────────────────────────────────────────────────────
# verify_file — per-file Z3 patch certification
# ──────────────────────────────────────────────────────────────────────────────


def _write_file(source: str) -> str:
    """Write dedented source to a temp .py file, return its path."""
    d = Path(tempfile.mkdtemp())
    p = d / "code.py"
    p.write_text(textwrap.dedent(source))
    return str(p)


def test_verify_file_proved_safe_on_guarded_code():
    path = _write_file("""
        def call_llm(client):
            response = client.chat.completions.create(model="gpt-4o", messages=[])
            if not response.choices:
                return None
            return response.choices[0].message.content
    """)
    result = verify_file(path)
    assert result.proved_safe
    assert result.violations == []


def test_verify_file_unsafe_on_unguarded_code():
    path = _write_file("""
        def bad(client):
            response = client.completions.create(model="gpt-4o", messages=[])
            return response.choices[0].message.content
    """)
    result = verify_file(path)
    assert not result.proved_safe
    assert len(result.violations) >= 1


def test_verify_file_safe_on_no_llm_calls():
    path = _write_file("""
        def pure_math(x, y):
            return x + y
    """)
    result = verify_file(path)
    assert result.proved_safe
    assert result.scopes_analyzed == 0


def test_verify_file_certifies_fixer_output(tmp_path):
    """After fixer applies a guard, verify_file should prove the patched file safe."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from pact.failure_mode import FailureEvidence
    from pact.fixer import fix_file

    source = textwrap.dedent("""
        def call_llm(client):
            response = client.chat.completions.create(model="gpt-4o", messages=[])
            return response.choices[0].message.content
    """).lstrip()
    f = tmp_path / "code.py"
    f.write_text(source)
    ev = FailureEvidence(
        mode_name="llm_response_unguarded",
        file=str(f),
        line=3,
        call="response.choices[0]",
        message="",
    )
    result = fix_file(str(f), [ev])
    assert result.changed
    f.write_text(result.patched)
    proof = verify_file(str(f))
    assert proof.proved_safe, f"Z3 still finds violations after fix: {proof.violations}"
