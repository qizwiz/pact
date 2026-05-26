"""Tests for pact spec gen (specgen.py)."""

from .specgen import synthesize

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(src: str, name: str = "Test") -> str:
    return synthesize(src, name)


# ---------------------------------------------------------------------------
# Model extraction
# ---------------------------------------------------------------------------


class TestModelExtraction:
    def test_detects_django_model_class(self):
        src = """
from django.db import models
class Widget(models.Model):
    name = models.CharField(max_length=100)
    price = models.IntegerField()
"""
        spec = _spec(src)
        assert "VARIABLES" in spec
        assert "widgets" in spec
        assert "SET OF Widget records" in spec

    def test_string_field_maps_to_STRING(self):
        src = """
from django.db import models
class Foo(models.Model):
    title = models.CharField(max_length=50)
"""
        spec = _spec(src)
        assert "r.title \\in STRING" in spec

    def test_integer_field_maps_to_Int(self):
        src = """
from django.db import models
class Foo(models.Model):
    count = models.IntegerField()
"""
        spec = _spec(src)
        assert "r.count \\in Int" in spec

    def test_positive_integer_maps_to_Nat(self):
        src = """
from django.db import models
class Foo(models.Model):
    retries = models.PositiveIntegerField()
"""
        spec = _spec(src)
        assert "r.retries \\in Nat" in spec

    def test_boolean_field_maps_to_BOOLEAN(self):
        src = """
from django.db import models
class Foo(models.Model):
    active = models.BooleanField(default=True)
"""
        spec = _spec(src)
        assert "r.active \\in BOOLEAN" in spec

    def test_nullable_field_includes_NULL(self):
        src = """
from django.db import models
class Foo(models.Model):
    notes = models.TextField(null=True)
"""
        spec = _spec(src)
        assert '"NULL"' in spec

    def test_init_starts_empty(self):
        src = """
from django.db import models
class Foo(models.Model):
    x = models.CharField()
"""
        spec = _spec(src)
        assert "foos = {}" in spec

    def test_no_models_produces_stub(self):
        src = "x = 1\n"
        spec = _spec(src)
        assert "no Django models or Celery tasks found" in spec


# ---------------------------------------------------------------------------
# Unique constraints
# ---------------------------------------------------------------------------


class TestUniqueConstraints:
    def test_unique_together_extracted(self):
        src = """
from django.db import models
class Booking(models.Model):
    user = models.ForeignKey('User', on_delete=models.CASCADE)
    slot = models.IntegerField()
    class Meta:
        unique_together = [['user', 'slot']]
"""
        spec = _spec(src)
        assert "UserSlotUnique" in spec
        assert "r1.user" in spec
        assert "r1.slot" in spec

    def test_unique_constraint_object_extracted(self):
        src = """
from django.db import models
from django.db.models import Q
class Config(models.Model):
    dataset = models.ForeignKey('Dataset', on_delete=models.CASCADE)
    template = models.ForeignKey('Template', on_delete=models.CASCADE)
    class Meta:
        constraints = [
            models.UniqueConstraint(
                condition=Q(deleted=False),
                fields=['dataset', 'template'],
                name='unique_active_config',
            )
        ]
"""
        spec = _spec(src)
        assert "DatasetTemplateUnique" in spec


# ---------------------------------------------------------------------------
# Task extraction
# ---------------------------------------------------------------------------


class TestTaskExtraction:
    def test_shared_task_produces_action(self):
        src = """
from celery import shared_task
@shared_task
def process_batch(batch_id):
    pass
"""
        spec = _spec(src)
        assert "ProcessBatch" in spec
        assert "Corresponds to: @shared_task process_batch()" in spec

    def test_task_with_args_quantified_in_next(self):
        src = """
from celery import shared_task
@shared_task
def run_eval(job_id, model_id):
    pass
"""
        spec = _spec(src)
        assert "\\E _job_id \\in STRING" in spec
        assert "\\E _model_id \\in STRING" in spec
        assert "RunEval(_job_id, _model_id)" in spec

    def test_no_arg_task_in_next_no_quantifier(self):
        src = """
from celery import shared_task
@shared_task
def heartbeat():
    pass
"""
        spec = _spec(src)
        assert "\\/ Heartbeat" in spec
        # No \\E for no-arg task
        assert "\\E" not in spec

    def test_cache_get_and_set_flags_non_atomic(self):
        src = """
from celery import shared_task
from django.core.cache import cache
@shared_task
def update_counter(key):
    val = cache.get(key)
    cache.set(key, val + 1)
"""
        spec = _spec(src)
        assert "non-atomic" in spec
        assert "split step" in spec

    def test_cache_get_only_no_warning(self):
        src = """
from celery import shared_task
from django.core.cache import cache
@shared_task
def read_config(key):
    return cache.get(key)
"""
        spec = _spec(src)
        assert "non-atomic" not in spec


# ---------------------------------------------------------------------------
# Spec structure
# ---------------------------------------------------------------------------


class TestSpecStructure:
    def test_module_header_present(self):
        src = "from django.db import models\nclass X(models.Model):\n    n = models.IntegerField()\n"
        spec = _spec(src, "MyMod")
        assert "MODULE MyMod" in spec

    def test_extends_line_present(self):
        src = "x = 1\nclass A(models.Model): n = models.IntegerField()\n"
        spec = _spec(src)
        assert "EXTENDS Naturals, Sequences, FiniteSets, TLC" in spec

    def test_type_invariant_declared(self):
        src = "from django.db import models\nclass Y(models.Model):\n    v = models.CharField()\n"
        spec = _spec(src)
        assert "INVARIANT TypeInvariant" in spec

    def test_spec_formula_present(self):
        src = "from django.db import models\nclass Z(models.Model):\n    k = models.IntegerField()\n"
        spec = _spec(src)
        assert "Spec ==" in spec
        assert "Init" in spec
        assert "[][Next]_" in spec

    def test_placeholder_variable_when_tasks_only(self):
        src = """
from celery import shared_task
@shared_task
def do_work():
    pass
"""
        spec = _spec(src)
        assert "task_state" in spec
        assert "WF_<<task_state>>(Next)" in spec


# ---------------------------------------------------------------------------
# TLC ground-truth execution
# ---------------------------------------------------------------------------

# Minimal self-contained TLA+ spec with no CONSTANTS and a finite state space
# (2 states: flag \in BOOLEAN).  Uses only built-in TLA+ — no EXTENDS needed.
_MINIMAL_CLEAN_SPEC = """\
----------------------------- MODULE PactToggle -----------------------------
VARIABLES flag
Init == flag = FALSE
Next == flag' = ~flag
Spec == Init /\\ [][Next]_flag
TypeInvariant == flag \\in BOOLEAN
=============================================================================
"""


def _tlc_available() -> bool:
    import shutil
    from pathlib import Path

    return (
        bool(shutil.which("java"))
        and (Path.home() / ".local" / "share" / "tla2tools.jar").exists()
    )


def test_run_tlc_clean_spec():
    """TLC reports CLEAN for a simple counter spec with no violations."""
    if not _tlc_available():
        import pytest

        pytest.skip("java or tla2tools.jar not available")

    from .spec_learner import SpecGapRecord, _run_tlc_on_spec

    record = SpecGapRecord(
        tla_spec_text=_MINIMAL_CLEAN_SPEC,
        tlc_config_additions="INVARIANTS TypeInvariant",
    )
    result = _run_tlc_on_spec(record, verbose=False)
    assert (
        result.tlc_actual_result == "CLEAN"
    ), f"counter spec should be clean; got: {result.tlc_actual_result}"


def test_run_tlc_skips_when_no_spec():
    """_run_tlc_on_spec is a no-op when tla_spec_text is empty."""
    from .spec_learner import SpecGapRecord, _run_tlc_on_spec

    record = SpecGapRecord(tla_spec_text="")
    result = _run_tlc_on_spec(record, verbose=False)
    assert result.tlc_actual_result == ""


# ---------------------------------------------------------------------------
# ADR → import-linter contract synthesis
# ---------------------------------------------------------------------------


class TestADRContractSynthesis:
    """Tests for _extract_adr_rule, _emit_importlinter_contract, and wiring."""

    _ADR_WITH_RULE = """\
# ADR 7 — API layer must not import database layer

## Status
Accepted

## Context
The API handlers in `api/` were directly importing from `db/` models, creating a
tight coupling that made it impossible to swap storage backends.

## Decision
The `api` package must never import directly from the `db` package.
All database access must go through the `services` layer.

## Evidence
- `api/views.py`
- `db/models.py`
"""

    _ADR_NO_RULE = """\
# ADR 12 — Use UTC timestamps everywhere

## Status
Accepted

## Context
Inconsistent timezone handling was causing subtle bugs in reporting.

## Decision
All datetime objects stored in the database must be timezone-aware UTC.
Use `django.utils.timezone.now()` instead of `datetime.datetime.now()`.
"""

    def _patch_call(self, monkeypatch, return_json: dict):
        """Patch _call on the same module object that _extract_adr_rule lives in.

        pytest --import-mode=importlib may load intent.py under a different
        dotted name (pact-standalone.intent vs pact.intent), producing two
        distinct module objects for the same file.  Patching by __module__ name
        ensures we always hit the module whose __dict__ is the function's globals.
        """
        import sys
        from .intent import _extract_adr_rule as _fn

        # _fn.__module__ is the authoritative sys.modules key whose global
        # namespace this function resolves names against at call time.
        actual_mod = sys.modules[_fn.__module__]
        monkeypatch.setattr(actual_mod, "_call", lambda *a, **kw: return_json)

    def test_extract_adr_rule_with_import_constraint(self, tmp_path, monkeypatch):
        """ADR describing a forbidden import returns a populated rule dict."""
        from .intent import _extract_adr_rule

        expected = {
            "rule_type": "forbidden_import",
            "source_module": "api",
            "forbidden_modules": ["db"],
            "required_modules": [],
            "rationale": "API layer must not depend on DB layer directly",
        }
        self._patch_call(monkeypatch, expected)

        rule = _extract_adr_rule(
            self._ADR_WITH_RULE, key="fake-key", model="claude-test"
        )
        assert rule is not None
        assert rule["rule_type"] == "forbidden_import"
        assert rule["source_module"] == "api"
        assert "db" in rule["forbidden_modules"]

    def test_extract_adr_rule_no_structural_constraint(self, monkeypatch):
        """ADR without import constraints returns None."""
        from .intent import _extract_adr_rule

        self._patch_call(monkeypatch, {"rule_type": None})

        rule = _extract_adr_rule(self._ADR_NO_RULE, key="fake-key", model="claude-test")
        assert rule is None

    def test_extract_adr_rule_no_key_returns_none(self):
        """When no API key is provided, extraction returns None without calling LLM."""
        from .intent import _extract_adr_rule

        rule = _extract_adr_rule(self._ADR_WITH_RULE, key="", model="claude-test")
        assert rule is None

    def test_emit_importlinter_contract_forbidden(self, tmp_path):
        """_emit_importlinter_contract produces valid .importlinter format for forbidden rule."""
        from .intent import _emit_importlinter_contract

        rule = {
            "rule_type": "forbidden_import",
            "source_module": "api",
            "forbidden_modules": ["db", "models"],
            "required_modules": [],
            "rationale": "API layer must not import DB layer",
        }
        text = _emit_importlinter_contract(rule, tmp_path)

        assert "[importlinter:contract:" in text
        assert "type = forbidden" in text
        assert "source_modules" in text
        assert "    api" in text
        assert "forbidden_modules" in text
        assert "    db" in text
        assert "    models" in text

    def test_emit_importlinter_contract_contains_rationale(self, tmp_path):
        """Contract name field contains the rationale text."""
        from .intent import _emit_importlinter_contract

        rule = {
            "rule_type": "forbidden_import",
            "source_module": "api",
            "forbidden_modules": ["db"],
            "required_modules": [],
            "rationale": "API layer must not depend on DB",
        }
        text = _emit_importlinter_contract(rule, tmp_path)
        assert "API layer must not depend on DB" in text

    def test_write_importlinter_contract_creates_file(self, tmp_path):
        """Writing a new contract creates .importlinter in project root."""
        from .intent import _write_importlinter_contract

        rule = {
            "rule_type": "forbidden_import",
            "source_module": "api",
            "forbidden_modules": ["db"],
            "required_modules": [],
            "rationale": "API must not import DB",
        }
        written = _write_importlinter_contract(rule, tmp_path)
        assert written is True

        dotfile = tmp_path / ".importlinter"
        assert dotfile.exists()
        content = dotfile.read_text()
        assert "[importlinter:contract:adr-api-forbidden-import-db]" in content
        assert "source_modules" in content
        assert "    api" in content

    def test_write_importlinter_contract_no_duplicate(self, tmp_path):
        """Writing the same contract twice skips the second write."""
        from .intent import _write_importlinter_contract

        rule = {
            "rule_type": "forbidden_import",
            "source_module": "api",
            "forbidden_modules": ["db"],
            "required_modules": [],
            "rationale": "API must not import DB",
        }
        first = _write_importlinter_contract(rule, tmp_path)
        second = _write_importlinter_contract(rule, tmp_path)

        assert first is True
        assert second is False


# ---------------------------------------------------------------------------
# L1.5 test-intent extraction
# ---------------------------------------------------------------------------


class TestTestIntent:
    """Tests for _extract_test_intent, _match_tests_for_module, and _find_test_files."""

    def test_description_strips_test_prefix_and_converts_underscores(self, tmp_path):
        """test_should_raise_on_empty_input → description 'should raise on empty input'."""
        from .intent import _extract_test_intent

        tf = tmp_path / "test_mymodule.py"
        tf.write_text(
            "def test_should_raise_on_empty_input():\n    assert True\n",
            encoding="utf-8",
        )
        signals = _extract_test_intent([tf])
        assert len(signals) == 1
        assert signals[0]["description"] == "should raise on empty input"
        assert signals[0]["test_name"] == "test_should_raise_on_empty_input"
        assert signals[0]["confidence"] == 0.75

    def test_syntax_error_file_is_skipped_gracefully(self, tmp_path):
        """A SyntaxError in a test file is caught; other files still processed."""
        import warnings

        from .intent import _extract_test_intent

        bad = tmp_path / "test_bad.py"
        bad.write_text("def test_broken(\n    # unclosed\n", encoding="utf-8")
        good = tmp_path / "test_good.py"
        good.write_text("def test_returns_true():\n    assert True\n", encoding="utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            signals = _extract_test_intent([bad, good])

        assert any("SyntaxError" in str(w.message) for w in caught)
        assert len(signals) == 1
        assert signals[0]["test_name"] == "test_returns_true"

    def test_non_test_prefix_functions_are_ignored(self, tmp_path):
        """Functions without test_ prefix are not extracted."""
        from .intent import _extract_test_intent

        tf = tmp_path / "test_mymodule.py"
        tf.write_text(
            "def helper():\n    pass\n\ndef test_valid():\n    assert True\n",
            encoding="utf-8",
        )
        signals = _extract_test_intent([tf])
        assert len(signals) == 1
        assert signals[0]["test_name"] == "test_valid"

    def test_class_method_includes_class_name(self, tmp_path):
        """Test methods inside a class with a base record the class name."""
        from .intent import _extract_test_intent

        tf = tmp_path / "test_mymodule.py"
        tf.write_text(
            "import unittest\n"
            "class TestFoo(unittest.TestCase):\n"
            "    def test_bar(self):\n"
            "        assert True\n",
            encoding="utf-8",
        )
        signals = _extract_test_intent([tf])
        assert len(signals) == 1
        assert signals[0]["class_name"] == "TestFoo"
        assert signals[0]["description"] == "bar"

    def test_assertion_pattern_captured(self, tmp_path):
        """First assert statement body is captured as assertion_pattern."""
        from .intent import _extract_test_intent

        tf = tmp_path / "test_mymodule.py"
        tf.write_text(
            "def test_value_is_positive():\n    assert value > 0\n",
            encoding="utf-8",
        )
        signals = _extract_test_intent([tf])
        assert signals[0]["assertion_pattern"] == "value > 0"

    def test_l15_coverage_assigned_when_tests_reference_module(self, tmp_path):
        """_match_tests_for_module returns signals when test file stem matches source stem."""

        from .intent import _extract_test_intent, _match_tests_for_module

        tf = tmp_path / "test_mymodule.py"
        tf.write_text("def test_does_something():\n    assert True\n", encoding="utf-8")
        source_path = tmp_path / "mymodule.py"
        source_path.write_text("# source\n", encoding="utf-8")

        all_signals = _extract_test_intent([tf])
        matched = _match_tests_for_module(source_path, all_signals)
        assert len(matched) == 1
        assert matched[0]["description"] == "does something"

    def test_no_match_for_unrelated_module(self, tmp_path):
        """Test file for 'other' does not match 'mymodule'."""
        from .intent import _extract_test_intent, _match_tests_for_module

        tf = tmp_path / "test_other.py"
        tf.write_text("def test_something():\n    assert True\n", encoding="utf-8")
        source_path = tmp_path / "mymodule.py"
        source_path.write_text("# source\n", encoding="utf-8")

        all_signals = _extract_test_intent([tf])
        matched = _match_tests_for_module(source_path, all_signals)
        assert matched == []

    def test_extract_intent_signals_includes_l15_block(self, tmp_path):
        """_extract_intent_signals includes TEST COVERAGE block when source_path given."""
        from .intent import _extract_intent_signals

        tf = tmp_path / "test_mymodule.py"
        tf.write_text(
            "def test_returns_positive_value():\n    assert result > 0\n",
            encoding="utf-8",
        )
        source_path = tmp_path / "mymodule.py"
        source_path.write_text("x = 1\n", encoding="utf-8")

        signals = _extract_intent_signals("x = 1\n", source_path=source_path)
        assert "L1.5" in signals
        assert "returns positive value" in signals

    def test_extract_intent_signals_no_l15_without_source_path(self):
        """Without source_path, no L1.5 block is emitted."""
        from .intent import _extract_intent_signals

        signals = _extract_intent_signals("x = 1\n")
        assert "L1.5" not in signals

    def test_synthesize_adr_contracts_no_key_returns_empty(self, tmp_path):
        """With no API key, synthesis returns empty list without error."""
        from .intent import _synthesize_adr_contracts
        from unittest.mock import MagicMock

        ctx = MagicMock()
        result = _synthesize_adr_contracts(
            ctx, tmp_path, key="", model="m", verbose=False
        )
        assert result == []

    def test_synthesize_adr_contracts_no_github_returns_empty(self, tmp_path):
        """When enrich_ctx has no github context, returns empty list."""
        from .intent import _synthesize_adr_contracts
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.github = None
        result = _synthesize_adr_contracts(
            ctx, tmp_path, key="key", model="m", verbose=False
        )
        assert result == []
