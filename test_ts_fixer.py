"""
Tests for ts_fixer.py — JavaScript/TypeScript automated patch generation.

Focuses on the empty_catch fixer:
  - Truly empty catch (e) {} → inserts console.error(e)
  - Truly empty catch {} → inserts console.error(e) and adds binding
  - Comment-only bodies are skipped (intentional suppression)
  - Multi-line empty bodies are patched
  - TS_FIX_MODES contains expected entries
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
