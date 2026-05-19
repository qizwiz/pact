"""
Tests for ts_fixer.py — JavaScript/TypeScript automated patch generation.

Covers:
  empty_catch fixer:
    - Truly empty catch (e) {} → inserts console.error(e)
    - Truly empty catch {} → inserts console.error(e) and adds binding
    - Comment-only bodies are skipped (intentional suppression)
    - Multi-line empty bodies are patched
  missing_await fixer:
    - axios.get() without await → adds await
    - fetch() without await → adds await
    - standalone unawaited call → adds await
    - Call not found in line → skipped
  TS_FIX_MODES contains expected entries
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

try:
    from .ts_fixer import TS_FIX_MODES, TsFileResult, fix_ts_file
except ImportError:
    from ts_fixer import TS_FIX_MODES, TsFileResult, fix_ts_file  # type: ignore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _viol(line: int, context: str = "empty_catch") -> object:
    return SimpleNamespace(
        line=line, call="catch", missing=["empty catch"], context=context
    )


def _fix(tmp_path: Path, filename: str, src: str, violations: list) -> TsFileResult:
    f = tmp_path / filename
    f.write_text(textwrap.dedent(src))
    return fix_ts_file(str(f), violations)


# ---------------------------------------------------------------------------
# TS_FIX_MODES
# ---------------------------------------------------------------------------


def test_fix_modes_contains_empty_catch():
    assert "empty_catch" in TS_FIX_MODES


def test_fix_modes_contains_missing_await():
    assert "missing_await" in TS_FIX_MODES


# ---------------------------------------------------------------------------
# Multi-line empty catch (e) — most common case
# ---------------------------------------------------------------------------


def test_empty_catch_with_param_multiline(tmp_path):
    src = """\
        function load() {
          try {
            return JSON.parse(data);
          } catch (err) {
          }
        }
    """
    # violation at line 4 (catch keyword)
    result = _fix(tmp_path, "a.js", src, [_viol(4)])
    assert result.changed
    assert len(result.applied) == 1
    assert "console.error(err);" in result.patched


def test_empty_catch_no_param_multiline(tmp_path):
    """catch {} without binding → add (e) binding and console.error(e)."""
    src = """\
        try {
          risky();
        } catch {
        }
    """
    result = _fix(tmp_path, "b.js", src, [_viol(3)])
    assert result.changed
    assert len(result.applied) == 1
    assert "catch (e)" in result.patched
    assert "console.error(e);" in result.patched


# ---------------------------------------------------------------------------
# Single-line empty catch
# ---------------------------------------------------------------------------


def test_empty_catch_single_line_with_param(tmp_path):
    src = "try { x(); } catch (e) {}\n"
    result = _fix(tmp_path, "c.js", src, [_viol(1)])
    assert result.changed
    assert "console.error(e);" in result.patched


def test_empty_catch_single_line_no_param(tmp_path):
    src = "try { x(); } catch {}\n"
    result = _fix(tmp_path, "d.js", src, [_viol(1)])
    assert result.changed
    assert "catch (e)" in result.patched
    assert "console.error(e);" in result.patched


# ---------------------------------------------------------------------------
# Comment-only body — must be skipped
# ---------------------------------------------------------------------------


def test_comment_only_body_skipped(tmp_path):
    src = """\
        try {
          doThing();
        } catch (e) {
          // intentionally ignored
        }
    """
    result = _fix(tmp_path, "e.js", src, [_viol(3)])
    assert not result.changed
    assert len(result.applied) == 0
    assert len(result.skipped) == 1


def test_block_comment_only_body_skipped(tmp_path):
    src = """\
        try {
          doThing();
        } catch {
          /* ignore */
        }
    """
    result = _fix(tmp_path, "f.js", src, [_viol(3)])
    assert not result.changed
    assert len(result.skipped) == 1


# ---------------------------------------------------------------------------
# Indentation preservation
# ---------------------------------------------------------------------------


def test_indentation_matches_closing_brace(tmp_path):
    src = """\
        function wrap() {
            try {
                risky();
            } catch (e) {
            }
        }
    """
    result = _fix(tmp_path, "g.js", src, [_viol(4)])
    assert result.changed
    # console.error should be indented 2 spaces more than the closing }
    lines = result.patched.splitlines()
    close_line = next(ln for ln in lines if ln.strip() == "}")
    error_line = next(ln for ln in lines if "console.error" in ln)
    close_indent = len(close_line) - len(close_line.lstrip())
    error_indent = len(error_line) - len(error_line.lstrip())
    assert error_indent == close_indent + 2


# ---------------------------------------------------------------------------
# Unknown mode — passed through to skipped
# ---------------------------------------------------------------------------


def test_unknown_mode_skipped(tmp_path):
    src = "try { x(); } catch (e) {}\n"
    v = _viol(1, context="some_other_mode")
    result = _fix(tmp_path, "h.js", src, [v])
    assert not result.changed
    assert len(result.skipped) == 1


# ---------------------------------------------------------------------------
# missing_await fixer
# ---------------------------------------------------------------------------


def _await_viol(line: int, call: str) -> object:
    return SimpleNamespace(
        line=line,
        call=call,
        missing=["async call without await — Promise never resolved"],
        context="missing_await",
    )


def test_missing_await_axios_get(tmp_path):
    """axios.get() inside async function gets await prepended."""
    src = """\
        async function load(url) {
          const result = axios.get(url);
          return result;
        }
    """
    result = _fix(tmp_path, "load.js", src, [_await_viol(2, "axios.get(url)")])
    assert result.changed
    assert len(result.applied) == 1
    assert "await axios.get(url)" in result.patched


def test_missing_await_fetch(tmp_path):
    """fetch() inside async function gets await prepended."""
    src = """\
        async function getData() {
          const resp = fetch("/api/data");
        }
    """
    result = _fix(tmp_path, "data.js", src, [_await_viol(2, 'fetch("/api/data")')])
    assert result.changed
    assert "await fetch" in result.patched


def test_missing_await_standalone_call(tmp_path):
    """Standalone unawaited call gets await prepended."""
    src = """\
        async function save(data) {
          axios.post("/save", data);
        }
    """
    result = _fix(
        tmp_path, "save.js", src, [_await_viol(2, 'axios.post("/save", data')]
    )
    assert result.changed
    assert "await axios.post" in result.patched


def test_missing_await_call_not_found_skipped(tmp_path):
    """If the call text can't be found in the line, skip rather than corrupt."""
    src = "async function x() { unknownFn(); }\n"
    result = _fix(tmp_path, "x.js", src, [_await_viol(1, "notInLine()")])
    assert not result.changed
    assert len(result.skipped) == 1


def test_missing_await_multiple_on_different_lines(tmp_path):
    """Two unawaited calls on different lines are both fixed."""
    src = """\
        async function multi() {
          const a = axios.get("/a");
          const b = fetch("/b");
        }
    """
    viols = [_await_viol(2, 'axios.get("/a")'), _await_viol(3, 'fetch("/b")')]
    result = _fix(tmp_path, "multi.js", src, viols)
    assert result.changed
    assert len(result.applied) == 2
    assert "await axios.get" in result.patched
    assert "await fetch" in result.patched
