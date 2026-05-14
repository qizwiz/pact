"""
Tests for the Z3 Fixedpoint engine.

We use synthetic Python source fixtures so tests don't depend on the
futureagi/ app structure and run in milliseconds.
"""

import textwrap
import tempfile
from pathlib import Path


from .z3_engine import PactEngine


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

import pytest

try:
    import z3 as _z3
    _HAS_Z3 = True
except ImportError:
    _HAS_Z3 = False

pytestmark_z3 = pytest.mark.skipif(not _HAS_Z3, reason="z3-solver not installed")


@pytestmark_z3
def test_llm_response_unguarded_proof():
    """Z3 must confirm: bug SAT (IndexError reachable), fix UNSAT (guard seals it)."""
    from .prover import prove_llm_response_unguarded
    cert = prove_llm_response_unguarded()
    assert cert.bug_sat,   "Bug scenario must be SAT — IndexError must be reachable"
    assert cert.fix_unsat, "Fix scenario must be UNSAT — guard must seal all paths"
    assert cert.witness,   "Must have a concrete witness (trigger + choices_len=0)"


@pytestmark_z3
def test_save_without_update_fields_proof():
    from .prover import prove_save_without_update_fields
    cert = prove_save_without_update_fields()
    assert cert.bug_sat and cert.fix_unsat


@pytestmark_z3
def test_missing_await_proof():
    from .prover import prove_missing_await
    cert = prove_missing_await()
    assert cert.bug_sat and cert.fix_unsat


@pytestmark_z3
def test_optional_dereference_proof():
    from .prover import prove_optional_dereference
    cert = prove_optional_dereference()
    assert cert.bug_sat and cert.fix_unsat
