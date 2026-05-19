"""
Tests for ts_checker.py — JS/TS tree-sitter backed failure mode checkers.

Focuses on the llm_response_unguarded detector and JS file support.
Skips gracefully when tree-sitter-javascript / tree-sitter-typescript is absent.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

try:
    from .ts_checker import check_ts_file, _HAS_JS, _HAS_TS
except ImportError:
    from ts_checker import check_ts_file, _HAS_JS, _HAS_TS  # type: ignore

needs_ts = pytest.mark.skipif(
    not _HAS_TS, reason="tree-sitter-typescript not installed"
)
needs_js = pytest.mark.skipif(
    not _HAS_JS, reason="tree-sitter-javascript not installed"
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _write_and_check(tmp_path: Path, filename: str, src: str) -> list:
    f = tmp_path / filename
    f.write_text(textwrap.dedent(src))
    return check_ts_file(str(f))


# ---------------------------------------------------------------------------
# llm_response_unguarded — TypeScript
# ---------------------------------------------------------------------------


@needs_ts
def test_ts_choices_0_flagged(tmp_path):
    """response.choices[0] without optional chaining is flagged in TS."""
    src = """\
        const msg = response.choices[0].message.content;
    """
    viols = _write_and_check(tmp_path, "api.ts", src)
    llm = [v for v in viols if v.context == "llm_response_unguarded"]
    assert llm, "choices[0] should be flagged"
    assert "choices[0]" in llm[0].call


@needs_ts
def test_ts_choices_optional_chain_safe(tmp_path):
    """choices?.[0] optional chaining is NOT flagged."""
    src = """\
        const msg = response.choices?.[0]?.message?.content;
    """
    viols = _write_and_check(tmp_path, "api.ts", src)
    llm = [v for v in viols if v.context == "llm_response_unguarded"]
    assert not llm, f"optional chaining should not be flagged: {llm}"


@needs_ts
def test_ts_choices_message_content_flagged(tmp_path):
    """response.choices[0].message.content (multi-level) is flagged at the [0] access."""
    src = """\
        const content = response.choices[0].message.content;
    """
    viols = _write_and_check(tmp_path, "api.ts", src)
    llm = [v for v in viols if v.context == "llm_response_unguarded"]
    assert llm


@needs_ts
def test_ts_non_choices_subscript_ignored(tmp_path):
    """arr[0] where arr is not named 'choices' is NOT flagged."""
    src = """\
        const first = items[0];
    """
    viols = _write_and_check(tmp_path, "api.ts", src)
    llm = [v for v in viols if v.context == "llm_response_unguarded"]
    assert not llm


@needs_ts
def test_ts_choices_non_zero_index_ignored(tmp_path):
    """response.choices[1] is NOT flagged (only index 0 is special)."""
    src = """\
        const alt = response.choices[1];
    """
    viols = _write_and_check(tmp_path, "api.ts", src)
    llm = [v for v in viols if v.context == "llm_response_unguarded"]
    assert not llm


# ---------------------------------------------------------------------------
# llm_response_unguarded — JavaScript
# ---------------------------------------------------------------------------


@needs_js
def test_js_choices_0_flagged(tmp_path):
    """response.choices[0] without optional chaining is flagged in .js files."""
    src = """\
        const msg = response.choices[0].message.content;
    """
    viols = _write_and_check(tmp_path, "api.js", src)
    llm = [v for v in viols if v.context == "llm_response_unguarded"]
    assert llm, "choices[0] should be flagged in JS"


@needs_js
def test_js_optional_chain_safe(tmp_path):
    """choices?.[0] in JS is NOT flagged."""
    src = """\
        const msg = response.choices?.[0]?.message?.content;
    """
    viols = _write_and_check(tmp_path, "api.js", src)
    llm = [v for v in viols if v.context == "llm_response_unguarded"]
    assert not llm


@needs_js
def test_js_empty_catch_flagged(tmp_path):
    """catch (e) {} is flagged in .js files."""
    src = """\
        try {
          doSomething();
        } catch (e) {}
    """
    viols = _write_and_check(tmp_path, "api.js", src)
    ec = [v for v in viols if v.context == "empty_catch"]
    assert ec, "empty catch should be flagged in JS"


@needs_js
def test_js_catch_with_body_not_flagged(tmp_path):
    """catch (e) { console.error(e); } is NOT flagged."""
    src = """\
        try {
          doSomething();
        } catch (e) {
          console.error(e);
        }
    """
    viols = _write_and_check(tmp_path, "api.js", src)
    ec = [v for v in viols if v.context == "empty_catch"]
    assert not ec


# ---------------------------------------------------------------------------
# missing_await — false-positive regressions
# ---------------------------------------------------------------------------


@needs_js
def test_settimeout_not_flagged_as_missing_await(tmp_path):
    """setTimeout is NOT async (returns timer ID) — must not be flagged."""
    src = """\
        async function poll(id) {
          timerRef.current = setTimeout(() => poll(id), 1000);
        }
    """
    viols = _write_and_check(tmp_path, "poll.js", src)
    ma = [v for v in viols if v.context == "missing_await"]
    assert not ma, f"setTimeout falsely flagged: {ma}"


@needs_js
def test_string_trim_not_flagged_as_missing_await(tmp_path):
    """query.trim() on a string must not be flagged as missing_await."""
    src = """\
        async function search(query) {
          const q = query.trim();
          return q;
        }
    """
    viols = _write_and_check(tmp_path, "search.js", src)
    ma = [v for v in viols if v.context == "missing_await"]
    assert not ma, f"query.trim() falsely flagged: {ma}"


@needs_js
def test_axios_get_flagged_as_missing_await(tmp_path):
    """axios.get() inside an async function must still be flagged."""
    src = """\
        async function load(url) {
          const result = axios.get(url);
          return result;
        }
    """
    viols = _write_and_check(tmp_path, "load.js", src)
    ma = [v for v in viols if v.context == "missing_await"]
    assert ma, "axios.get() should be flagged as missing_await"


# ---------------------------------------------------------------------------
# Skip-dir filtering
# ---------------------------------------------------------------------------


@needs_ts
def test_node_modules_files_excluded(tmp_path):
    """Files inside node_modules are never scanned."""
    nm = tmp_path / "node_modules" / "openai" / "dist"
    nm.mkdir(parents=True)
    bad = nm / "index.ts"
    bad.write_text("const x = response.choices[0].message;")

    try:
        from .ts_checker import check_ts_files
    except ImportError:
        from ts_checker import check_ts_files  # type: ignore
    viols = check_ts_files(tmp_path)
    assert all("node_modules" not in v.file for v in viols)
