"""
Property-based tests for pact's static checkers using Hypothesis.

Five-step methodology: TLA+ → ADR → Z3 → Hypothesis → integration probe.
This is the Hypothesis layer: we generate random-but-valid Python source fragments
and assert invariants about the checker output, not just spot-check examples.

Properties tested:
  - bare_except:            soundness (no false negatives) and precision (no false positives)
  - mutable_default_arg:    soundness and precision
  - save_without_update_fields: precision (safe saves never flagged)

Run: pytest test_hypothesis_checkers.py -v
"""

import tempfile
import textwrap

from hypothesis import given, settings
from hypothesis import strategies as st

from pact.failure_mode import (
    BARE_EXCEPT,
    MUTABLE_DEFAULT_ARG,
    SAVE_WITHOUT_UPDATE_FIELDS,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IDENTIFIER = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True)
_SAFE_VALUE = st.sampled_from(["None", "0", "''", '""', "True", "False", "42"])
_MUTABLE_VALUE = st.sampled_from(["[]", "{}", "set()"])
_EXCEPTION_NAME = st.sampled_from(
    ["ValueError", "KeyError", "TypeError", "RuntimeError", "OSError"]
)


def _file_violations(mode, source: str) -> list:
    """Write source to a temp file and run mode's file_check on it."""
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(textwrap.dedent(source))
        path = f.name
    if mode.file_check is None:
        return []
    return mode.file_check(path)


# ===========================================================================
# BARE_EXCEPT
# ===========================================================================


@given(_IDENTIFIER)
@settings(max_examples=100)
def test_bare_except_soundness(varname):
    """bare `except:` is always flagged, regardless of variable name in body."""
    source = f"""
        def fn():
            try:
                x = int({varname!r})
            except:
                pass
    """
    violations = _file_violations(BARE_EXCEPT, source)
    assert violations, f"bare except: not flagged for varname={varname!r}"


@given(_EXCEPTION_NAME)
@settings(max_examples=50)
def test_bare_except_precision_specific_exception(exc_name):
    """Specific exception types with real bodies are NOT flagged."""
    source = f"""
        def fn():
            try:
                x = 1
            except {exc_name} as e:
                raise RuntimeError("wrapped") from e
    """
    violations = _file_violations(BARE_EXCEPT, source)
    assert (
        not violations
    ), f"false positive: except {exc_name} with real body was flagged"


@given(_EXCEPTION_NAME)
@settings(max_examples=50)
def test_exception_with_log_body_not_flagged(exc_name):
    """except Exception: with a real handler body is NOT a violation."""
    source = f"""
        import logging
        log = logging.getLogger(__name__)
        def fn():
            try:
                x = 1
            except {exc_name} as e:
                log.exception("error: %s", e)
    """
    violations = _file_violations(BARE_EXCEPT, source)
    assert (
        not violations
    ), f"false positive: except {exc_name} with log call was flagged"


def test_except_exception_pass_flagged():
    """except Exception: pass IS a violation (silent swallow)."""
    source = """
        def fn():
            try:
                x = 1
            except Exception:
                pass
    """
    violations = _file_violations(BARE_EXCEPT, source)
    assert violations, "except Exception: pass should be flagged"


def test_except_exception_ellipsis_flagged():
    """except Exception: ... IS a violation (silent swallow)."""
    source = """
        def fn():
            try:
                x = 1
            except Exception:
                ...
    """
    violations = _file_violations(BARE_EXCEPT, source)
    assert violations, "except Exception: ... should be flagged"


# ===========================================================================
# MUTABLE_DEFAULT_ARG
# ===========================================================================


@given(_IDENTIFIER, _MUTABLE_VALUE)
@settings(max_examples=100)
def test_mutable_default_with_mutation_flagged(param_name, mutable):
    """def f(x=<mutable>) with a mutation in the body is flagged."""
    if mutable == "[]":
        mutation = f"{param_name}.append(1)"
    elif mutable == "{}":
        mutation = f'{param_name}["k"] = 1'
    else:  # set()
        mutation = f"{param_name}.add(1)"
    source = f"""
        def fn({param_name}={mutable}):
            {mutation}
            return {param_name}
    """
    violations = _file_violations(MUTABLE_DEFAULT_ARG, source)
    assert violations, f"def fn({param_name}={mutable}) with mutation not flagged"


@given(_IDENTIFIER, _SAFE_VALUE)
@settings(max_examples=100)
def test_safe_default_not_flagged(param_name, safe_val):
    """def f(x=<immutable>) is never flagged regardless of body."""
    source = f"""
        def fn({param_name}={safe_val}):
            return {param_name}
    """
    violations = _file_violations(MUTABLE_DEFAULT_ARG, source)
    assert (
        not violations
    ), f"false positive: def fn({param_name}={safe_val}) was flagged"


@given(_IDENTIFIER, _MUTABLE_VALUE)
@settings(max_examples=50)
def test_mutable_default_read_only_not_flagged(param_name, mutable):
    """def f(x=<mutable>) that only reads x (no mutation) is NOT flagged."""
    source = f"""
        def fn({param_name}={mutable}):
            return list({param_name})
    """
    violations = _file_violations(MUTABLE_DEFAULT_ARG, source)
    assert (
        not violations
    ), f"false positive: def fn({param_name}={mutable}) read-only was flagged"


# ===========================================================================
# SAVE_WITHOUT_UPDATE_FIELDS
# ===========================================================================


@given(
    _IDENTIFIER,
    st.lists(
        st.sampled_from(["name", "status", "value", "count"]), min_size=1, max_size=3
    ),
)
@settings(max_examples=50)
def test_save_with_update_fields_not_flagged(obj_name, fields):
    """model.save(update_fields=[...]) is never a violation."""
    if SAVE_WITHOUT_UPDATE_FIELDS.file_check is None:
        return
    field_list = str(fields)
    source = f"""
        from django.db import models
        class M(models.Model):
            name = models.CharField(max_length=64)
            class Meta: app_label = 'x'
        def update():
            obj = M.objects.get(pk=1)
            obj.name = "new"
            obj.save(update_fields={field_list})
    """
    violations = _file_violations(SAVE_WITHOUT_UPDATE_FIELDS, source)
    assert (
        not violations
    ), f"false positive: save(update_fields={field_list}) was flagged"


@given(
    st.sampled_from(["form", "serializer", "fs", "storage", "file", "input", "store"])
)
@settings(max_examples=20)
def test_safe_save_receiver_not_flagged(safe_receiver):
    """Known safe save() receivers (forms, serializers) are never flagged."""
    if SAVE_WITHOUT_UPDATE_FIELDS.file_check is None:
        return
    source = f"""
        from django.db import models
        def handler(self):
            self.{safe_receiver}.save()
    """
    violations = _file_violations(SAVE_WITHOUT_UPDATE_FIELDS, source)
    assert not violations, f"false positive: self.{safe_receiver}.save() was flagged"


# ===========================================================================
# Meta-property: checker output is deterministic
# ===========================================================================


@given(st.sampled_from([BARE_EXCEPT, MUTABLE_DEFAULT_ARG]))
@settings(max_examples=5)
def test_checker_is_deterministic(mode):
    """Running the same file twice produces the same violations."""
    source = """
        def fn(x=[]):
            x.append(1)
            try:
                pass
            except:
                pass
    """
    if mode.file_check is None:
        return
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(textwrap.dedent(source))
        path = f.name
    r1 = mode.file_check(path)
    r2 = mode.file_check(path)
    assert [(v.line, v.call) for v in r1] == [
        (v.line, v.call) for v in r2
    ], f"{mode.name} is non-deterministic"
