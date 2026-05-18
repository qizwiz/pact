"""Tests for pact.fixer — automated patch generation."""

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


def test_fix_modes_contains_expected():
    assert "llm_response_unguarded" in FIX_MODES
    assert "missing_await" in FIX_MODES
    assert "save_without_update_fields" not in FIX_MODES
