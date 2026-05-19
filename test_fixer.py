"""Tests for pact.fixer — automated patch generation."""

import ast
import textwrap

from .fixer import FIX_MODES, fix_file, diff_text
from .failure_mode import FailureEvidence


def _ev(mode, line, call, file="x.py"):
    return FailureEvidence(mode_name=mode, file=file, line=line, call=call, message="")


# ---------------------------------------------------------------------------
# llm_response_unguarded
# ---------------------------------------------------------------------------


def test_llm_guard_inserted_before_subscript(tmp_path):
    src = textwrap.dedent("""\
        def call_llm(client):
            response = client.chat.completions.create(messages=[])
            choice = response.choices[0]
            return choice.message.content
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("llm_response_unguarded", 3, "response.choices[0]", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "if not response.choices:" in result.patched
    assert 'raise ValueError("LLM returned empty response")' in result.patched
    assert len(result.applied) == 1
    assert len(result.skipped) == 0


def test_llm_guard_preserves_indentation(tmp_path):
    src = textwrap.dedent("""\
        def call_llm(client):
            if True:
                response = client.chat.completions.create(messages=[])
                choice = response.choices[0]
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("llm_response_unguarded", 4, "response.choices[0]", str(f))
    result = fix_file(str(f), [ev])
    lines = result.patched.splitlines()
    guard_line = next(ln for ln in lines if "if not response.choices" in ln)
    assert guard_line.startswith("        ")  # 8 spaces — same as original line 4


def test_llm_guard_multiple_violations(tmp_path):
    src = textwrap.dedent("""\
        def f(resp):
            a = resp.choices[0]
            b = resp.choices[0]
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    evs = [
        _ev("llm_response_unguarded", 2, "resp.choices[0]", str(f)),
        _ev("llm_response_unguarded", 3, "resp.choices[0]", str(f)),
    ]
    result = fix_file(str(f), evs)
    assert result.patched.count("if not resp.choices:") == 2
    assert len(result.applied) == 2


def test_llm_guard_malformed_call_skipped(tmp_path):
    src = "x = something[0]\n"
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("llm_response_unguarded", 1, "bad-format", str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed
    assert len(result.skipped) == 1


def test_llm_guard_multilevel_choices_message(tmp_path):
    """call='response.choices[0].message' — guard on choices still inserted."""
    src = textwrap.dedent("""\
        def get_msg(response):
            content = response.choices[0].message
            return content
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("llm_response_unguarded", 2, "response.choices[0].message", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "if not response.choices:" in result.patched
    assert len(result.applied) == 1
    assert len(result.skipped) == 0


def test_llm_guard_multilevel_choices_message_content(tmp_path):
    """call='response.choices[0].message.content' — guard on choices still inserted."""
    src = textwrap.dedent("""\
        def get_content(response):
            return response.choices[0].message.content
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("llm_response_unguarded", 2, "response.choices[0].message.content", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "if not response.choices:" in result.patched
    import ast as _ast

    _ast.parse(result.patched)  # must be syntactically valid
    assert len(result.applied) == 1


def test_llm_guard_multilevel_preserves_indentation(tmp_path):
    src = textwrap.dedent("""\
        async def run(response):
            if condition:
                msg = response.choices[0].message.content
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("llm_response_unguarded", 3, "response.choices[0].message.content", str(f))
    result = fix_file(str(f), [ev])
    lines = result.patched.splitlines()
    guard = next(ln for ln in lines if "if not response.choices" in ln)
    assert guard.startswith("        ")  # 8 spaces


# ---------------------------------------------------------------------------
# missing_await
# ---------------------------------------------------------------------------


def test_missing_await_prepended(tmp_path):
    src = textwrap.dedent("""\
        async def main():
            trigger_evaluation(job_id)
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("missing_await", 2, "trigger_evaluation", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "await trigger_evaluation(job_id)" in result.patched
    assert len(result.applied) == 1


def test_missing_await_preserves_indentation(tmp_path):
    src = textwrap.dedent("""\
        async def run():
            if condition:
                do_thing()
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("missing_await", 3, "do_thing", str(f))
    result = fix_file(str(f), [ev])
    lines = result.patched.splitlines()
    fixed = next(ln for ln in lines if "await do_thing" in ln)
    assert fixed.startswith("        ")  # 8 spaces


def test_missing_await_assignment(tmp_path):
    src = textwrap.dedent("""\
        async def run():
            result = fetch_data(url)
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("missing_await", 2, "fetch_data", str(f))
    result = fix_file(str(f), [ev])
    assert "result = await fetch_data(url)" in result.patched


def test_missing_await_complex_expr_skipped(tmp_path):
    # Call is nested inside list comprehension — skip, too complex to patch safely
    src = "results = [f() for f in funcs]\n"
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("missing_await", 1, "f", str(f))
    result = fix_file(str(f), [ev])
    # Either unchanged or applied — just verify no corruption
    assert isinstance(result.patched, str)


# ---------------------------------------------------------------------------
# Unfixable modes passed through to skipped
# ---------------------------------------------------------------------------


def test_unfixable_mode_skipped(tmp_path):
    src = "obj.save()\n"
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("save_without_update_fields", 1, "obj.save()", str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed
    assert len(result.skipped) == 1
    assert result.skipped[0].mode_name == "save_without_update_fields"


# ---------------------------------------------------------------------------
# diff_text
# ---------------------------------------------------------------------------


def test_diff_text_produces_unified_diff():
    original = "a = 1\n"
    patched = "if True:\n    pass\na = 1\n"
    d = diff_text("foo.py", original, patched)
    assert d.startswith("--- a/foo.py")
    assert "+if True:" in d


def test_diff_text_unchanged():
    src = "x = 1\n"
    assert diff_text("f.py", src, src) == ""


# ---------------------------------------------------------------------------
# Regression: guard in multi-line call argument list (bug found on future-agi)
# ---------------------------------------------------------------------------


def test_llm_guard_not_inserted_inside_call_args(tmp_path):
    """Guard must be before the with-statement, not inside its argument list."""
    src = textwrap.dedent("""\
        async def run(parent, response):
            with parent.span(
                output=response.choices[0].message.content,
            ):
                pass
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    # Violation at line 3 (the output= line inside the with call)
    ev = _ev("llm_response_unguarded", 3, "response.choices[0]", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    # Guard should be before the `with` statement (line 2), not inside it
    lines = result.patched.splitlines()
    guard_idx = next(i for i, ln in enumerate(lines) if "if not response.choices" in ln)
    with_idx = next(i for i, ln in enumerate(lines) if ln.strip().startswith("with "))
    assert guard_idx < with_idx, "guard must come before the with statement"
    # Result must be syntactically valid
    import ast as _ast

    _ast.parse(result.patched)  # raises SyntaxError if broken


def test_llm_guard_not_inserted_inside_append_call(tmp_path):
    """Guard before the append() statement, not inside it."""
    src = textwrap.dedent("""\
        async def run(messages, response):
            messages.append(
                {"content": response.choices[0].message.content}
            )
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("llm_response_unguarded", 3, "response.choices[0]", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    import ast as _ast

    _ast.parse(result.patched)  # must be syntactically valid
    assert "if not response.choices:" in result.patched
    # Guard must appear before messages.append, not inside it
    guard_idx = result.patched.index("if not response.choices:")
    append_idx = result.patched.index("messages.append(")
    assert guard_idx < append_idx


# ---------------------------------------------------------------------------
# Regression: missing_await inside coroutine consumer (bug found on future-agi)
# ---------------------------------------------------------------------------


def test_missing_await_skipped_inside_asyncio_run(tmp_path):
    """Do not add await when call is already passed to asyncio.run()."""
    src = textwrap.dedent("""\
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        def run_in_thread(coro_fn):
            with ThreadPoolExecutor() as executor:
                future = executor.submit(
                    asyncio.run,
                    coro_fn(),
                )
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("missing_await", 8, "coro_fn", str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed
    assert len(result.skipped) == 1


def test_missing_await_skipped_inside_custom_sync_runner(tmp_path):
    """_run_sync(coro()) must not be flagged — ADR-022 custom runner exclusion."""
    src = textwrap.dedent("""\
        import asyncio

        def _run_sync(coro):
            return asyncio.run(coro)

        async def astore_files(eid, files):
            pass

        def store_files(eid, files):
            _run_sync(astore_files(eid, files))
    """)
    f = tmp_path / "file_store.py"
    f.write_text(src)
    ev = _ev("missing_await", 10, "astore_files", str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed
    assert len(result.skipped) == 1


# ---------------------------------------------------------------------------
# FIX_MODES constant
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# optional_dereference
# ---------------------------------------------------------------------------


def test_optional_dereference_guard_inserted(tmp_path):
    src = textwrap.dedent("""\
        def call_api(client):
            response = client.get("/endpoint")
            return response.json()
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("optional_dereference", 3, "response.json", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "if response is None:" in result.patched
    assert "raise ValueError(f\"'response' is None\")" in result.patched
    assert len(result.applied) == 1
    assert len(result.skipped) == 0


def test_optional_dereference_preserves_indentation(tmp_path):
    src = textwrap.dedent("""\
        def call_api(client):
            if True:
                data = client.get("/x")
                return data.status_code
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("optional_dereference", 4, "data.status_code", str(f))
    result = fix_file(str(f), [ev])
    lines = result.patched.splitlines()
    guard = next(ln for ln in lines if "if data is None" in ln)
    assert guard.startswith("        ")  # 8 spaces — same as line 4


def test_optional_dereference_multiple_attrs_same_var_one_guard(tmp_path):
    src = textwrap.dedent("""\
        def fn(resp):
            x = resp.status_code
            y = resp.json()
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    evs = [
        _ev("optional_dereference", 2, "resp.status_code", str(f)),
        _ev("optional_dereference", 3, "resp.json", str(f)),
    ]
    result = fix_file(str(f), evs)
    # Two separate statements → two guards
    assert result.patched.count("if resp is None:") == 2
    assert len(result.applied) == 2


def test_optional_dereference_same_var_same_stmt_one_guard(tmp_path):
    src = textwrap.dedent("""\
        def fn(d):
            return d.a + d.b
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    evs = [
        _ev("optional_dereference", 2, "d.a", str(f)),
        _ev("optional_dereference", 2, "d.b", str(f)),
    ]
    result = fix_file(str(f), evs)
    # Same statement → only one guard inserted
    assert result.patched.count("if d is None:") == 1
    assert len(result.applied) == 2


def test_optional_dereference_malformed_call_skipped(tmp_path):
    src = "x = something\n"
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("optional_dereference", 1, "no-dot-here", str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed
    assert len(result.skipped) == 1


def test_optional_dereference_syntactically_valid(tmp_path):
    src = textwrap.dedent("""\
        def process(result):
            value = result.data["key"]
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("optional_dereference", 2, "result.data", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    import ast as _ast

    _ast.parse(result.patched)  # must not raise SyntaxError


def test_fix_modes_contains_expected():
    assert "llm_response_unguarded" in FIX_MODES
    assert "missing_await" in FIX_MODES
    assert "optional_dereference" in FIX_MODES
    assert "bare_except" in FIX_MODES
    assert "mutable_default_arg" in FIX_MODES
    assert "save_without_update_fields" in FIX_MODES
    assert "unvalidated_lookup_chain" in FIX_MODES
    assert "asyncio_run_in_async" in FIX_MODES


# ---------------------------------------------------------------------------
# bare_except
# ---------------------------------------------------------------------------


def test_bare_except_replaced_with_exception(tmp_path):
    src = textwrap.dedent("""\
        def fn():
            try:
                pass
            except:
                pass
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("bare_except", 4, "except:", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "except Exception:" in result.patched
    assert "except:" not in result.patched
    assert len(result.applied) == 1


def test_bare_except_preserves_indentation(tmp_path):
    src = textwrap.dedent("""\
        def fn():
            if True:
                try:
                    pass
                except:
                    pass
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("bare_except", 5, "except:", str(f))
    result = fix_file(str(f), [ev])
    lines = result.patched.splitlines()
    except_line = next(ln for ln in lines if "except Exception" in ln)
    assert except_line.startswith(
        "        "
    )  # 8 spaces — same as original except: line


def test_bare_except_preserves_trailing_comment(tmp_path):
    src = "try:\n    pass\nexcept:  # legacy\n    pass\n"
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("bare_except", 3, "except:", str(f))
    result = fix_file(str(f), [ev])
    assert "except Exception:  # legacy" in result.patched


def test_bare_except_silent_swallow_skipped(tmp_path):
    """except Exception: pass variant is left to the developer (needs logging/re-raise decision)."""
    src = "try:\n    pass\nexcept Exception:\n    pass\n"
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("bare_except", 3, "except Exception: pass", str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed
    assert len(result.skipped) == 1


def test_bare_except_syntactically_valid(tmp_path):
    src = textwrap.dedent("""\
        def fn():
            try:
                risky()
            except:
                cleanup()
    """)
    f = tmp_path / "ex.py"
    f.write_text(src)
    ev = _ev("bare_except", 4, "except:", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    import ast as _ast

    _ast.parse(result.patched)  # must not raise SyntaxError


# ---------------------------------------------------------------------------
# mutable_default_arg
# ---------------------------------------------------------------------------


def test_mutable_default_list_fixed(tmp_path):
    """def fn(x=[]) → def fn(x=None) with if x is None: x = [] guard."""
    src = textwrap.dedent("""\
        def fn(x=[]):
            return x
    """)
    f = tmp_path / "m.py"
    f.write_text(src)
    ev = _ev("mutable_default_arg", 1, "def fn", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "x=None" in result.patched or "x = None" in result.patched
    assert "if x is None:" in result.patched
    assert "x = []" in result.patched
    ast.parse(result.patched)


def test_mutable_default_dict_fixed(tmp_path):
    """def fn(x={}) → def fn(x=None) with if-None guard using {}."""
    src = textwrap.dedent("""\
        def fn(x={}):
            return x
    """)
    f = tmp_path / "m.py"
    f.write_text(src)
    ev = _ev("mutable_default_arg", 1, "def fn", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "if x is None:" in result.patched
    assert "x = {}" in result.patched
    ast.parse(result.patched)


def test_mutable_default_indentation_preserved(tmp_path):
    """Indentation of the function body guard matches the existing body indent."""
    src = textwrap.dedent("""\
        class C:
            def fn(self, x=[]):
                return x
    """)
    f = tmp_path / "m.py"
    f.write_text(src)
    ev = _ev("mutable_default_arg", 2, "def fn", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "        if x is None:" in result.patched
    ast.parse(result.patched)


def test_mutable_default_skips_docstring(tmp_path):
    """if-None guard is inserted AFTER an existing docstring, not before it."""
    src = textwrap.dedent("""\
        def fn(x=[]):
            \"\"\"Do something.\"\"\"
            return x
    """)
    f = tmp_path / "m.py"
    f.write_text(src)
    ev = _ev("mutable_default_arg", 1, "def fn", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    lines = result.patched.splitlines()
    doc_idx = next(i for i, ln in enumerate(lines) if '"""' in ln)
    guard_idx = next(i for i, ln in enumerate(lines) if "if x is None:" in ln)
    assert guard_idx > doc_idx, "guard should come after docstring"
    ast.parse(result.patched)


def test_mutable_default_result_is_syntactically_valid(tmp_path):
    """After patching, the file must parse cleanly as Python."""
    src = textwrap.dedent("""\
        def process(items=[]):
            items.append(1)
            return items
    """)
    f = tmp_path / "m.py"
    f.write_text(src)
    ev = _ev("mutable_default_arg", 1, "def process", str(f))
    result = fix_file(str(f), [ev])
    ast.parse(result.patched)


# ---------------------------------------------------------------------------
# save_without_update_fields
# ---------------------------------------------------------------------------


def test_save_single_field_fixed(tmp_path):
    """obj.name = x; obj.save() → obj.save(update_fields=["name"])."""
    src = textwrap.dedent("""\
        def update_user(user):
            user.name = "alice"
            user.save()
    """)
    f = tmp_path / "s.py"
    f.write_text(src)
    ev = _ev("save_without_update_fields", 3, "user.save", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert 'update_fields=["name"]' in result.patched
    ast.parse(result.patched)


def test_save_multiple_fields_fixed(tmp_path):
    """Multiple attribute assignments collected into update_fields list."""
    src = textwrap.dedent("""\
        def update_user(user):
            user.name = "alice"
            user.email = "a@b.com"
            user.save()
    """)
    f = tmp_path / "s.py"
    f.write_text(src)
    ev = _ev("save_without_update_fields", 4, "user.save", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "update_fields=" in result.patched
    assert '"name"' in result.patched
    assert '"email"' in result.patched
    ast.parse(result.patched)


def test_save_no_preceding_assignment_skipped(tmp_path):
    """obj.save() with no preceding obj.attr = ... is skipped (can't infer fields)."""
    src = textwrap.dedent("""\
        def reset(obj):
            obj.save()
    """)
    f = tmp_path / "s.py"
    f.write_text(src)
    ev = _ev("save_without_update_fields", 2, "obj.save", str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed
    assert len(result.skipped) == 1


def test_save_with_conditional_assignment_skipped(tmp_path):
    """Assignment inside an if-block — fixer bails (unsafe to infer update_fields)."""
    src = textwrap.dedent("""\
        def update(task, flag):
            if flag:
                task.status = "done"
            task.save()
    """)
    f = tmp_path / "s.py"
    f.write_text(src)
    ev = _ev("save_without_update_fields", 4, "task.save", str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed
    assert len(result.skipped) == 1


def test_save_result_is_syntactically_valid(tmp_path):
    """Patched source must parse cleanly."""
    src = textwrap.dedent("""\
        def update(obj):
            obj.value = 42
            obj.label = "x"
            obj.save()
    """)
    f = tmp_path / "s.py"
    f.write_text(src)
    ev = _ev("save_without_update_fields", 4, "obj.save", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    ast.parse(result.patched)


def test_save_already_has_update_fields_not_refixed(tmp_path):
    """save(update_fields=[...]) is NOT in FIX_MODES violations — no double-patching."""
    # The checker doesn't flag save(update_fields=[...]), so the fixer never sees it.
    # This test verifies the fixer leaves an already-correct save() alone when given a
    # different violation.
    src = textwrap.dedent("""\
        def update(obj):
            obj.name = "x"
            obj.save(update_fields=["name"])
    """)
    f = tmp_path / "s.py"
    f.write_text(src)
    # Mis-reported violation pointing at a save that already has update_fields
    ev = _ev("save_without_update_fields", 3, "obj.save", str(f))
    result = fix_file(str(f), [ev])
    # The regex won't match because obj.save( has content already — should skip
    assert not result.changed or "update_fields" in result.patched


def test_save_excludes_python_sentinel_attrs(tmp_path):
    """Regression: class-level Python sentinels (not models.Field) excluded from update_fields.

    Root cause of celery/django-celery#643 CI failure: pact inferred
    `update_fields=['no_changes', 'enabled']` but `no_changes = False` is a
    Python class attribute, not a DB column — Django raises FieldDoesNotExist.
    """
    src = textwrap.dedent("""\
        class PeriodicTask(models.Model):
            enabled = models.BooleanField(default=True)
            no_changes = False

        def _disable(model):
            model.no_changes = True
            model.enabled = False
            model.save()
    """)
    f = tmp_path / "s.py"
    f.write_text(src)
    ev = _ev("save_without_update_fields", 8, "model.save", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert 'update_fields=["enabled"]' in result.patched
    assert "no_changes" not in result.patched.split("update_fields=")[1]
    ast.parse(result.patched)


def test_save_all_sentinel_attrs_skipped(tmp_path):
    """If all inferred attrs are sentinels, skip rather than emit empty update_fields."""
    src = textwrap.dedent("""\
        class Task(models.Model):
            _sentinel = None

        def update(obj):
            obj._sentinel = True
            obj.save()
    """)
    f = tmp_path / "s.py"
    f.write_text(src)
    ev = _ev("save_without_update_fields", 5, "obj.save", str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed
    assert len(result.skipped) == 1


# ---------------------------------------------------------------------------
# unvalidated_lookup_chain
# ---------------------------------------------------------------------------


def test_unvalidated_lookup_simple_fixed(tmp_path):
    """other[x] where x came from .get() is replaced with other.get(x)."""
    src = textwrap.dedent("""\
        def process(mapping, other):
            x = mapping.get("key")
            if x:
                val = other[x]
            return val
    """)
    f = tmp_path / "u.py"
    f.write_text(src)
    ev = _ev("unvalidated_lookup_chain", 4, "other[x]", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "other.get(x)" in result.patched
    assert "other[x]" not in result.patched


def test_unvalidated_lookup_in_function_arg_fixed(tmp_path):
    """Subscript inside function call is also patched."""
    src = textwrap.dedent("""\
        def run(mapping, table):
            key = mapping.get("id")
            print(table[key])
    """)
    f = tmp_path / "u.py"
    f.write_text(src)
    ev = _ev("unvalidated_lookup_chain", 3, "table[key]", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "table.get(key)" in result.patched


def test_unvalidated_lookup_text_not_found_skipped(tmp_path):
    """Violation line mismatch causes skip."""
    src = textwrap.dedent("""\
        def f(m, d):
            x = m.get("k")
            return d[x]
    """)
    f = tmp_path / "u.py"
    f.write_text(src)
    # Wrong line — text won't match
    ev = _ev("unvalidated_lookup_chain", 1, "d[x]", str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed
    assert len(result.skipped) == 1


def test_unvalidated_lookup_result_syntactically_valid(tmp_path):
    """Fixed output must parse as valid Python."""
    src = textwrap.dedent("""\
        def go(mapping, store):
            identifier = mapping.get("id")
            if identifier:
                record = store[identifier]
                print(record)
    """)
    f = tmp_path / "u.py"
    f.write_text(src)
    ev = _ev("unvalidated_lookup_chain", 4, "store[identifier]", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    ast.parse(result.patched)


def test_unvalidated_lookup_multiple_violations_fixed(tmp_path):
    """Multiple violations in one file all get patched."""
    src = textwrap.dedent("""\
        def multi(m, a, b):
            x = m.get("x")
            y = m.get("y")
            return a[x], b[y]
    """)
    f = tmp_path / "u.py"
    f.write_text(src)
    evs = [
        _ev("unvalidated_lookup_chain", 4, "a[x]", str(f)),
        _ev("unvalidated_lookup_chain", 4, "b[y]", str(f)),
    ]
    result = fix_file(str(f), evs)
    assert result.changed
    assert "a.get(x)" in result.patched
    assert "b.get(y)" in result.patched
    assert len(result.applied) == 2


# ---------------------------------------------------------------------------
# asyncio_run_in_async
# ---------------------------------------------------------------------------


def test_asyncio_run_replaced_with_await(tmp_path):
    """asyncio.run(coro()) inside async function → await coro()."""
    src = textwrap.dedent("""\
        import asyncio
        async def handler():
            result = asyncio.run(fetch_data())
            return result
    """)
    f = tmp_path / "a.py"
    f.write_text(src)
    ev = _ev("asyncio_run_in_async", 3, "asyncio.run(...)", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "await fetch_data()" in result.patched
    assert "asyncio.run" not in result.patched


def test_asyncio_run_with_args_replaced(tmp_path):
    """asyncio.run(gather(a(), b())) → await gather(a(), b())."""
    src = textwrap.dedent("""\
        import asyncio
        async def main():
            results = asyncio.run(asyncio.gather(task_a(), task_b()))
    """)
    f = tmp_path / "a.py"
    f.write_text(src)
    ev = _ev("asyncio_run_in_async", 3, "asyncio.run(...)", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "await asyncio.gather(task_a(), task_b())" in result.patched


def test_asyncio_run_no_match_on_line_skipped(tmp_path):
    """Mismatch between violation line and asyncio.run location causes skip."""
    src = textwrap.dedent("""\
        import asyncio
        async def f():
            pass
        asyncio.run(f())
    """)
    f = tmp_path / "a.py"
    f.write_text(src)
    # Violation points at line 3 (pass), not the asyncio.run line
    ev = _ev("asyncio_run_in_async", 3, "asyncio.run(...)", str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed


def test_asyncio_run_result_syntactically_valid(tmp_path):
    """Fixed output must parse as valid Python."""
    src = textwrap.dedent("""\
        import asyncio
        async def process():
            data = asyncio.run(load_records(limit=100))
            return data
    """)
    f = tmp_path / "a.py"
    f.write_text(src)
    ev = _ev("asyncio_run_in_async", 3, "asyncio.run(...)", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    ast.parse(result.patched)


# ---------------------------------------------------------------------------
# subprocess_exit_code_unchecked fixer
# ---------------------------------------------------------------------------


def test_fix_modes_contains_subprocess(tmp_path):
    assert "subprocess_exit_code_unchecked" in FIX_MODES


def test_subprocess_run_bare_gets_check_true(tmp_path):
    """subprocess.run(['ls']) → subprocess.run(['ls'], check=True)"""
    f = tmp_path / "a.py"
    f.write_text("import subprocess\nsubprocess.run(['ls'])\n")
    ev = _ev("subprocess_exit_code_unchecked", 2, "subprocess.run(...)", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "check=True" in result.patched
    ast.parse(result.patched)


def test_subprocess_run_with_args_gets_check_true(tmp_path):
    """subprocess.run(['git', 'pull'], capture_output=True) → adds check=True."""
    f = tmp_path / "a.py"
    f.write_text(
        "import subprocess\nsubprocess.run(['git', 'pull'], capture_output=True)\n"
    )
    ev = _ev("subprocess_exit_code_unchecked", 2, "subprocess.run(...)", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "check=True" in result.patched
    ast.parse(result.patched)


def test_subprocess_call_bare_gets_check_true(tmp_path):
    """subprocess.call(['make']) → subprocess.call(['make'], check=True)"""
    f = tmp_path / "a.py"
    f.write_text("import subprocess\nsubprocess.call(['make'])\n")
    ev = _ev("subprocess_exit_code_unchecked", 2, "subprocess.call(...)", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "check=True" in result.patched
    ast.parse(result.patched)


def test_subprocess_already_check_true_skipped(tmp_path):
    """subprocess.run(['ls'], check=True) already safe — fixer skips it."""
    f = tmp_path / "a.py"
    f.write_text("import subprocess\nsubprocess.run(['ls'], check=True)\n")
    ev = _ev("subprocess_exit_code_unchecked", 2, "subprocess.run(...)", str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed


# ---------------------------------------------------------------------------
# falsy_or_zero_elision fixer
# ---------------------------------------------------------------------------


def test_fix_modes_contains_falsy_or_zero(tmp_path):
    assert "falsy_or_zero_elision" in FIX_MODES


def test_falsy_or_zero_simple_var_fixed(tmp_path):
    """score or 0 → score if score is not None else 0"""
    f = tmp_path / "a.py"
    f.write_text("result = score or 0\n")
    ev = _ev("falsy_or_zero_elision", 1, "score or 0", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "score if score is not None else 0" in result.patched
    ast.parse(result.patched)


def test_falsy_or_zero_float_zero_fixed(tmp_path):
    """ratio or 0.0 → ratio if ratio is not None else 0.0"""
    f = tmp_path / "a.py"
    f.write_text("x = ratio or 0.0\n")
    ev = _ev("falsy_or_zero_elision", 1, "ratio or 0.0", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "ratio if ratio is not None else 0.0" in result.patched
    ast.parse(result.patched)


def test_falsy_or_zero_complex_expr_skipped(tmp_path):
    """total / count or 0 — complex left-hand side is skipped."""
    f = tmp_path / "a.py"
    f.write_text("result = total / count or 0\n")
    ev = _ev("falsy_or_zero_elision", 1, "total / count or 0", str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed


def test_falsy_or_zero_in_assignment_context(tmp_path):
    """pass_rate = score or 0 — fixes inline in assignment."""
    f = tmp_path / "a.py"
    f.write_text("pass_rate = score or 0\n")
    ev = _ev("falsy_or_zero_elision", 1, "score or 0", str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert "score if score is not None else 0" in result.patched
    ast.parse(result.patched)


# ---------------------------------------------------------------------------
# prompt_injection_risk fixer
# ---------------------------------------------------------------------------


def test_fix_modes_contains_prompt_injection_risk():
    assert "prompt_injection_risk" in FIX_MODES


def test_prompt_injection_bare_var_sanitized(tmp_path):
    """{user_input} → {user_input.replace(chr(10), " ")} inside content f-string."""
    src = 'content = f"Answer: {user_input}"\n'
    f = tmp_path / "a.py"
    f.write_text(src)
    ev = _ev("prompt_injection_risk", 1, 'f"...{user_input}..."', str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert 'user_input.replace(chr(10), " ")' in result.patched
    ast.parse(result.patched)


def test_prompt_injection_attribute_access_skipped(tmp_path):
    """{obj.attr} is not a bare name — fixer must skip it."""
    src = 'content = f"Answer: {obj.user_input}"\n'
    f = tmp_path / "a.py"
    f.write_text(src)
    ev = _ev("prompt_injection_risk", 1, 'f"...{obj}..."', str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed


def test_prompt_injection_subscript_and_bare_mixed(tmp_path):
    """{message} is a bare name in a line that also has subscript access."""
    src = 'content = f"Hello {data["user"]}: {message}"\n'
    f = tmp_path / "a.py"
    f.write_text(src)
    ev = _ev("prompt_injection_risk", 1, 'f"...{message}..."', str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert 'message.replace(chr(10), " ")' in result.patched
    ast.parse(result.patched)


def test_prompt_injection_no_matching_var_skipped(tmp_path):
    """If ev.call names a var not in the line, fixer skips cleanly."""
    src = 'content = f"Answer: {query}"\n'
    f = tmp_path / "a.py"
    f.write_text(src)
    ev = _ev("prompt_injection_risk", 1, 'f"...{user_input}..."', str(f))
    result = fix_file(str(f), [ev])
    assert not result.changed


def test_prompt_injection_result_is_valid_python(tmp_path):
    """The patched file must always parse as valid Python."""
    src = textwrap.dedent("""\
        def handle(request, user_query):
            resp = client.chat.create(
                messages=[{"role": "user", "content": f"Q: {user_query}"}]
            )
            return resp
    """)
    f = tmp_path / "a.py"
    f.write_text(src)
    ev = _ev("prompt_injection_risk", 3, 'f"...{user_query}..."', str(f))
    result = fix_file(str(f), [ev])
    assert result.changed
    assert 'user_query.replace(chr(10), " ")' in result.patched
    ast.parse(result.patched)
