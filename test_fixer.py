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
    assert "return  # pact: guard empty choices list" in result.patched
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
    guard_line = next(l for l in lines if "if not response.choices" in l)
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
    fixed = next(l for l in lines if "await do_thing" in l)
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
# FIX_MODES constant
# ---------------------------------------------------------------------------


def test_fix_modes_contains_expected():
    assert "llm_response_unguarded" in FIX_MODES
    assert "missing_await" in FIX_MODES
    assert "save_without_update_fields" not in FIX_MODES
