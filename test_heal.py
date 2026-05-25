"""Tests for pact heal oracle autodetection and safety gap enforcement."""

import json
from pathlib import Path

import pytest

from .heal import HealResult, _autodetect_test_cmd, _func_body_at_line, heal_project


# ---------------------------------------------------------------------------
# _autodetect_test_cmd
# ---------------------------------------------------------------------------


class TestAutodetectTestCmd:
    def test_pytest_ini_detected(self, tmp_path):
        import sys

        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd is not None
        assert "pytest" in cmd
        assert sys.executable in cmd  # must use venv python, not bare "python"

    def test_pyproject_toml_detected(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.poetry]\n")
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd is not None
        assert "pytest" in cmd

    def test_conftest_detected(self, tmp_path):
        (tmp_path / "conftest.py").write_text("")
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd is not None
        assert "pytest" in cmd

    def test_tox_ini_detected(self, tmp_path):
        (tmp_path / "tox.ini").write_text("[tox]\n")
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd is not None
        assert "tox" in cmd

    def test_makefile_test_target_detected(self, tmp_path):
        (tmp_path / "Makefile").write_text("all:\n\techo hi\n\ntest:\n\tpytest\n")
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd is not None
        assert "make" in cmd

    def test_makefile_without_test_target_skipped(self, tmp_path):
        (tmp_path / "Makefile").write_text("all:\n\techo hi\n")
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd is None

    def test_empty_dir_returns_none(self, tmp_path):
        assert _autodetect_test_cmd(tmp_path) is None

    def test_pytest_takes_priority_over_tox(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        (tmp_path / "tox.ini").write_text("[tox]\n")
        cmd = _autodetect_test_cmd(tmp_path)
        assert "pytest" in cmd  # pytest wins

    def test_pyproject_pact_oracle_cmd_overrides_all(self, tmp_path):
        # [tool.pact].oracle_cmd wins over any auto-detected runner
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pact]\noracle_cmd = "make custom-test"\n'
        )
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd == "make custom-test"


# ---------------------------------------------------------------------------
# oracle_warning on HealResult
# ---------------------------------------------------------------------------


class TestFuncBodyAtLine:
    def _lines(self, source: str) -> list[str]:
        return [l + "\n" for l in source.splitlines()]

    def test_returns_enclosing_function(self):
        src = "def foo():\n    x = 1\n    return x\n\ndef bar():\n    y = 2\n"
        lines = self._lines(src)
        ctx, start, end = _func_body_at_line(lines, 2)
        assert "def foo" in ctx
        assert "def bar" not in ctx
        assert start == 1
        assert end == 3

    def test_returns_innermost_when_nested(self):
        src = "def outer():\n    def inner():\n        return 1\n    return inner\n"
        lines = self._lines(src)
        ctx, start, end = _func_body_at_line(lines, 3)
        assert "def inner" in ctx
        assert start == 2

    def test_falls_back_on_syntax_error(self):
        lines = ["def broken(:\n", "    pass\n"]
        ctx, start, end = _func_body_at_line(lines, 1)
        assert isinstance(ctx, str)
        assert len(ctx) > 0


class TestOracleWarning:
    def _empty_intent(self, tmp_path) -> Path:
        p = tmp_path / "intent.json"
        p.write_text(json.dumps({"modules": []}))
        return p

    def test_no_warning_when_no_patches(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        intent = self._empty_intent(tmp_path)
        result = heal_project(
            violations_path=intent,
            apply=True,
            project_root=tmp_path,
        )
        assert result.oracle_warning == ""

    def test_oracle_warning_field_exists_on_result(self):
        r = HealResult(project="test")
        assert r.oracle_warning == ""

    def test_oracle_warning_set_correctly(self):
        r = HealResult(project="test", patches_accepted=2)
        r.oracle_warning = "2 patch(es) applied with Z3 verification only"
        assert "Z3 verification only" in r.oracle_warning
