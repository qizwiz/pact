"""Tests for `pact rewrite` (vrefactor) — verified behaviour-preserving refactoring.

Each case pins a DIRECTION: a valid rewrite is applied, a behaviour-breaking one is rejected, the
{file} per-file oracle actually drives the loop, a broken pristine oracle is a harness error (not a
silent no-op), and the byte-cost metric is enforced. Skips cleanly when the tree-sitter extra is
absent, so CI without `pact-tool[refactor]` stays green rather than erroring.
"""

import json
import sys

import pytest

from . import vrefactor
from .vrefactor import rewrite, selftest

pytestmark = pytest.mark.skipif(
    not vrefactor._HAS_TS, reason="tree-sitter extra not installed"
)


def _py_oracle(d, mod):
    """An oracle command that imports and runs mod's test function inside dir d."""
    return "cd %s && %s -c 'import %s; %s.test()'" % (d, sys.executable, mod, mod)


def _done_event(capsys):
    """Return the JSON 'done' event rewrite() printed to stdout."""
    for line in reversed(capsys.readouterr().out.splitlines()):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event") == "done":
            return ev
    return None


def test_selftest_passes():
    assert selftest() == 0


def test_valid_rewrite_applied_and_shrinks(tmp_path, capsys):
    src = tmp_path / "a.py"
    src.write_text("def f(x):\n    x = x + 1\n    return x\n")
    (tmp_path / "test_a.py").write_text("import a\ndef test(): assert a.f(1) == 2\n")
    rc = rewrite(
        str(src), _py_oracle(tmp_path, "test_a"), "python", 60, False, 100, cwd=tmp_path
    )
    assert rc == 0
    got = src.read_text()
    assert "x += 1" in got and "x = x + 1" not in got
    done = _done_event(capsys)
    assert done["applied"] == 1
    assert done["bytes_after"] < done["bytes_before"]  # the cost metric is enforced


def test_behaviour_breaker_rejected(tmp_path):
    # A class with an inconsistent __ne__ makes `not (a == b)` != `a != b`; the oracle catches it.
    src = tmp_path / "b.py"
    src.write_text(
        "class W:\n"
        "    def __eq__(self, o): return True\n"
        "    def __ne__(self, o): return True\n"
        "def g():\n"
        "    return not (W() == W())\n"
    )
    (tmp_path / "test_b.py").write_text("import b\ndef test(): assert b.g() is False\n")
    rc = rewrite(
        str(src), _py_oracle(tmp_path, "test_b"), "python", 60, False, 100, cwd=tmp_path
    )
    assert rc == 0
    got = src.read_text()
    assert "not (W() == W())" in got and "W() != W()" not in got  # reverted


def test_file_template_reaches_per_file_oracle(tmp_path):
    # {file} is the mechanism the Lean oracle rides on: the oracle must see the file path.
    src = tmp_path / "c.py"
    src.write_text("def h(x):\n    x = x + 1\n    return x\n")
    (tmp_path / "test_c.py").write_text("import c\ndef test(): assert c.h(1) == 2\n")
    oracle = (
        "%s -m py_compile {file} && cd %s && %s -c 'import test_c; test_c.test()'"
        % (
            sys.executable,
            tmp_path,
            sys.executable,
        )
    )
    rc = rewrite(str(src), oracle, "python", 60, False, 100, cwd=tmp_path)
    assert rc == 0
    assert "x += 1" in src.read_text()


def test_broken_pristine_oracle_is_harness_error(tmp_path):
    # If the oracle fails on the UNMODIFIED file, that is a harness error (rc 2), not a green no-op.
    src = tmp_path / "d.py"
    original = "def f(x):\n    x = x + 1\n    return x\n"
    src.write_text(original)
    rc = rewrite(str(src), "false", "python", 60, False, 100, cwd=tmp_path)
    assert rc == 2
    assert src.read_text() == original  # untouched


def test_dry_run_changes_nothing(tmp_path):
    src = tmp_path / "e.py"
    original = "def f(x):\n    x = x + 1\n    return x\n"
    src.write_text(original)
    rc = rewrite(str(src), "true", "python", 60, True, 100, cwd=tmp_path)  # dry=True
    assert rc == 0
    assert src.read_text() == original


def test_lean_project_autodetects_lean_oracle(tmp_path):
    # A lakefile.lean in the tree → the autodetected oracle is a per-file `lake env lean {file}`.
    (tmp_path / "lakefile.lean").write_text("import Lake\n")
    f = tmp_path / "Foo.lean"
    f.write_text("theorem t : True := trivial\n")
    oracle = vrefactor.autodetect_oracle(tmp_path, f)
    assert oracle is not None
    assert "lake env lean" in oracle and "{file}" in oracle
