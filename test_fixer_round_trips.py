"""
Round-trip tests: pact checker detects violation → fixer patches → pact re-checks zero remain.

Each test uses pact's own scanner as the oracle — no hand-crafted expected strings.
This is the pact-generates-tests pattern: the checker verifies its own fixer output.

Implementation note: all _scan_file_* functions use lru_cache keyed on path.  We write the
fixed source to a *different* path so the cache does not return stale results.
"""

import textwrap
from pathlib import Path

from .failure_mode import (
    _scan_file_asyncio_run_in_async,
    _scan_file_bare_except,
    _scan_file_falsy_or_zero_elision,
    _scan_file_json_loads_unguarded,
    _scan_file_missing_await,
    _scan_file_mutable_defaults,
    _scan_file_optional_deref,
    _scan_file_subprocess_exit_code,
    _scan_file_timeout_not_set,
)
from .fixer import fix_file


def _round_trip(
    tmp_path: Path, source: str, scanner, mode_name: str
) -> tuple[int, int]:
    """
    Write source → scan → fix → re-scan on a different path (bypasses lru_cache).
    Returns (initial_violation_count, remaining_after_fix).
    """
    p = tmp_path / "src.py"
    p_fixed = tmp_path / "src_fixed.py"
    p.write_text(textwrap.dedent(source).lstrip())
    violations = [v for v in scanner(str(p)) if v.mode_name == mode_name]
    assert (
        violations
    ), f"test input has no {mode_name} violations — fix the synthetic source"
    result = fix_file(str(p), violations)
    p_fixed.write_text(result.patched)
    remaining = [v for v in scanner(str(p_fixed)) if v.mode_name == mode_name]
    return len(violations), len(remaining)


def test_bare_except_round_trip(tmp_path):
    src = """\
        def process(data):
            try:
                return int(data)
            except:
                pass
    """
    initial, remaining = _round_trip(
        tmp_path, src, _scan_file_bare_except, "bare_except"
    )
    assert initial >= 1
    assert remaining == 0, f"bare_except: {remaining} violations remain after fix"


def test_json_loads_round_trip(tmp_path):
    src = """\
        import json
        def parse_response(body):
            data = json.loads(body)
            return data["result"]
    """
    initial, remaining = _round_trip(
        tmp_path, src, _scan_file_json_loads_unguarded, "json_loads_unguarded"
    )
    assert initial >= 1
    assert (
        remaining == 0
    ), f"json_loads_unguarded: {remaining} violations remain after fix"


def test_optional_dereference_round_trip(tmp_path):
    src = """\
        def get_name(data):
            val = data.get("name")
            return val.strip()
    """
    initial, remaining = _round_trip(
        tmp_path, src, _scan_file_optional_deref, "optional_dereference"
    )
    assert initial >= 1
    assert (
        remaining == 0
    ), f"optional_dereference: {remaining} violations remain after fix"


def test_missing_await_round_trip(tmp_path):
    src = """\
        import asyncio

        async def fetch_data(url):
            return url

        def start():
            fetch_data("http://example.com")
    """
    initial, remaining = _round_trip(
        tmp_path, src, _scan_file_missing_await, "missing_await"
    )
    assert initial >= 1
    assert remaining == 0, f"missing_await: {remaining} violations remain after fix"


def test_mutable_default_arg_round_trip(tmp_path):
    src = """\
        def append_item(value, items=[]):
            items.append(value)
            return items
    """
    initial, remaining = _round_trip(
        tmp_path, src, _scan_file_mutable_defaults, "mutable_default_arg"
    )
    assert initial >= 1
    assert (
        remaining == 0
    ), f"mutable_default_arg: {remaining} violations remain after fix"


def test_falsy_or_zero_round_trip(tmp_path):
    src = "x = score or 0\n"
    initial, remaining = _round_trip(
        tmp_path, src, _scan_file_falsy_or_zero_elision, "falsy_or_zero_elision"
    )
    assert initial >= 1
    assert (
        remaining == 0
    ), f"falsy_or_zero_elision: {remaining} violations remain after fix"


def test_asyncio_run_in_async_round_trip(tmp_path):
    src = """\
        import asyncio
        async def fetch(url):
            result = asyncio.run(do_request(url))
            return result
    """
    initial, remaining = _round_trip(
        tmp_path, src, _scan_file_asyncio_run_in_async, "asyncio_run_in_async"
    )
    assert initial >= 1
    assert (
        remaining == 0
    ), f"asyncio_run_in_async: {remaining} violations remain after fix"


def test_subprocess_exit_code_round_trip(tmp_path):
    src = """\
        import subprocess
        def run_cmd(cmd):
            proc = subprocess.run(cmd, shell=True)
            return proc.stdout
    """
    initial, remaining = _round_trip(
        tmp_path, src, _scan_file_subprocess_exit_code, "subprocess_exit_code_unchecked"
    )
    assert initial >= 1
    assert (
        remaining == 0
    ), f"subprocess_exit_code_unchecked: {remaining} violations remain after fix"


def test_timeout_not_set_round_trip(tmp_path):
    src = """\
        import requests
        def fetch_data(url):
            response = requests.get(url)
            return response.json()
    """
    initial, remaining = _round_trip(
        tmp_path, src, _scan_file_timeout_not_set, "timeout_not_set"
    )
    assert initial >= 1
    assert remaining == 0, f"timeout_not_set: {remaining} violations remain after fix"
